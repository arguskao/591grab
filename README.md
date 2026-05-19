# 591 Taiwan Real Estate Pipeline

Automated pipeline to collect, store, and analyze rental, resale, and new development listing data from [591.com.tw](https://www.591.com.tw/).

## Project Structure

```
591_pipeline/
├── config.yaml                 # All tunable parameters
├── requirements.txt
├── README.md
├── data/
│   ├── raw/                    # Date-stamped snapshots (rentals_2026-05-19.csv)
│   ├── processed/              # Weekly summary CSVs
│   ├── output/                 # Final CSVs + market summary markdown
│   └── charts/                 # Generated PNG charts
├── logs/                       # Rotating log files
└── src/
    ├── utils.py                # Shared: HTTP client, NUXT parser, normalization
    ├── scrape_rentals.py       # 租屋 rental listings
    ├── scrape_new_developments.py  # 新建案 (預售屋 + 新成屋)
    ├── scrape_resales.py       # 中古屋 resale listings
    └── process_weekly.py       # Weekly aggregation, charts, summary
```

## Setup

```bash
cd 591_pipeline
pip install -r requirements.txt
```

No additional dependencies required — uses `requests`, `pandas`, `matplotlib`, `pyyaml`.

## Configuration

Edit `config.yaml` to adjust:

- **`active_cities`**: which cities to scrape (default: `[1, 3, 8, 15, 17]` = 台北/新北/台中/台南/高雄)
- **`http.delay_min` / `delay_max`**: request delay range (default 2–4 seconds)
- **`rental.max_pages`**: safety cap per city (default 60 = ~1800 listings/city)
- Add more city IDs from `target_cities` to expand coverage

## Running the Scrapers

Each scraper runs independently and produces both a raw snapshot and a final CSV:

```bash
# Scrape rental listings
python src/scrape_rentals.py

# Scrape resale (中古屋) listings
python src/scrape_resales.py

# Scrape new development (新建案) listings
python src/scrape_new_developments.py
```

### Expected runtime

With 5 cities and 2–4s delay per request:
- **Rentals**: ~30–60 min (largest dataset, ~50 pages/city)
- **Resales**: ~30–60 min
- **New developments**: ~10–20 min (fewer pages)

### Output files

| Scraper | Raw snapshot | Final output |
|---------|-------------|--------------|
| Rentals | `data/raw/rentals_YYYY-MM-DD.csv` | `data/output/591_rentals.csv` |
| Resales | `data/raw/resales_YYYY-MM-DD.csv` | `data/output/591_resales.csv` |
| New Dev | `data/raw/new_developments_YYYY-MM-DD.csv` | `data/output/591_new_developments.csv` |

## Weekly Processing

After running the scrapers, generate analysis:

```bash
python src/process_weekly.py
```

This produces:
- `data/processed/weekly_rental_summary.csv`
- `data/processed/weekly_resale_summary.csv`
- `data/processed/weekly_new_development_summary.csv`
- `data/charts/*.png` — bar charts by city, trend charts (after 2+ weeks)
- `data/output/weekly_market_summary.md` — analytical summary

## Full Weekly Run

```bash
cd /Users/jimmylolo/591_pipeline
python src/scrape_rentals.py
python src/scrape_new_developments.py
python src/scrape_resales.py
python src/process_weekly.py
```

## Scheduling

### Recommended: macOS launchd (most reliable, no token cost)

Create `~/Library/LaunchAgents/com.jimmylolo.591-pipeline.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jimmylolo.591-pipeline</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>cd /Users/jimmylolo/591_pipeline && python3 src/scrape_rentals.py && python3 src/scrape_new_developments.py && python3 src/scrape_resales.py && python3 src/process_weekly.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/jimmylolo/591_pipeline/logs/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/jimmylolo/591_pipeline/logs/launchd_stderr.log</string>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.jimmylolo.591-pipeline.plist
```

### Alternative: cron

```bash
crontab -e
# Add:
0 6 * * 1 cd /Users/jimmylolo/591_pipeline && python3 src/scrape_rentals.py && python3 src/scrape_new_developments.py && python3 src/scrape_resales.py && python3 src/process_weekly.py >> logs/cron.log 2>&1
```

### Scheduling comparison

| Method | Pros | Cons |
|--------|------|------|
| **macOS launchd** | Runs on missed schedule, native, no tokens | macOS only |
| **cron** | Universal, simple | Doesn't run missed jobs |
| **GitHub Actions** | Cloud-hosted, cross-machine | Needs repo push, IP may get blocked |
| **Claude scheduled task** | Easy setup | Costs tokens, may timeout |

**Recommendation**: Use launchd for local weekly runs. It costs zero tokens and handles laptop-sleep recovery.

## API Endpoints Discovered

| Section | Endpoint | Auth |
|---------|----------|------|
| 租屋 Rent | `GET bff-house.591.com.tw/v3/web/rent/list` | None |
| 中古屋 Sale | `GET bff-house.591.com.tw/v1/web/sale/list` | T591_TOKEN cookie |
| 新建案 New | `GET api.591.com.tw/home/housing/list-search` | None |

## Key Fields

### Rental columns
`listing_id, title, city, district, address, rent, area_ping, property_type, floor, rooms, posted_date, scrape_date, days_on_market, allows_cooking, allows_pet, has_elevator, near_metro, listing_url`

### Resale columns
`listing_id, title, city, district, address, total_price_wan, total_price, unit_price_wan_per_ping, area_ping, main_area_ping, building_age, building_shape, rooms, floor, community_name, posted_date, scrape_date, days_on_market, listing_url`

### New development columns
`listing_id, project_name, city, district, developer, new_building_type, construction_status, unit_price_min_wan, unit_price_max_wan, total_price_min_wan, total_price_max_wan, area_min_ping, area_max_ping, room_config, listing_url`

## Known Limitations

1. **Rate limiting**: 591 may throttle or block after heavy use. The pipeline uses 2–4s randomized delays, but reduce `active_cities` or increase delays if issues arise.
2. **Sale API token**: The resale endpoint requires a `T591_TOKEN` cookie from `sale.591.com.tw`. The pipeline auto-fetches this, but the token may expire during long runs.
3. **Price display**: Some new development prices show "價格待定" (price TBD) — these are stored as null.
4. **Posted date**: Rental `posttime` is a unix timestamp. Resale `posttime` is also a timestamp. Some relative times like "3天前" are parsed as approximate dates.
5. **Anti-bot**: 591 uses Cloudflare. If `requests` gets blocked, the code can be adapted to use `cloudscraper` or subprocess `curl`.

## Continuing From Another Machine

1. Copy the entire `591_pipeline/` folder
2. Run `pip install -r requirements.txt`
3. Existing `data/raw/` snapshots carry over — `process_weekly.py` reads all historical snapshots for trend analysis
4. Adjust `config.yaml` paths if needed
