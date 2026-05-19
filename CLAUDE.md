# 591 Taiwan Real Estate Pipeline

## What this project does
Automated weekly scraping pipeline for Taiwan's 591.com.tw real estate platform.
Collects rental, resale (中古屋), and new development (新建案) listing data via JSON APIs,
then produces summary CSVs, matplotlib charts, and a markdown market report.

## How to run

### Full weekly pipeline
```bash
python3 src/scrape_rentals.py
python3 src/scrape_new_developments.py
python3 src/scrape_resales.py
python3 src/process_weekly.py
```

### Run a single scraper
```bash
python3 src/scrape_rentals.py      # 租屋
python3 src/scrape_resales.py      # 中古屋
python3 src/scrape_new_developments.py  # 新建案
```

## API endpoints (no HTML scraping — all JSON)
- Rent: `GET bff-house.591.com.tw/v3/web/rent/list?region={city}&firstRow={offset}`
- Sale: `GET bff-house.591.com.tw/v1/web/sale/list?type=2&shType=list&regionid={city}&firstRow={offset}`
- New: `GET api.591.com.tw/home/housing/list-search?regionid={city}&page={n}`

## Config
Edit `config.yaml` to change:
- `active_cities`: which cities to scrape (default: 台北/新北/台中/台南/高雄)
- `http.delay_min/max`: request delay (default 2–4s)
- `rental.max_pages`: safety cap per city

## Output locations
- `data/raw/` — date-stamped snapshots (rentals_YYYY-MM-DD.csv)
- `data/output/` — final CSVs (591_rentals.csv, 591_resales.csv, 591_new_developments.csv)
- `data/processed/` — weekly summary CSVs
- `data/charts/` — PNG charts
- `data/output/weekly_market_summary.md` — market report
- `logs/` — per-script log files

## Setup on a fresh machine
```bash
pip3 install -r requirements.txt
mkdir -p data/{raw,processed,output,charts,cache} logs
```

## Weekly scheduling (macOS launchd)
A launchd plist example is in README.md. Load with:
```bash
launchctl load ~/Library/LaunchAgents/com.jimmylolo.591-pipeline.plist
```

## Key design decisions
- Uses `requests` library with 2–4s randomized delays between API calls
- Each scraper deduplicates by listing_id
- Raw snapshots are date-stamped; process_weekly.py reads all historical snapshots for trend analysis
- Charts use matplotlib with PingFang TC font for Chinese labels
