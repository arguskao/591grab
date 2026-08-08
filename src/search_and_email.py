#!/usr/bin/env python3
"""Run configured 591 resale searches, save clean CSV files, and email them.

This entry point is intended for a weekly Render Cron Job. Search criteria live in
``scheduled_searches.yaml``; email credentials are read only from environment
variables.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrape_resales import normalize_resale
from utils import Http591Client, load_config, setup_logging


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEARCH_CONFIG = ROOT / "scheduled_searches.yaml"
RESEND_EMAIL_ENDPOINT = "https://api.resend.com/emails"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

TOP_LEVEL_KEYS = {"timezone", "output_dir", "email", "searches"}
EMAIL_KEYS = {"subject_prefix", "preview_limit"}
SEARCH_KEYS = {
    "id",
    "name",
    "enabled",
    "region_id",
    "city",
    "section_id",
    "district",
    "districts",
    "price_min_wan",
    "price_max_wan",
    "rooms_min",
    "posted_since",
    "posted_within_days",
    "max_pages",
}
DISTRICT_KEYS = {"section_id", "name"}

# Keep the listing URL as the only URL column. This prevents spreadsheet apps
# from visually combining it with a photo URL and producing a broken link.
CSV_COLUMNS = [
    "listing_id",
    "title",
    "city",
    "district",
    "address",
    "total_price_wan",
    "unit_price_wan_per_ping",
    "area_ping",
    "main_area_ping",
    "rooms",
    "living_rooms",
    "bathrooms",
    "property_type",
    "building_shape",
    "building_age",
    "floor",
    "total_floors",
    "has_carport",
    "community_name",
    "posted_date",
    "scrape_date",
    "days_on_market",
    "browse_count",
    "listing_url",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="搜尋 591 中古屋、輸出 CSV，並透過 Resend 寄信"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_SEARCH_CONFIG,
        help="搜尋條件 YAML（預設：scheduled_searches.yaml）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="執行搜尋並產生 CSV，但不寄信",
    )
    parser.add_argument(
        "--run-date",
        type=date.fromisoformat,
        help="指定執行日期 YYYY-MM-DD；主要供測試使用",
    )
    return parser.parse_args(argv)


def load_search_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"找不到搜尋設定檔：{path}")
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise ValueError("搜尋設定檔最外層必須是 YAML object")
    return config


def _normalize_districts(
    search: dict[str, Any], search_id: str
) -> list[dict[str, Any]]:
    """Validate old/new district syntax and return stable ID/name pairs."""

    has_multi = "districts" in search
    has_legacy_section = search.get("section_id") not in (None, "")
    has_legacy_name = search.get("district") not in (None, "")
    has_any_legacy = "section_id" in search or "district" in search

    if has_multi and has_any_legacy:
        raise ValueError(
            f"搜尋 {search_id} 的 districts 不可與 section_id / district 同時使用"
        )

    if has_multi:
        raw_districts = search.get("districts")
        if not isinstance(raw_districts, list) or not raw_districts:
            raise ValueError(f"搜尋 {search_id} 的 districts 必須是非空白清單")
    else:
        if not has_legacy_section or not has_legacy_name:
            raise ValueError(
                f"搜尋 {search_id} 必須設定 districts，"
                "或同時設定 section_id 與 district"
            )
        raw_districts = [
            {"section_id": search["section_id"], "name": search["district"]}
        ]

    districts: list[dict[str, Any]] = []
    seen_section_ids: set[int] = set()
    seen_names: set[str] = set()
    for district_index, raw_district in enumerate(raw_districts, start=1):
        if not isinstance(raw_district, dict):
            raise ValueError(
                f"搜尋 {search_id} 的 districts 第 {district_index} 筆必須是 object"
            )
        unknown = set(raw_district) - DISTRICT_KEYS
        if unknown:
            raise ValueError(
                f"搜尋 {search_id} 的 districts 第 {district_index} 筆含有未知欄位："
                + ", ".join(sorted(unknown))
            )
        if raw_district.get("section_id") in (None, ""):
            raise ValueError(
                f"搜尋 {search_id} 的 districts 第 {district_index} 筆缺少 section_id"
            )
        if raw_district.get("name") in (None, ""):
            raise ValueError(
                f"搜尋 {search_id} 的 districts 第 {district_index} 筆缺少 name"
            )
        raw_section_id = raw_district["section_id"]
        if isinstance(raw_section_id, bool):
            raise ValueError(
                f"搜尋 {search_id} 的 districts 第 {district_index} 筆 section_id 格式不正確"
            )
        if isinstance(raw_section_id, int):
            section_id = raw_section_id
        elif isinstance(raw_section_id, str) and re.fullmatch(
            r"[0-9]+", raw_section_id.strip()
        ):
            section_id = int(raw_section_id.strip())
        else:
            raise ValueError(
                f"搜尋 {search_id} 的 districts 第 {district_index} 筆 section_id 格式不正確"
            )

        raw_name = raw_district["name"]
        if not isinstance(raw_name, str):
            raise ValueError(
                f"搜尋 {search_id} 的 districts 第 {district_index} 筆 name 必須是文字"
            )
        name = raw_name.strip()
        if section_id < 1:
            raise ValueError(
                f"搜尋 {search_id} 的 districts 第 {district_index} 筆 section_id 必須大於 0"
            )
        if not name:
            raise ValueError(
                f"搜尋 {search_id} 的 districts 第 {district_index} 筆 name 不可為空"
            )
        if section_id in seen_section_ids:
            raise ValueError(f"搜尋 {search_id} 的行政區 ID 重複：{section_id}")
        if name in seen_names:
            raise ValueError(f"搜尋 {search_id} 的行政區名稱重複：{name}")
        seen_section_ids.add(section_id)
        seen_names.add(name)
        districts.append({"section_id": section_id, "name": name})

    return districts


def validate_searches(config: dict[str, Any]) -> list[dict[str, Any]]:
    unknown_top_level = set(config) - TOP_LEVEL_KEYS
    if unknown_top_level:
        raise ValueError(
            "搜尋設定檔含有未知欄位：" + ", ".join(sorted(unknown_top_level))
        )

    email_config = config.get("email") or {}
    if not isinstance(email_config, dict):
        raise ValueError("email 必須是 YAML object")
    unknown_email = set(email_config) - EMAIL_KEYS
    if unknown_email:
        raise ValueError("email 含有未知欄位：" + ", ".join(sorted(unknown_email)))
    try:
        preview_limit = int(email_config.get("preview_limit", 10))
    except (TypeError, ValueError) as exc:
        raise ValueError("email.preview_limit 必須是整數") from exc
    if not 1 <= preview_limit <= 100:
        raise ValueError("email.preview_limit 必須介於 1 與 100")

    raw_searches = config.get("searches")
    if not isinstance(raw_searches, list) or not raw_searches:
        raise ValueError("scheduled_searches.yaml 至少需要一組 searches")

    enabled: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_searches, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"searches 第 {index} 組必須是 YAML object")
        unknown_search = set(raw) - SEARCH_KEYS
        if unknown_search:
            raise ValueError(
                f"searches 第 {index} 組含有未知欄位："
                + ", ".join(sorted(unknown_search))
            )
        if not raw.get("enabled", True):
            continue

        search = dict(raw)
        search_id = str(search.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", search_id):
            raise ValueError(
                f"searches 第 {index} 組的 id 只能使用英文、數字、底線或連字號"
            )
        if search_id in seen_ids:
            raise ValueError(f"搜尋 id 重複：{search_id}")
        seen_ids.add(search_id)
        search["id"] = search_id

        for key in ("region_id", "city", "rooms_min"):
            if search.get(key) in (None, ""):
                raise ValueError(f"搜尋 {search_id} 缺少必要欄位：{key}")

        try:
            search["region_id"] = int(search["region_id"])
            search["rooms_min"] = int(search["rooms_min"])
            search["max_pages"] = int(search.get("max_pages", 20))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"搜尋 {search_id} 的數字欄位格式不正確") from exc

        if search["rooms_min"] < 1:
            raise ValueError(f"搜尋 {search_id} 的 rooms_min 必須至少為 1")
        if search["max_pages"] < 1:
            raise ValueError(f"搜尋 {search_id} 的 max_pages 必須至少為 1")

        search["districts"] = _normalize_districts(search, search_id)
        # Downstream code uses one canonical shape even when an older config
        # supplied the single-district section_id / district fields.
        search.pop("section_id", None)
        search.pop("district", None)

        for key in ("price_min_wan", "price_max_wan"):
            if search.get(key) is not None:
                try:
                    search[key] = float(search[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"搜尋 {search_id} 的 {key} 格式不正確") from exc
                if not math.isfinite(search[key]) or search[key] < 0:
                    raise ValueError(
                        f"搜尋 {search_id} 的 {key} 必須是大於等於 0 的有限數字"
                    )

        minimum = search.get("price_min_wan")
        maximum = search.get("price_max_wan")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"搜尋 {search_id} 的最低總價不可高於最高總價")

        has_fixed_date = search.get("posted_since") not in (None, "")
        has_relative_date = search.get("posted_within_days") not in (None, "")
        if has_fixed_date and has_relative_date:
            raise ValueError(
                f"搜尋 {search_id} 的 posted_since 與 posted_within_days 只能擇一"
            )
        if has_fixed_date:
            try:
                search["posted_since"] = date.fromisoformat(
                    str(search["posted_since"])
                ).isoformat()
            except ValueError as exc:
                raise ValueError(
                    f"搜尋 {search_id} 的 posted_since 必須是 YYYY-MM-DD"
                ) from exc
        if has_relative_date:
            try:
                search["posted_within_days"] = int(search["posted_within_days"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"搜尋 {search_id} 的 posted_within_days 必須是整數"
                ) from exc
            if search["posted_within_days"] < 1:
                raise ValueError(
                    f"搜尋 {search_id} 的 posted_within_days 必須至少為 1"
                )

        search["name"] = re.sub(
            r"[\r\n]+", " ", str(search.get("name") or search_id)
        ).strip()
        search["city"] = str(search["city"]).strip()
        if not search["name"]:
            search["name"] = search_id
        if not search["city"]:
            raise ValueError(f"搜尋 {search_id} 的 city 不可為空")
        enabled.append(search)

    if not enabled:
        raise ValueError("目前沒有 enabled: true 的搜尋條件")
    return enabled


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"無法辨識 timezone：{name}") from exc


def resolve_cutoff(search: dict[str, Any], run_date: date) -> date | None:
    if search.get("posted_since"):
        return date.fromisoformat(str(search["posted_since"]))
    if search.get("posted_within_days"):
        # Inclusive calendar-day window: 7 means today plus the preceding 6 days.
        return run_date - timedelta(days=int(search["posted_within_days"]) - 1)
    return None


def _query_number(value: float | int) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def build_price_query(search: dict[str, Any]) -> str | None:
    minimum = search.get("price_min_wan")
    maximum = search.get("price_max_wan")
    if minimum is None and maximum is None:
        return None
    low = _query_number(minimum) if minimum is not None else ""
    high = _query_number(maximum) if maximum is not None else ""
    # 591's current custom price syntax places a dollar sign after each bound.
    # Example: 600$_1200$ means NT$6M–12M (prices are expressed in 萬 here).
    return f"{low}$_{high}$"


def build_room_query(rooms_min: int) -> str:
    # 591 uses buckets 1..5; bucket 5 represents five rooms or more.
    start = min(max(int(rooms_min), 1), 5)
    return ",".join(str(value) for value in range(start, 6))


def build_query_params(
    search: dict[str, Any], per_page: int, first_row: int, total_rows: int
) -> dict[str, Any]:
    section_ids = [district["section_id"] for district in search["districts"]]
    section_query: int | str = (
        section_ids[0]
        if len(section_ids) == 1
        else ",".join(str(section_id) for section_id in section_ids)
    )
    params: dict[str, Any] = {
        "type": 2,
        "category": 1,
        "shType": "list",
        "regionid": search["region_id"],
        "section": section_query,
        "pattern": build_room_query(search["rooms_min"]),
        "firstRow": first_row,
        "totalRows": total_rows,
        "order": "postTime_desc",
    }
    price = build_price_query(search)
    if price:
        params["price"] = price
    relative_days = search.get("posted_within_days")
    if relative_days in {1, 3, 5, 7, 15, 30}:
        params["publish_day"] = int(relative_days)
    return params


def parse_item_posted_date(
    raw: Any, timezone: ZoneInfo, run_date: date
) -> date | None:
    if raw is None:
        return None

    if isinstance(raw, (int, float)) or str(raw).strip().isdigit():
        try:
            timestamp = float(raw)
        except (TypeError, ValueError):
            timestamp = 0
        if timestamp > 1_000_000_000:
            return datetime.fromtimestamp(timestamp, timezone).date()

    value = str(raw).strip()
    iso_match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if iso_match:
        try:
            return date(*(int(part) for part in iso_match.groups()))
        except ValueError:
            return None

    days_match = re.search(r"(\d+)\s*天前", value)
    if days_match:
        return run_date - timedelta(days=int(days_match.group(1)))
    if re.search(r"(\d+)\s*(小時|分鐘)前", value) or value in {"剛剛", "今天"}:
        return run_date
    return None


def normalize_scheduled_item(
    item: dict[str, Any], search: dict[str, Any], timezone: ZoneInfo, run_date: date
) -> dict[str, Any]:
    candidate = dict(item)
    candidate["_city_name"] = search["city"]
    candidate["_region_id"] = search["region_id"]
    row = normalize_resale(candidate)

    posted = parse_item_posted_date(item.get("posttime"), timezone, run_date)
    row["posted_date"] = posted.isoformat() if posted else None
    row["scrape_date"] = run_date.isoformat()
    row["days_on_market"] = (run_date - posted).days if posted else None
    return row


def row_matches_search(
    row: dict[str, Any], search: dict[str, Any], cutoff: date | None
) -> bool:
    # The API occasionally inserts promoted/recommended rows. Always enforce an
    # allowed section ID/name pair again after normalization.
    if str(row.get("city") or "").strip() != search["city"]:
        return False

    allowed_by_id = {
        str(district["section_id"]): district["name"]
        for district in search["districts"]
    }
    district_name = str(row.get("district") or "").strip()
    if district_name not in allowed_by_id.values():
        return False

    section_id = row.get("section_id")
    if (
        section_id not in (None, "")
        and allowed_by_id.get(str(section_id)) != district_name
    ):
        return False

    try:
        price = float(row["total_price_wan"])
        rooms = int(row["rooms"])
    except (KeyError, TypeError, ValueError):
        return False

    minimum = search.get("price_min_wan")
    maximum = search.get("price_max_wan")
    if minimum is not None and price < float(minimum):
        return False
    if maximum is not None and price > float(maximum):
        return False
    if rooms < int(search["rooms_min"]):
        return False

    if cutoff:
        try:
            posted = date.fromisoformat(str(row.get("posted_date")))
        except (TypeError, ValueError):
            return False
        if posted < cutoff:
            return False
    return True


def _sort_key(row: dict[str, Any]) -> tuple[int, float, str]:
    try:
        posted_ordinal = date.fromisoformat(str(row.get("posted_date"))).toordinal()
    except (TypeError, ValueError):
        posted_ordinal = 0
    try:
        price = float(row.get("total_price_wan"))
    except (TypeError, ValueError):
        price = math.inf
    return -posted_ordinal, price, str(row.get("listing_id") or "")


def listing_fingerprint(item: dict[str, Any]) -> tuple[str, ...]:
    """Identify duplicate aliases that 591 occasionally inserts.

    The API can return the same listing content twice with two house IDs, where
    the generated detail URL for the larger alias ID returns 404. Matching the
    stable content fields lets us keep one canonical result without combining
    genuinely different listings that merely share a title.
    """

    def clean(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    values = (
        clean(item.get("region_id")),
        clean(item.get("section_id")),
        clean(item.get("title")),
        clean(item.get("address")),
        clean(item.get("price")),
        clean(item.get("room")),
        clean(item.get("posttime")),
    )
    # Sparse rows are unsafe to merge; make their fingerprint ID-specific.
    if not values[2] or not values[6]:
        return (*values, clean(item.get("houseid")))
    return values


def _listing_id_rank(value: Any) -> tuple[int, str]:
    text = str(value or "").strip()
    try:
        return int(text), text
    except ValueError:
        return sys.maxsize, text


def search_listings(
    client: Http591Client,
    pipeline_config: dict[str, Any],
    search: dict[str, Any],
    timezone: ZoneInfo,
    run_date: date,
    logger: Any,
) -> list[dict[str, Any]]:
    sale = pipeline_config["sale"]
    per_page = int(sale.get("items_per_page", 30))
    max_pages = min(int(search["max_pages"]), int(sale.get("max_pages", 60)))
    cutoff = resolve_cutoff(search, run_date)

    results_by_fingerprint: dict[tuple[str, ...], dict[str, Any]] = {}
    seen_ids: set[str] = set()
    total_rows = 0
    completed = False

    for page in range(max_pages):
        first_row = page * per_page
        params = build_query_params(search, per_page, first_row, total_rows)
        response = client.get_json(
            sale["api_base"],
            sale["api_path"],
            params=params,
            referer=sale.get("referer", ""),
            origin=sale.get("origin", ""),
        )
        if str(response.get("status")) != "1":
            raise RuntimeError(
                f"591 API 回傳非成功狀態：{response.get('status')!r}"
            )

        inner = response.get("data") or {}
        if page == 0:
            try:
                total_rows = int(inner.get("total") or 0)
            except (TypeError, ValueError):
                total_rows = 0
            logger.info("%s：API 初步找到 %d 筆", search["name"], total_rows)

        items = inner.get("house_list") or inner.get("items") or []
        if not isinstance(items, list) or not items:
            if total_rows and first_row < total_rows:
                raise RuntimeError(
                    f"591 分頁提前結束：預期 {total_rows} 筆，offset={first_row}"
                )
            completed = True
            break

        page_resale_count = 0
        page_new_ids = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("is_newhouse") or str(item.get("type")) == "8":
                continue
            page_resale_count += 1

            listing_id = str(item.get("houseid") or "").strip()
            if not listing_id or listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)
            page_new_ids += 1

            try:
                row = normalize_scheduled_item(item, search, timezone, run_date)
            except Exception as exc:
                logger.warning("略過無法解析的物件 %s：%s", listing_id, exc)
                continue
            if row_matches_search(row, search, cutoff):
                fingerprint = listing_fingerprint(item)
                existing = results_by_fingerprint.get(fingerprint)
                if existing is None or _listing_id_rank(listing_id) < _listing_id_rank(
                    existing.get("listing_id")
                ):
                    results_by_fingerprint[fingerprint] = row

        logger.info(
            "%s：完成第 %d 頁，目前精確符合 %d 筆",
            search["name"],
            page + 1,
            len(results_by_fingerprint),
        )

        if total_rows and first_row + per_page >= total_rows:
            completed = True
            break
        if not total_rows and page_resale_count < per_page:
            completed = True
            break
        if page_new_ids == 0:
            raise RuntimeError("591 分頁沒有出現新的中古屋，為避免漏件已停止")

    if not completed:
        if total_rows:
            fetched_through = max_pages * per_page
            raise RuntimeError(
                f"搜尋結果超過 max_pages={max_pages}，"
                f"只抓到 offset {fetched_through} / {total_rows}；未寄成完整結果"
            )
        raise RuntimeError(
            f"已到 max_pages={max_pages}，但 591 未提供總筆數，無法確認結果完整"
        )

    results = list(results_by_fingerprint.values())
    results.sort(key=_sort_key)
    return results


def resolve_output_dir(config: dict[str, Any]) -> Path:
    configured = Path(str(config.get("output_dir", "data/output/scheduled")))
    if configured.is_absolute():
        raise ValueError("output_dir 必須是專案內的相對路徑")
    resolved = (ROOT / configured).resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError("output_dir 不可離開專案目錄")
    return resolved


def save_csv(rows: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows).reindex(columns=CSV_COLUMNS)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def criteria_text(search: dict[str, Any], run_date: date) -> str:
    minimum = search.get("price_min_wan")
    maximum = search.get("price_max_wan")
    if minimum is not None and maximum is not None:
        price = f"{_query_number(minimum)}–{_query_number(maximum)} 萬"
    elif minimum is not None:
        price = f"{_query_number(minimum)} 萬以上"
    elif maximum is not None:
        price = f"{_query_number(maximum)} 萬以下"
    else:
        price = "總價不限"

    cutoff = resolve_cutoff(search, run_date)
    posted = f"{cutoff.isoformat()} 起刊登" if cutoff else "刊登日不限"
    district_names = "、".join(
        district["name"] for district in search["districts"]
    )
    return (
        f"{search['city']}{district_names}；{price}；"
        f"{search['rooms_min']} 房以上；{posted}"
    )


def _display(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "—"
    try:
        if pd.isna(value):
            return "—"
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value}{suffix}"


def build_email_content(
    search_results: list[dict[str, Any]],
    errors: list[dict[str, str]],
    run_date: date,
    subject_prefix: str,
    preview_limit: int,
) -> tuple[str, str, str]:
    total = sum(len(result["rows"]) for result in search_results)
    subject_prefix = re.sub(r"[\r\n]+", " ", subject_prefix).strip()
    if not subject_prefix:
        subject_prefix = "591 每週物件"
    subject = f"[{subject_prefix}] {run_date.isoformat()}｜共 {total} 筆"
    if errors:
        subject += f"｜{len(errors)} 組失敗"

    summary_html: list[str] = []
    summary_text: list[str] = []
    table_rows: list[str] = []
    for result in search_results:
        search = result["search"]
        rows = result["rows"]
        description = criteria_text(search, run_date)
        summary_html.append(
            "<li><strong>"
            + html.escape(search["name"])
            + "</strong>："
            + str(len(rows))
            + " 筆<br><span style=\"color:#555\">"
            + html.escape(description)
            + "</span></li>"
        )
        summary_text.append(f"- {search['name']}：{len(rows)} 筆（{description}）")

        for row in rows[:preview_limit]:
            url = html.escape(str(row.get("listing_url") or ""), quote=True)
            title = html.escape(str(row.get("title") or "未命名物件"))
            title_link = f'<a href="{url}">{title}</a>' if url else title
            location = "".join(
                [
                    str(row.get("city") or ""),
                    str(row.get("district") or ""),
                    str(row.get("address") or ""),
                ]
            )
            table_rows.append(
                "<tr>"
                f"<td>{html.escape(search['name'])}</td>"
                f"<td>{html.escape(_display(row.get('posted_date')))}</td>"
                f"<td>{title_link}</td>"
                f"<td>{html.escape(location)}</td>"
                f"<td style=\"text-align:right\">{html.escape(_display(row.get('rooms'), ' 房'))}</td>"
                f"<td style=\"text-align:right\">{html.escape(_display(row.get('total_price_wan'), ' 萬'))}</td>"
                f"<td style=\"text-align:right\">{html.escape(_display(row.get('area_ping'), ' 坪'))}</td>"
                "</tr>"
            )

    error_html = ""
    error_text: list[str] = []
    if errors:
        items = "".join(
            f"<li><strong>{html.escape(error['name'])}</strong>："
            f"{html.escape(error['error'])}</li>"
            for error in errors
        )
        error_html = (
            "<h2 style=\"color:#b42318\">執行錯誤</h2>"
            f"<ul>{items}</ul>"
        )
        error_text = [f"- {error['name']}：{error['error']}" for error in errors]

    if table_rows:
        preview_html = (
            "<h2>物件預覽</h2>"
            "<p style=\"color:#555\">每組最多顯示 "
            f"{preview_limit} 筆；完整結果請見 CSV 附件。</p>"
            "<table style=\"border-collapse:collapse;width:100%;font-size:14px\">"
            "<thead><tr>"
            "<th style=\"text-align:left;border-bottom:2px solid #ddd;padding:8px\">搜尋</th>"
            "<th style=\"text-align:left;border-bottom:2px solid #ddd;padding:8px\">刊登日</th>"
            "<th style=\"text-align:left;border-bottom:2px solid #ddd;padding:8px\">物件</th>"
            "<th style=\"text-align:left;border-bottom:2px solid #ddd;padding:8px\">地址</th>"
            "<th style=\"text-align:right;border-bottom:2px solid #ddd;padding:8px\">房數</th>"
            "<th style=\"text-align:right;border-bottom:2px solid #ddd;padding:8px\">總價</th>"
            "<th style=\"text-align:right;border-bottom:2px solid #ddd;padding:8px\">坪數</th>"
            "</tr></thead><tbody>"
            + "".join(table_rows)
            + "</tbody></table>"
        )
    else:
        preview_html = "<p>這次沒有符合條件的物件；CSV 附件仍會保留欄位標題。</p>"

    html_body = (
        "<!doctype html><html><body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "color:#222;line-height:1.5\">"
        f"<h1>591 每週物件搜尋｜{run_date.isoformat()}</h1>"
        f"<p>本次精確符合共 <strong>{total}</strong> 筆。</p>"
        f"<ul>{''.join(summary_html)}</ul>"
        f"{error_html}{preview_html}"
        "<p style=\"margin-top:28px;color:#777;font-size:12px\">"
        "此信由排程自動產生；物件可能隨時下架或變更，請以 591 頁面為準。"
        "</p></body></html>"
    )

    text_lines = [
        f"591 每週物件搜尋｜{run_date.isoformat()}",
        f"本次精確符合共 {total} 筆。",
        "",
        *summary_text,
    ]
    if error_text:
        text_lines.extend(["", "執行錯誤：", *error_text])
    text_lines.extend(["", "完整結果與物件連結請見 CSV 附件。"])
    return subject, html_body, "\n".join(text_lines)


def parse_recipients(raw: str) -> list[str]:
    recipients = [part.strip() for part in re.split(r"[,;]", raw) if part.strip()]
    if not recipients:
        raise ValueError("EMAIL_TO 未設定收件地址")
    invalid = [recipient for recipient in recipients if "@" not in recipient]
    if invalid:
        raise ValueError(f"EMAIL_TO 含有無效地址：{', '.join(invalid)}")
    return recipients


def build_attachments(paths: list[Path]) -> list[dict[str, str]]:
    total_size = sum(path.stat().st_size for path in paths)
    if total_size > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"CSV 附件共 {total_size / 1024 / 1024:.1f} MB，"
            f"超過安全上限 {MAX_ATTACHMENT_BYTES / 1024 / 1024:.0f} MB"
        )
    return [
        {
            "filename": path.name,
            "content": base64.b64encode(path.read_bytes()).decode("ascii"),
        }
        for path in paths
    ]


def send_with_resend(
    *,
    api_key: str,
    sender: str,
    recipients: list[str],
    subject: str,
    html_body: str,
    text_body: str,
    attachments: list[Path],
    idempotency_key: str,
    timeout: int = 30,
) -> str:
    payload: dict[str, Any] = {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    if attachments:
        payload["attachments"] = build_attachments(attachments)

    response = requests.post(
        RESEND_EMAIL_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "User-Agent": "taiwan-home-listing-data-scraper-591/1.0",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = response.text[:500]
        raise RuntimeError(
            f"Resend 寄信失敗（HTTP {response.status_code}）：{detail}"
        ) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Resend 回應不是有效 JSON") from exc
    return str(body.get("id") or "unknown")


def make_idempotency_key(
    config_path: Path,
    run_date: date,
    attachments: list[Path],
    errors: list[dict[str, str]],
) -> str:
    hasher = hashlib.sha256(config_path.read_bytes())
    for path in sorted(attachments):
        hasher.update(path.name.encode("utf-8"))
        hasher.update(path.read_bytes())
    hasher.update(json.dumps(errors, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    digest = hasher.hexdigest()[:20]
    return f"weekly-591-{run_date.isoformat()}-{digest}"


def validate_location_mapping(
    searches: list[dict[str, Any]], pipeline_config: dict[str, Any]
) -> None:
    city_map = pipeline_config.get("target_cities") or {}
    for search in searches:
        configured_city = city_map.get(
            search["region_id"], city_map.get(str(search["region_id"]))
        )
        if configured_city and str(configured_city) != search["city"]:
            raise ValueError(
                f"搜尋 {search['id']} 的 region_id={search['region_id']} "
                f"在 config.yaml 對應 {configured_city}，不是 {search['city']}"
            )


def run(args: argparse.Namespace) -> int:
    os.chdir(ROOT)
    search_config_path = args.config.expanduser().resolve()
    search_config = load_search_config(search_config_path)
    searches = validate_searches(search_config)
    timezone = resolve_timezone(str(search_config.get("timezone", "Asia/Taipei")))
    run_date = args.run_date or datetime.now(timezone).date()

    pipeline_config = load_config(str(ROOT / "config.yaml"))
    validate_location_mapping(searches, pipeline_config)
    for search in searches:
        cutoff = resolve_cutoff(search, run_date)
        if cutoff and cutoff > run_date:
            raise ValueError(
                f"搜尋 {search['id']} 的刊登起始日 {cutoff} 晚於執行日 {run_date}"
            )

    api_key = ""
    recipients: list[str] = []
    sender = ""
    if not args.dry_run:
        api_key = os.environ.get("RESEND_API_KEY", "").strip()
        if not api_key:
            raise ValueError("缺少環境變數 RESEND_API_KEY")
        recipients = parse_recipients(os.environ.get("EMAIL_TO", ""))
        sender = os.environ.get(
            "EMAIL_FROM", "591 房屋搜尋 <onboarding@resend.dev>"
        ).strip()
        if not sender:
            raise ValueError("EMAIL_FROM 不可為空")

    logger = setup_logging("search_and_email", pipeline_config["paths"]["log_dir"])
    client = Http591Client(pipeline_config, logger)
    output_dir = resolve_output_dir(search_config)

    search_results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    attachments: list[Path] = []

    for search in searches:
        logger.info("=== 執行搜尋：%s ===", search["name"])
        try:
            rows = search_listings(
                client,
                pipeline_config,
                search,
                timezone,
                run_date,
                logger,
            )
            csv_path = save_csv(
                rows, output_dir / f"{search['id']}_{run_date.isoformat()}.csv"
            )
            attachments.append(csv_path)
            search_results.append({"search": search, "rows": rows, "path": csv_path})
            logger.info("%s：精確符合 %d 筆，已儲存 %s", search["name"], len(rows), csv_path)
        except Exception as exc:
            logger.exception("搜尋失敗：%s", search["name"])
            errors.append({"name": search["name"], "error": str(exc)})

    email_config = search_config.get("email") or {}
    preview_limit = int(email_config.get("preview_limit", 10))
    if preview_limit < 1:
        raise ValueError("email.preview_limit 必須至少為 1")
    subject, html_body, text_body = build_email_content(
        search_results,
        errors,
        run_date,
        str(email_config.get("subject_prefix", "591 每週物件")),
        preview_limit,
    )

    total = sum(len(result["rows"]) for result in search_results)
    if args.dry_run:
        logger.info("dry-run：略過寄信，共 %d 筆，CSV %d 份", total, len(attachments))
        for path in attachments:
            print(path)
        return 1 if errors else 0

    message_id = send_with_resend(
        api_key=api_key,
        sender=sender,
        recipients=recipients,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        attachments=attachments,
        idempotency_key=make_idempotency_key(
            search_config_path, run_date, attachments, errors
        ),
        timeout=int(pipeline_config["http"].get("timeout", 30)),
    )
    logger.info("寄信成功：Resend message id=%s", message_id)
    return 1 if errors else 0


def main() -> None:
    try:
        raise SystemExit(run(parse_args()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
