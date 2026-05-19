"""
Shared utilities for the 591.com.tw scraping pipeline.

Provides HTTP client with polite delays/retries, response caching,
NUXT SSR data parser, and data normalization helpers.
"""

import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str | None = None) -> dict:
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(name: str, log_dir: str = "logs") -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    today = datetime.now().strftime("%Y-%m-%d")
    fh = logging.FileHandler(
        Path(log_dir) / f"{name}_{today}.log", encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------

class Http591Client:
    """HTTP client with polite delays, retries, and disk caching."""

    def __init__(self, config: dict, logger: logging.Logger | None = None):
        self.cfg = config["http"]
        self.log = logger or logging.getLogger("http")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.cfg["user_agent"],
            "Accept": "application/json, text/html, */*; q=0.01",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        self.cache_dir = Path(self.cfg.get("cache_dir", "data/cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._csrf_tokens: dict[str, str] = {}

    # -- caching --

    def _cache_path(self, url: str, params: dict | None) -> Path:
        key = url + json.dumps(params or {}, sort_keys=True)
        h = hashlib.sha256(key.encode()).hexdigest()[:24]
        return self.cache_dir / f"{h}.html"

    def _is_cache_valid(self, path: Path) -> bool:
        if not path.exists():
            return False
        ttl = self.cfg.get("cache_ttl_hours", 6)
        age = time.time() - path.stat().st_mtime
        return age < ttl * 3600

    # -- polite delay --

    def _delay(self):
        lo = self.cfg.get("delay_min", 2.0)
        hi = self.cfg.get("delay_max", 4.0)
        d = random.uniform(lo, hi)
        time.sleep(d)

    # -- core GET with retries --

    def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        use_cache: bool = False,
    ) -> requests.Response:
        if use_cache:
            cp = self._cache_path(url, params)
            if self._is_cache_valid(cp):
                self.log.debug("Cache hit: %s", url)
                resp = requests.Response()
                resp.status_code = 200
                resp._content = cp.read_bytes()
                resp.encoding = "utf-8"
                return resp

        self._delay()
        max_retries = self.cfg.get("retry_max", 3)
        backoff = self.cfg.get("retry_backoff", 2.0)
        timeout = self.cfg.get("timeout", 30)

        for attempt in range(max_retries + 1):
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=timeout
                )
                if resp.status_code == 429:
                    wait = backoff ** (attempt + 1) + random.uniform(1, 3)
                    self.log.warning("Rate limited (429). Waiting %.1fs…", wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = backoff ** attempt + random.uniform(0, 1)
                    self.log.warning("Server error %d. Retrying in %.1fs…",
                                     resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()

                if use_cache:
                    cp.write_bytes(resp.content)
                return resp

            except requests.ConnectionError as exc:
                if attempt < max_retries:
                    wait = backoff ** attempt + random.uniform(0, 1)
                    self.log.warning("Connection error: %s. Retrying in %.1fs…",
                                     exc, wait)
                    time.sleep(wait)
                else:
                    raise

        raise RuntimeError(f"Failed after {max_retries + 1} attempts: {url}")

    # -- JSON API helper --

    def get_json(
        self,
        base_url: str,
        path: str,
        params: dict | None = None,
        referer: str = "",
        origin: str = "",
    ) -> dict:
        url = base_url.rstrip("/") + path
        headers = {}
        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        resp = self.get(url, params=params, headers=headers)
        return resp.json()

    # -- CSRF token (for endpoints that need it) --

    def get_csrf_token(self, token_url: str) -> str:
        if token_url in self._csrf_tokens:
            return self._csrf_tokens[token_url]
        self.log.debug("Fetching CSRF token from %s", token_url)
        resp = self.session.get(
            token_url,
            headers={"User-Agent": self.cfg["user_agent"]},
            timeout=self.cfg.get("timeout", 30),
            allow_redirects=True,
        )
        token = None
        for cookie in self.session.cookies:
            if cookie.name == "T591_TOKEN":
                token = cookie.value
                break
        if token:
            self._csrf_tokens[token_url] = token
            self.log.debug("Got CSRF token: %s…", token[:8])
        return token or ""


# ---------------------------------------------------------------------------
# NUXT SSR Parser (Nuxt 2 function-encoded data)
# ---------------------------------------------------------------------------

class NuxtParser:
    """
    Parses window.__NUXT__ = (function(a,b,...){return {...}}(v1,v2,...))
    by building a param->value mapping and resolving variable references.
    """

    @staticmethod
    def parse(html: str) -> dict | None:
        start = html.find("window.__NUXT__=(function(")
        if start == -1:
            return None

        script_end = html.find("</script>", start)
        nuxt_code = html[start:script_end]

        # Extract parameter names
        p_start = len("window.__NUXT__=(function(")
        p_end = nuxt_code.find(")", p_start)
        params = [p.strip() for p in nuxt_code[p_start:p_end].split(",")]

        # Find body / args boundary: '}(' near end
        last_pp = nuxt_code.rfind("))")
        i = last_pp
        args_str = ""
        body_end = 0
        while i > 0:
            if nuxt_code[i : i + 2] == "}(":
                candidate = nuxt_code[i + 2 : last_pp]
                if len(candidate) > 500:
                    args_str = candidate
                    body_end = i + 1
                    break
            i -= 1
        if not args_str:
            return None

        ret = nuxt_code.find("{return ", p_end)
        body_str = nuxt_code[ret + len("{return ") : body_end]

        # Parse argument values
        args = NuxtParser._parse_args(args_str)
        mapping = NuxtParser._build_mapping(params, args)
        return {"body": body_str, "mapping": mapping}

    @staticmethod
    def _parse_args(args_str: str) -> list[str]:
        args: list[str] = []
        current = ""
        in_string = False
        string_char = None
        depth = 0
        escape_next = False

        for ch in args_str:
            if escape_next:
                current += ch
                escape_next = False
                continue
            if ch == "\\" and in_string:
                current += ch
                escape_next = True
                continue
            if in_string:
                current += ch
                if ch == string_char:
                    in_string = False
                continue
            if ch in ('"', "'"):
                in_string = True
                string_char = ch
                current += ch
            elif ch == "," and depth == 0:
                args.append(current.strip())
                current = ""
            elif ch in ("(", "[", "{"):
                depth += 1
                current += ch
            elif ch in (")", "]", "}"):
                depth -= 1
                current += ch
            else:
                current += ch
        if current.strip():
            args.append(current.strip())
        return args

    @staticmethod
    def _build_mapping(params: list[str], args: list[str]) -> dict:
        mapping: dict = {}
        for idx, param in enumerate(params):
            if idx >= len(args):
                break
            val = args[idx].strip()
            if val == "void 0":
                mapping[param] = None
            elif val == "true":
                mapping[param] = True
            elif val == "false":
                mapping[param] = False
            elif val in ('""', "''"):
                mapping[param] = ""
            elif (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                mapping[param] = val[1:-1]
            else:
                try:
                    mapping[param] = int(val)
                except ValueError:
                    try:
                        mapping[param] = float(val)
                    except ValueError:
                        mapping[param] = val
        return mapping

    @staticmethod
    def resolve(var_name, mapping: dict):
        """Resolve a NUXT variable reference to its actual value."""
        val = mapping.get(var_name, var_name)
        if isinstance(val, str) and not val.startswith('"'):
            # might be a chained reference
            return mapping.get(val, val)
        return val

    @staticmethod
    def extract_items(body: str, mapping: dict, item_index: int = 0) -> list[dict]:
        """
        Extract items arrays from the NUXT body.
        item_index selects which items:[] occurrence to parse.
        """
        positions = [m.start() for m in re.finditer(r"items:\[", body)]
        if item_index >= len(positions):
            return []

        pos = positions[item_index]
        # Find matching ]
        start = pos + len("items:[")
        depth = 0
        end = start
        while end < len(body):
            if body[end] == "[":
                depth += 1
            elif body[end] == "]":
                if depth == 0:
                    break
                depth -= 1
            end += 1

        items_str = body[start:end]
        raw_items = re.split(r"\},\{", items_str)
        results = []
        for ri in raw_items:
            ri = ri.strip().lstrip("{").rstrip("}")
            fields = re.findall(
                r"(\w+):(" r'"[^"]*"' r"|" r"[a-zA-Z_$]\w*" r"|\d+(?:\.\d+)?)", ri
            )
            item: dict = {}
            for key, var in fields:
                if var.startswith('"'):
                    item[key] = var.strip('"').replace("\\u002F", "/")
                else:
                    resolved = mapping.get(var, var)
                    if isinstance(resolved, str):
                        resolved = resolved.replace("\\u002F", "/")
                    item[key] = resolved
            results.append(item)
        return results


# ---------------------------------------------------------------------------
# Data normalisation helpers
# ---------------------------------------------------------------------------

def normalize_price(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    s = str(raw).replace(",", "").replace("元", "").replace("/月", "").strip()
    if not s or s in ("面議", "價格待定", "洽詢", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_area(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    s = str(raw).replace("坪", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_floor(raw) -> tuple[int | None, int | None]:
    if not raw or not isinstance(raw, str):
        return None, None
    raw = raw.replace("\\u002F", "/").replace("F", "").replace("f", "")
    parts = raw.split("/")
    try:
        floor = int(parts[0].strip()) if parts[0].strip() else None
    except ValueError:
        floor = None
    try:
        total = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else None
    except ValueError:
        total = None
    return floor, total


def parse_room_str(raw: str) -> dict:
    """Parse '3房2廳2衛' into component counts."""
    result = {"rooms": None, "living_rooms": None, "bathrooms": None}
    if not raw:
        return result
    m = re.search(r"(\d+)\s*房", raw)
    if m:
        result["rooms"] = int(m.group(1))
    m = re.search(r"(\d+)\s*廳", raw)
    if m:
        result["living_rooms"] = int(m.group(1))
    m = re.search(r"(\d+)\s*衛", raw)
    if m:
        result["bathrooms"] = int(m.group(1))
    return result


def parse_posttime(raw) -> str | None:
    """Convert unix timestamp or relative string to YYYY-MM-DD."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and raw > 1_000_000_000:
        return datetime.fromtimestamp(raw).strftime("%Y-%m-%d")
    s = str(raw)
    m = re.search(r"(\d+)\s*天前", s)
    if m:
        days = int(m.group(1))
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*小時前", s)
    if m:
        return datetime.now().strftime("%Y-%m-%d")
    return None


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def ensure_dirs(config: dict):
    """Create all data directories from config."""
    for key in ("raw_dir", "processed_dir", "output_dir", "charts_dir", "log_dir"):
        Path(config["paths"][key]).mkdir(parents=True, exist_ok=True)
