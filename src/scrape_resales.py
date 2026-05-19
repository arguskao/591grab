#!/usr/bin/env python3
"""
Scrape 591.com.tw resale (中古屋) listings via the BFF JSON API.

API: GET https://bff-house.591.com.tw/v1/web/sale/list
     ?type=2&shType=list&regionid={city}&firstRow={offset}&totalRows={total}

The response embeds both promoted new-development ads (type=8, is_newhouse=1)
and actual resale listings (type="2") in house_list.  We keep only resale rows.

Outputs:
    data/raw/resales_{date}.csv     – raw scraped data
    data/output/591_resales.csv     – deduplicated final
"""

import os
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
    parse_floor,
    parse_posttime,
    parse_room_str,
    setup_logging,
    today_str,
)

ROOT = Path(__file__).resolve().parent.parent


def scrape_city(client, cfg, region_id, city_name, logger):
    """Scrape all resale listings for one city."""
    api_base = cfg["sale"]["api_base"]
    api_path = cfg["sale"]["api_path"]
    referer = cfg["sale"]["referer"]
    origin = cfg["sale"]["origin"]
    per_page = cfg["sale"]["items_per_page"]
    max_pages = cfg["sale"]["max_pages"]

    all_items = []
    seen_ids = set()
    total_rows = 0

    for page in range(max_pages):
        first_row = page * per_page
        params = {
            "type": 2,
            "shType": "list",
            "regionid": region_id,
            "firstRow": first_row,
            "totalRows": total_rows,
        }
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
        if page == 0 and inner.get("total"):
            total_rows = inner["total"]
            logger.info("%s: %s total sale listings", city_name, total_rows)

        items = inner.get("house_list", inner.get("items", []))
        if not items:
            logger.info("%s: no more items at page %d", city_name, page)
            break

        resale_count = 0
        for item in items:
            # Skip promoted new-development ads embedded in sale results
            if item.get("is_newhouse") or str(item.get("type")) == "8":
                continue

            lid = item.get("houseid")
            if lid and lid not in seen_ids:
                seen_ids.add(lid)
                item["_city_name"] = city_name
                item["_region_id"] = region_id
                all_items.append(item)
                resale_count += 1

        logger.info(
            "%s page %d: %d items, %d resale new (total %d)",
            city_name, page, len(items), resale_count, len(all_items),
        )

        if len(items) < per_page:
            break

    return all_items


def normalize_resale(item: dict) -> dict:
    """Transform a raw API item into a flat row."""
    floor, total_floors = parse_floor(item.get("floor", ""))
    room_info = parse_room_str(item.get("room", ""))
    posted = parse_posttime(item.get("posttime"))
    scrape = today_str()

    tags = item.get("tag", [])
    if not isinstance(tags, list):
        tags = []

    # price is in 萬 (ten-thousands TWD)
    price_wan = normalize_price(item.get("price"))
    total_price = price_wan * 10000 if price_wan else None

    unitprice_raw = item.get("unitprice")
    unit_price = normalize_price(unitprice_raw)

    return {
        "listing_id": item.get("houseid"),
        "listing_type": "resale",
        "title": item.get("title", ""),
        "city": item.get("_city_name", item.get("region_name", "")),
        "region_id": item.get("_region_id", item.get("region_id")),
        "district": item.get("section_name", ""),
        "section_id": item.get("section_id"),
        "address": (item.get("address") or "").replace("\\u002F", "/"),
        "total_price_wan": price_wan,
        "total_price": total_price,
        "show_price": item.get("showprice", ""),
        "unit_price_wan_per_ping": unit_price,
        "unit_price_display": (item.get("unit_price") or "").replace("\\u002F", "/"),
        "price_has_carport": item.get("price_has_carport"),
        "area_ping": normalize_area(item.get("area")),
        "main_area_ping": normalize_area(item.get("mainarea")),
        "floor": floor,
        "total_floors": total_floors,
        "floor_display": (item.get("floor") or "").replace("\\u002F", "/"),
        "rooms": room_info["rooms"],
        "living_rooms": room_info["living_rooms"],
        "bathrooms": room_info["bathrooms"],
        "room_display": item.get("room", ""),
        "property_type": item.get("kind_name", ""),
        "property_type_code": item.get("kind"),
        "building_shape": item.get("shape_name", ""),
        "building_age": item.get("houseage"),
        "building_age_display": item.get("showhouseage", ""),
        "has_carport": bool(item.get("has_carport")),
        "cart_model": item.get("cartmodel", ""),
        "community_name": item.get("community_name", ""),
        "community_link": item.get("community_link", ""),
        "lister_name": item.get("nick_name", ""),
        "lister_type_code": item.get("housetype"),
        "sale_type": item.get("saletype"),
        "is_down_price": bool(item.get("is_down_price")),
        "posted_date": posted,
        "scrape_date": scrape,
        "days_on_market": (
            (datetime.strptime(scrape, "%Y-%m-%d") - datetime.strptime(posted, "%Y-%m-%d")).days
            if posted else None
        ),
        "browse_count": item.get("browsenum"),
        "listing_url": f"https://sale.591.com.tw/home/house/detail/2/{item.get('houseid', '')}.html",
        "photo_url": (item.get("photo_url") or "").replace("\\u002F", "/"),
    }


def main():
    os.chdir(ROOT)
    config = load_config()
    ensure_dirs(config)
    logger = setup_logging("scrape_resales", config["paths"]["log_dir"])
    client = Http591Client(config, logger)

    active_cities = config.get("active_cities", [1])
    city_map = config["target_cities"]

    all_rows = []
    for region_id in active_cities:
        city_name = city_map.get(region_id, city_map.get(str(region_id), f"city_{region_id}"))
        logger.info("=== Scraping resales for %s (region=%s) ===", city_name, region_id)
        items = scrape_city(client, config, region_id, city_name, logger)
        for item in items:
            try:
                row = normalize_resale(item)
                all_rows.append(row)
            except Exception as exc:
                logger.warning("Normalize error for listing %s: %s",
                               item.get("houseid"), exc)

    df = pd.DataFrame(all_rows)
    if df.empty:
        logger.warning("No resale data collected.")
        return

    df.drop_duplicates(subset=["listing_id"], keep="first", inplace=True)
    logger.info("Total unique resale listings: %d", len(df))

    date = today_str()
    raw_path = Path(config["paths"]["raw_dir"]) / f"resales_{date}.csv"
    df.to_csv(raw_path, index=False, encoding="utf-8-sig")
    logger.info("Raw data saved to %s", raw_path)

    output_path = Path(config["paths"]["output_dir"]) / "591_resales.csv"
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info("Output saved to %s", output_path)

    logger.info(
        "Resale scrape complete: %d listings across %d cities",
        len(df), len(active_cities),
    )


if __name__ == "__main__":
    main()
