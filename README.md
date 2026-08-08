# 591 Taiwan Real Estate Pipeline

Automated pipeline to collect, store, and analyze rental, resale, and new development listing data from [591.com.tw](https://www.591.com.tw/).

## Project Structure

```
591_pipeline/
├── config.yaml                 # All tunable parameters
├── scheduled_searches.yaml     # Weekly filtered-search criteria
├── render.yaml                 # Render Cron Job definition
├── .env.example                # Email environment-variable template
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
    ├── search_and_email.py     # Filtered search + CSV + weekly email
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

## 每週條件搜尋並寄到 Gmail

這個流程不需要 FastAPI，也不需要另外架 API。Render Cron Job 每週直接執行
`python src/search_and_email.py`，搜尋完成後透過 Resend 寄一封信及 CSV 附件。

目前 `scheduled_searches.yaml` 已設定為：

- 台南市東區
- 總價 600–1200 萬（包含 600 與 1200）
- 5 房以上
- 2026-07-01 起刊登（包含當日）

591 偶爾會在搜尋結果混入推薦物件，因此程式還會在本機逐筆確認行政區、
總價、房數與刊登日。CSV 不包含圖片網址，`listing_url` 會維持為單一、可直接
開啟的物件連結。

### 1. 先在本機測試搜尋

```bash
python src/search_and_email.py --dry-run
```

`--dry-run` 會真的查詢 591 並在 `data/output/scheduled/` 產生 CSV，但不會寄信。

### 2. 設定 Resend 與 Gmail

1. 使用要收信的 Gmail 註冊 [Resend](https://resend.com/)。
2. 在 Resend 建立 API key。
3. 複製環境變數範例並填入自己的資料：

```bash
cp .env.example .env
```

`.env` 的內容：

```dotenv
RESEND_API_KEY=re_xxxxxxxxx
EMAIL_TO=your-name@gmail.com
EMAIL_FROM="591 房屋搜尋 <onboarding@resend.dev>"
```

使用 Resend 的 `onboarding@resend.dev` 測試寄件地址時，收件人必須是註冊
Resend 的同一個信箱。若將來要寄給其他人，需在 Resend 驗證自己的寄件網域，
再修改 `EMAIL_FROM`。

本機測試寄信：

```bash
set -a
source .env
set +a
python src/search_and_email.py
```

`.env` 已列入 `.gitignore`，不要將真正的 API key 提交到 Git。

### 3. 部署成 Render Cron Job

1. 將這個專案推送到 GitHub。
2. 到 [Render Dashboard](https://dashboard.render.com/) 選擇 **New > Blueprint**，
   連接此 GitHub repository。Render 會讀取根目錄的 `render.yaml`。
3. 在 Render 填入 `RESEND_API_KEY` 與 `EMAIL_TO`；不要把秘密寫入 YAML。
4. 建立後先使用 **Manual Trigger** 測試一次，確認 Gmail 收到信與 CSV。

`render.yaml` 的排程是 `0 0 * * 1`（UTC），換算為台灣時間是每週一上午
08:00。Render Cron Job 目前不是完全免費服務，部署前請在 Render 畫面確認
[Cron Job pricing](https://render.com/docs/cronjobs#pricing)。Render 的檔案系統不會
永久保存，但本流程會在同一次執行中把 CSV 寄出，因此不受影響。

### 修改搜尋條件

只需編輯 `scheduled_searches.yaml`：

| 欄位 | 用途 |
|---|---|
| `region_id` / `city` | 縣市 ID 與名稱 |
| `section_id` / `district` | 行政區 ID 與名稱 |
| `price_min_wan` / `price_max_wan` | 總價下限與上限，單位為萬 |
| `rooms_min` | 最少房數 |
| `posted_since` | 固定起始日，格式 `YYYY-MM-DD` |
| `posted_within_days` | 最近幾個台灣日曆日，包含執行日 |
| `enabled` | 設為 `false` 可暫停該組搜尋 |

`posted_since` 與 `posted_within_days` 只能擇一。現在使用固定
`posted_since: "2026-07-01"`，所以同一個仍在架上的物件可能每週重複出現；若只想
看最近一週，請刪除 `posted_since`，改成：

```yaml
posted_within_days: 7
```

需要同時搜尋不同條件時，可複製整組 `searches` 項目並給它不同的 ASCII `id`。
每週仍只寄一封信，每組搜尋各附一份 CSV。

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

## Local Scheduling for the Full Pipeline

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

**Recommendation**: Use Render Cron for the filtered Gmail report described above.
Use launchd when the full data pipeline should run locally and the Mac is available.

## API Endpoints Discovered

| Section | Endpoint | Auth |
|---------|----------|------|
| 租屋 Rent | `GET bff-house.591.com.tw/v3/web/rent/list` | None |
| 中古屋 Sale | `GET bff-house.591.com.tw/v1/web/sale/list` | No token currently required for list requests |
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
2. **Unofficial API**: The resale list request currently works without `T591_TOKEN`, but 591 may change its endpoint, parameters, or anti-bot rules without notice.
3. **Price display**: Some new development prices show "價格待定" (price TBD) — these are stored as null.
4. **Posted date**: Rental `posttime` is a unix timestamp. Resale `posttime` is also a timestamp. Some relative times like "3天前" are parsed as approximate dates.
5. **Anti-bot**: 591 uses Cloudflare. If `requests` gets blocked, the code can be adapted to use `cloudscraper` or subprocess `curl`.

## Continuing From Another Machine

1. Copy the entire `591_pipeline/` folder
2. Run `pip install -r requirements.txt`
3. Existing `data/raw/` snapshots carry over — `process_weekly.py` reads all historical snapshots for trend analysis
4. Adjust `config.yaml` paths if needed
# 591grab
