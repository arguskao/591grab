#!/usr/bin/env python3
"""
Scrape 591.com.tw new development (新建案) listings via the JSON API.

API: GET https://api.591.com.tw/home/housing/list-search
     ?regionid={city}&page={page}

Covers both 預售屋 (pre-sale) and 新成屋 (newly built) — differentiated
by the `status` field (1=預售屋, 2=新成屋, 3=待確認).

Outputs:
    data/raw/new_developments_{date}.csv     – raw scraped data
    data/output/591_new_developments.csv     – deduplicated final
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import (
    Http591Client,
    ensure_dirs,
    load_config,
    normalize_area,
    normalize_price,
    setup_logging,
    today_str,
)

ROOT = Path(__file__).resolve().parent.parent

STATUS_MAP = {1: "預售屋", 2: "新成屋", 3: "待確認"}


def parse_area_range(raw: str) -> tuple[float | None, float | None]:
    """Parse '26~43坪' into (26.0, 43.0)."""
    if not raw:
        return None, None
    nums = re.findall(r"[\d.]+", raw)
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    if len(nums) == 1:
        return float(nums[0]), float(nums[0])
    return None, None


def parse_price_range(raw: str) -> tuple[float | None, float | None, str]:
    """Parse price string like '2,280~3,580萬' or '68~80萬/坪'."""
    if not raw or raw in ("價格待定", "面議", "洽詢"):
        return None, None, ""
    unit = ""
    if "萬/坪" in raw or "萬元/坪" in raw:
        unit = "萬/坪"
    elif "萬" in raw:
        unit = "萬"
    nums = re.findall(r"[\d,.]+", raw)
    nums = [float(n.replace(",", "")) for n in nums if n.replace(",", "").replace(".", "").isdigit() or "." in n.replace(",", "")]
    if len(nums) >= 2:
        return nums[0], nums[1], unit
    if len(nums) == 1:
        return nums[0], nums[0], unit
    return None, None, unit


def scrape_city(client, cfg, region_id, city_name, logger):
    """Scrape all new development listings for one city."""
    api_base = cfg["newhouse"]["api_base"]
    api_path = cfg["newhouse"]["api_path"]
    referer = cfg["newhouse"]["referer"]
    origin = cfg["newhouse"]["origin"]
    max_pages = cfg["newhouse"]["max_pages"]

    all_items = []
    seen_ids = set()

    for page in range(1, max_pages + 1):
        params = {"regionid": region_id, "page": page}
        try:
            data = client.get_json(
                api_base, api_path, params=params, referer=referer, origin=origin
            )
        except Exception as exc:
            logger.error("API error for %s page %d: %s", city_name, page, exc)
            break

        if data.get("status") != 1:
            logger.warning("Non-success status for %s page %d: %s",
                           city_name, page, data.get("status"))
            break

        inner = data.get("data", {})
        if page == 1:
            total = inner.get("total", 0)
            total_pages = inner.get("total_page", 0)
            logger.info("%s: %d projects, %d pages", city_name, total, total_pages)

        items = inner.get("items", [])
        if not items:
            logger.info("%s: no more items at page %d", city_name, page)
            break

        new_count = 0
        for item in items:
            hid = item.get("hid")
            if hid and hid not in seen_ids:
                seen_ids.add(hid)
                item["_city_name"] = city_name
                item["_region_id"] = region_id
                all_items.append(item)
                new_count += 1

        logger.info(
            "%s page %d: %d items, %d new (total %d)",
            city_name, page, len(items), new_count, len(all_items),
        )

    return all_items


def normalize_newhouse(item: dict) -> dict:
    """Transform a raw API item into a flat row."""
    area_min, area_max = parse_area_range(item.get("area", ""))
    scrape = today_str()

    # Price parsing
    price_str = item.get("price", "")
    price_unit_str = item.get("price_unit", "")
    show_unitprice = item.get("show_unitprice", "")
    show_unitprice_unit = item.get("show_unitprice_unit", "")

    # Unit price per ping — try show_unitprice first, then fall back to price+price_unit
    up_min, up_max = None, None
    if show_unitprice:
        nums = re.findall(r"[\d,.]+", str(show_unitprice))
        nums = [float(n.replace(",", "")) for n in nums if n.replace(",", "").replace(".", "").isdigit() or "." in n.replace(",", "")]
        if len(nums) >= 2:
            up_min, up_max = nums[0], nums[1]
        elif len(nums) == 1:
            up_min = up_max = nums[0]

    if up_min is None and price_str and "萬" in str(price_unit_str) and "坪" in str(price_unit_str):
        nums = re.findall(r"[\d,.]+", str(price_str))
        nums = [float(n.replace(",", "")) for n in nums if n.replace(",", "").replace(".", "").isdigit() or "." in n.replace(",", "")]
        if len(nums) >= 2:
            up_min, up_max = nums[0], nums[1]
        elif len(nums) == 1:
            up_min = up_max = nums[0]

    # Total price range
    tp_min, tp_max = None, None
    all_price1 = item.get("all_price1")
    all_price2 = item.get("all_price2")
    if all_price1 and isinstance(all_price1, (int, float)) and all_price1 > 0:
        tp_min = all_price1
    if all_price2 and isinstance(all_price2, (int, float)) and all_price2 > 0:
        tp_max = all_price2

    # Fall back: parse total price from price field if unit is 萬 (not per-ping)
    if tp_min is None and price_str and price_unit_str and "坪" not in str(price_unit_str):
        nums = re.findall(r"[\d,.]+", str(price_str))
        nums = [float(n.replace(",", "")) for n in nums if n.replace(",", "").replace(".", "").isdigit() or "." in n.replace(",", "")]
        if len(nums) >= 2:
            tp_min, tp_max = nums[0], nums[1]
        elif len(nums) == 1:
            tp_min = tp_max = nums[0]

    status_code = item.get("status")
    status_name = STATUS_MAP.get(status_code, str(status_code))

    tags = item.get("tag", [])
    if not isinstance(tags, list):
        tags = []
    # Infer new_building_type from tags or status
    new_building_type = status_name
    if "預售屋" in tags:
        new_building_type = "預售屋"
    elif "新成屋" in tags:
        new_building_type = "新成屋"

    return {
        "listing_id": item.get("hid"),
        "listing_type": "new_development",
        "project_name": item.get("build_name", ""),
        "title": item.get("build_name", ""),
        "city": item.get("_city_name", item.get("region", "")),
        "region_id": item.get("_region_id", item.get("regionid")),
        "district": item.get("section", ""),
        "sectionid": item.get("sectionid"),
        "address": item.get("address", ""),
        "address_short": item.get("address_new", ""),
        "developer": item.get("shop_name", ""),
        "new_building_type": new_building_type,
        "construction_status": status_name,
        "construction_status_code": status_code,
        "sale_status": item.get("sale_status"),
        "build_type": item.get("build_type"),
        "purpose": item.get("purpose_str", ""),
        "price_display": price_str,
        "price_unit_display": price_unit_str,
        "unit_price_min_wan": up_min,
        "unit_price_max_wan": up_max,
        "unit_price_display": f"{show_unitprice} {show_unitprice_unit}".strip(),
        "total_price_min_wan": tp_min,
        "total_price_max_wan": tp_max,
        "area_min_ping": area_min,
        "area_max_ping": area_max,
        "area_display": item.get("area", ""),
        "room_config": item.get("room", ""),
        "open_sell_year_month": item.get("open_sell_year_month", ""),
        "update_time": item.get("updatetime", ""),
        "tags": ", ".join(tags) if tags else "",
        "call_count": item.get("call_num"),
        "phone": item.get("phone", ""),
        "has_video": bool(item.get("is_video")),
        "scrape_date": scrape,
        "listing_url": f"https://newhouse.591.com.tw/{item.get('hid', '')}",
        "cover_url": item.get("cover", ""),
    }


def main():
    os.chdir(ROOT)
    config = load_config()
    ensure_dirs(config)
    logger = setup_logging("scrape_new_developments", config["paths"]["log_dir"])
    client = Http591Client(config, logger)

    active_cities = config.get("active_cities", [1])
    city_map = config["target_cities"]

    all_rows = []
    for region_id in active_cities:
        city_name = city_map.get(region_id, city_map.get(str(region_id), f"city_{region_id}"))
        logger.info("=== Scraping new developments for %s (region=%s) ===", city_name, region_id)
        items = scrape_city(client, config, region_id, city_name, logger)
        for item in items:
            try:
                row = normalize_newhouse(item)
                all_rows.append(row)
            except Exception as exc:
                logger.warning("Normalize error for project %s: %s",
                               item.get("hid"), exc)

    df = pd.DataFrame(all_rows)
    if df.empty:
        logger.warning("No new development data collected.")
        return

    df.drop_duplicates(subset=["listing_id"], keep="first", inplace=True)
    logger.info("Total unique new development listings: %d", len(df))

    date = today_str()
    raw_path = Path(config["paths"]["raw_dir"]) / f"new_developments_{date}.csv"
    df.to_csv(raw_path, index=False, encoding="utf-8-sig")
    logger.info("Raw data saved to %s", raw_path)

    output_path = Path(config["paths"]["output_dir"]) / "591_new_developments.csv"
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info("Output saved to %s", output_path)

    logger.info(
        "New development scrape complete: %d projects across %d cities",
        len(df), len(active_cities),
    )


if __name__ == "__main__":
    main()
