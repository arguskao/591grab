#!/usr/bin/env python3
"""
Weekly processing pipeline for 591 real estate data.

Reads the latest scraped CSVs, computes summary statistics by city/district/week,
generates matplotlib charts and a markdown market summary.

Outputs:
    data/processed/weekly_rental_summary.csv
    data/processed/weekly_new_development_summary.csv
    data/processed/weekly_resale_summary.csv
    data/charts/*.png
    data/output/weekly_market_summary.md
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import ensure_dirs, load_config, setup_logging, today_str

ROOT = Path(__file__).resolve().parent.parent

# Chinese font for matplotlib
import matplotlib.font_manager as fm
_cjk_fonts = ["PingFang TC", "Heiti TC", "Apple LiGothic", "Arial Unicode MS",
              "Noto Sans CJK TC", "Microsoft JhengHei"]
_available = {f.name for f in fm.fontManager.ttflist}
for _fn in _cjk_fonts:
    if _fn in _available:
        plt.rcParams["font.sans-serif"] = [_fn] + plt.rcParams["font.sans-serif"]
        break
plt.rcParams["axes.unicode_minus"] = False


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_all_snapshots(raw_dir: str, prefix: str) -> pd.DataFrame:
    """Load all raw CSV snapshots matching prefix and concat."""
    raw_path = Path(raw_dir)
    files = sorted(raw_path.glob(f"{prefix}_*.csv"))
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        df = pd.read_csv(f, encoding="utf-8-sig")
        frames.append(df)
    combined = pd.concat(frames, ignore_index=True)
    if "listing_id" in combined.columns:
        combined.drop_duplicates(subset=["listing_id", "scrape_date"], keep="last", inplace=True)
    return combined


def add_week_column(df: pd.DataFrame) -> pd.DataFrame:
    """Add a week column based on scrape_date."""
    if "scrape_date" not in df.columns:
        return df
    df["scrape_date_dt"] = pd.to_datetime(df["scrape_date"], errors="coerce")
    df["week"] = df["scrape_date_dt"].dt.to_period("W").astype(str)
    return df


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def rental_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "rent" not in df.columns:
        return pd.DataFrame()
    df = add_week_column(df)
    valid = df[df["rent"].notna() & (df["rent"] > 0)].copy()

    grouped = valid.groupby(["week", "city", "district"]).agg(
        listing_count=("rent", "size"),
        average_rent=("rent", "mean"),
        rent_p25=("rent", lambda x: x.quantile(0.25)),
        rent_p50=("rent", "median"),
        rent_p75=("rent", lambda x: x.quantile(0.75)),
        average_area=("area_ping", "mean"),
        average_days_on_market=("days_on_market", "mean"),
        median_days_on_market=("days_on_market", "median"),
    ).reset_index()

    grouped = grouped.round(0)
    return grouped


def resale_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "total_price_wan" not in df.columns:
        return pd.DataFrame()
    df = add_week_column(df)
    valid = df[df["total_price_wan"].notna() & (df["total_price_wan"] > 0)].copy()

    grouped = valid.groupby(["week", "city", "district"]).agg(
        listing_count=("total_price_wan", "size"),
        average_total_price_wan=("total_price_wan", "mean"),
        price_p25=("total_price_wan", lambda x: x.quantile(0.25)),
        price_p50=("total_price_wan", "median"),
        price_p75=("total_price_wan", lambda x: x.quantile(0.75)),
        average_unit_price_wan=("unit_price_wan_per_ping", "mean"),
        unit_price_p25=("unit_price_wan_per_ping", lambda x: x.quantile(0.25)),
        unit_price_p50=("unit_price_wan_per_ping", "median"),
        unit_price_p75=("unit_price_wan_per_ping", lambda x: x.quantile(0.75)),
        average_area=("area_ping", "mean"),
        average_days_on_market=("days_on_market", "mean"),
        median_days_on_market=("days_on_market", "median"),
    ).reset_index()

    grouped = grouped.round(2)
    return grouped


def newdev_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    df = add_week_column(df)

    grouped = df.groupby(["week", "city", "district"]).agg(
        listing_count=("listing_id", "size"),
        avg_unit_price_min=("unit_price_min_wan", "mean"),
        avg_unit_price_max=("unit_price_max_wan", "mean"),
        unit_price_p50_min=("unit_price_min_wan", "median"),
        unit_price_p50_max=("unit_price_max_wan", "median"),
        presale_count=("new_building_type", lambda x: (x == "預售屋").sum()),
        new_built_count=("new_building_type", lambda x: (x == "新成屋").sum()),
    ).reset_index()

    grouped = grouped.round(2)
    return grouped


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def _filter_recent_weeks(df: pd.DataFrame, weeks: int = 13) -> pd.DataFrame:
    """Keep only the most recent N weeks of data."""
    if df.empty or "week" not in df.columns:
        return df
    unique_weeks = sorted(df["week"].unique())
    recent = unique_weeks[-weeks:]
    return df[df["week"].isin(recent)].copy()


def chart_rental_median_by_city(df: pd.DataFrame, charts_dir: str):
    """Bar chart of median rent by city for the latest snapshot."""
    if df.empty or "rent" not in df.columns:
        return
    valid = df[df["rent"].notna() & (df["rent"] > 0)]
    city_stats = valid.groupby("city")["rent"].median().sort_values(ascending=False)
    if city_stats.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    bars = ax.barh(city_stats.index, city_stats.values, color="#4A90D9")
    ax.set_xlabel("月租金中位數 (NT$)")
    ax.set_title("各縣市租金中位數")
    ax.invert_yaxis()
    for bar, val in zip(bars, city_stats.values):
        ax.text(val + 200, bar.get_y() + bar.get_height() / 2,
                f"${val:,.0f}", va="center", fontsize=9)
    plt.tight_layout()
    plt.savefig(Path(charts_dir) / "rental_median_by_city.png", dpi=150)
    plt.close()


def chart_resale_median_by_city(df: pd.DataFrame, charts_dir: str):
    """Bar chart of median total price by city."""
    if df.empty or "total_price_wan" not in df.columns:
        return
    valid = df[df["total_price_wan"].notna() & (df["total_price_wan"] > 0)]
    city_stats = valid.groupby("city")["total_price_wan"].median().sort_values(ascending=False)
    if city_stats.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    bars = ax.barh(city_stats.index, city_stats.values, color="#E87C5D")
    ax.set_xlabel("總價中位數 (萬元)")
    ax.set_title("各縣市中古屋總價中位數")
    ax.invert_yaxis()
    for bar, val in zip(bars, city_stats.values):
        ax.text(val + 20, bar.get_y() + bar.get_height() / 2,
                f"{val:,.0f}萬", va="center", fontsize=9)
    plt.tight_layout()
    plt.savefig(Path(charts_dir) / "resale_median_by_city.png", dpi=150)
    plt.close()


def chart_newdev_unit_price_by_city(df: pd.DataFrame, charts_dir: str):
    """Bar chart of median unit price range for new developments."""
    if df.empty or "unit_price_min_wan" not in df.columns:
        return
    valid = df[df["unit_price_min_wan"].notna()]
    stats = valid.groupby("city").agg(
        lo=("unit_price_min_wan", "median"),
        hi=("unit_price_max_wan", "median"),
    ).sort_values("hi", ascending=False)
    if stats.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    y = range(len(stats))
    ax.barh(y, stats["hi"] - stats["lo"], left=stats["lo"], color="#6BBF8A", height=0.6)
    ax.set_yticks(y)
    ax.set_yticklabels(stats.index)
    ax.set_xlabel("單價 (萬/坪)")
    ax.set_title("各縣市新建案單價中位數區間")
    ax.invert_yaxis()
    for i, (lo, hi) in enumerate(zip(stats["lo"], stats["hi"])):
        if pd.notna(lo) and pd.notna(hi):
            ax.text(hi + 0.5, i, f"{lo:.0f}~{hi:.0f}", va="center", fontsize=9)
    plt.tight_layout()
    plt.savefig(Path(charts_dir) / "newdev_unit_price_by_city.png", dpi=150)
    plt.close()


def chart_weekly_trend(
    summary: pd.DataFrame,
    value_col: str,
    title: str,
    ylabel: str,
    filename: str,
    charts_dir: str,
    top_n_cities: int = 5,
):
    """Line chart of a metric over weeks for the top N cities."""
    if summary.empty or value_col not in summary.columns:
        return
    summary = _filter_recent_weeks(summary)
    if summary.empty:
        return

    city_avg = summary.groupby("city")[value_col].mean().nlargest(top_n_cities)
    top_cities = city_avg.index.tolist()
    filtered = summary[summary["city"].isin(top_cities)]

    city_weekly = filtered.groupby(["week", "city"])[value_col].mean().unstack("city")

    fig, ax = plt.subplots(figsize=(12, 6))
    for city in top_cities:
        if city in city_weekly.columns:
            ax.plot(city_weekly.index, city_weekly[city], marker="o", label=city, linewidth=2)
    ax.set_xlabel("週")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(loc="best")
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(Path(charts_dir) / filename, dpi=150)
    plt.close()


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------

def generate_summary(
    rentals: pd.DataFrame,
    resales: pd.DataFrame,
    newdevs: pd.DataFrame,
    rental_sum: pd.DataFrame,
    resale_sum: pd.DataFrame,
    newdev_sum: pd.DataFrame,
) -> str:
    today = today_str()
    lines = [
        f"# 591 Weekly Real Estate Market Summary",
        f"Week ending: {today}",
        "",
    ]

    lines.append("## Key Takeaways")
    takeaways = []
    if not rentals.empty and "rent" in rentals.columns:
        med = rentals["rent"].median()
        cnt = len(rentals)
        takeaways.append(f"- **Rentals**: {cnt:,} active listings, median rent NT${med:,.0f}/月")
    if not resales.empty and "total_price_wan" in resales.columns:
        med = resales["total_price_wan"].median()
        cnt = len(resales)
        takeaways.append(f"- **Resales**: {cnt:,} listings, median price {med:,.0f}萬")
    if not newdevs.empty:
        cnt = len(newdevs)
        pre = (newdevs.get("new_building_type", pd.Series()) == "預售屋").sum()
        new = (newdevs.get("new_building_type", pd.Series()) == "新成屋").sum()
        takeaways.append(f"- **New Developments**: {cnt:,} projects ({pre} 預售屋, {new} 新成屋)")
    if not takeaways:
        takeaways.append("- No data available for this week.")
    lines.extend(takeaways)
    lines.append("")

    # Rental Market
    lines.append("## Rental Market")
    if not rentals.empty and "rent" in rentals.columns:
        valid = rentals[rentals["rent"].notna() & (rentals["rent"] > 0)]
        top5 = valid.groupby("city")["rent"].agg(["median", "count"]).sort_values("median", ascending=False).head(5)
        lines.append("")
        lines.append("| City | Median Rent | Listings |")
        lines.append("|------|-------------|----------|")
        for city, row in top5.iterrows():
            lines.append(f"| {city} | NT${row['median']:,.0f} | {int(row['count']):,} |")
        lines.append("")
        # Property type breakdown
        type_stats = valid.groupby("property_type")["rent"].agg(["median", "count"]).sort_values("count", ascending=False).head(5)
        lines.append("**By property type:**")
        lines.append("")
        lines.append("| Type | Median Rent | Listings |")
        lines.append("|------|-------------|----------|")
        for ptype, row in type_stats.iterrows():
            lines.append(f"| {ptype} | NT${row['median']:,.0f} | {int(row['count']):,} |")
    else:
        lines.append("No rental data available.")
    lines.append("")

    # Resale Market
    lines.append("## Resale Market")
    if not resales.empty and "total_price_wan" in resales.columns:
        valid = resales[resales["total_price_wan"].notna() & (resales["total_price_wan"] > 0)]
        top5 = valid.groupby("city").agg(
            median_price=("total_price_wan", "median"),
            median_unit=("unit_price_wan_per_ping", "median"),
            count=("total_price_wan", "size"),
        ).sort_values("median_price", ascending=False).head(5)
        lines.append("")
        lines.append("| City | Median Price (萬) | Median $/坪 (萬) | Listings |")
        lines.append("|------|-------------------|------------------|----------|")
        for city, row in top5.iterrows():
            up = f"{row['median_unit']:.1f}" if pd.notna(row['median_unit']) else "N/A"
            lines.append(f"| {city} | {row['median_price']:,.0f} | {up} | {int(row['count']):,} |")
    else:
        lines.append("No resale data available.")
    lines.append("")

    # New Developments
    lines.append("## New Developments")
    if not newdevs.empty:
        valid = newdevs[newdevs["unit_price_min_wan"].notna()]
        top5 = valid.groupby("city").agg(
            median_unit_lo=("unit_price_min_wan", "median"),
            median_unit_hi=("unit_price_max_wan", "median"),
            count=("listing_id", "size"),
        ).sort_values("median_unit_hi", ascending=False).head(5)
        lines.append("")
        lines.append("| City | Unit Price Range (萬/坪) | Projects |")
        lines.append("|------|-------------------------|----------|")
        for city, row in top5.iterrows():
            lo = f"{row['median_unit_lo']:.0f}" if pd.notna(row['median_unit_lo']) else "?"
            hi = f"{row['median_unit_hi']:.0f}" if pd.notna(row['median_unit_hi']) else "?"
            lines.append(f"| {city} | {lo}~{hi} | {int(row['count']):,} |")
    else:
        lines.append("No new development data available.")
    lines.append("")

    # District highlights (top 3 most expensive rental districts)
    lines.append("## District Highlights")
    if not rentals.empty and "rent" in rentals.columns:
        valid = rentals[rentals["rent"].notna() & (rentals["rent"] > 0)]
        dist_stats = valid.groupby(["city", "district"])["rent"].agg(["median", "count"])
        dist_stats = dist_stats[dist_stats["count"] >= 10].sort_values("median", ascending=False).head(5)
        if not dist_stats.empty:
            lines.append("")
            lines.append("**Most expensive rental districts (min 10 listings):**")
            lines.append("")
            for (city, dist), row in dist_stats.iterrows():
                lines.append(f"- {city} {dist}: NT${row['median']:,.0f}/月 ({int(row['count'])} listings)")
    lines.append("")

    # Data notes
    lines.append("## Data Notes")
    lines.append(f"- Data scraped from 591.com.tw on {today}")
    lines.append("- Prices are as listed; actual transaction prices may differ")
    lines.append("- 新建案 prices are developer asking prices and may be negotiable")
    lines.append("- days_on_market is calculated from posted_date where available")
    lines.append("- Confidence intervals require multi-week data accumulation")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    os.chdir(ROOT)
    config = load_config()
    ensure_dirs(config)
    logger = setup_logging("process_weekly", config["paths"]["log_dir"])

    raw_dir = config["paths"]["raw_dir"]
    proc_dir = config["paths"]["processed_dir"]
    out_dir = config["paths"]["output_dir"]
    charts_dir = config["paths"]["charts_dir"]

    # Load data
    logger.info("Loading rental data…")
    rentals = load_all_snapshots(raw_dir, "rentals")
    logger.info("Rental rows: %d", len(rentals))

    logger.info("Loading resale data…")
    resales = load_all_snapshots(raw_dir, "resales")
    logger.info("Resale rows: %d", len(resales))

    logger.info("Loading new development data…")
    newdevs = load_all_snapshots(raw_dir, "new_developments")
    logger.info("New development rows: %d", len(newdevs))

    # Compute summaries
    logger.info("Computing summaries…")
    rental_sum = rental_summary(rentals)
    resale_sum = resale_summary(resales)
    newdev_sum = newdev_summary(newdevs)

    # Save summaries
    if not rental_sum.empty:
        rental_sum.to_csv(Path(proc_dir) / "weekly_rental_summary.csv",
                          index=False, encoding="utf-8-sig")
        logger.info("Saved rental summary (%d rows)", len(rental_sum))

    if not resale_sum.empty:
        resale_sum.to_csv(Path(proc_dir) / "weekly_resale_summary.csv",
                          index=False, encoding="utf-8-sig")
        logger.info("Saved resale summary (%d rows)", len(resale_sum))

    if not newdev_sum.empty:
        newdev_sum.to_csv(Path(proc_dir) / "weekly_new_development_summary.csv",
                          index=False, encoding="utf-8-sig")
        logger.info("Saved new development summary (%d rows)", len(newdev_sum))

    # Generate charts
    logger.info("Generating charts…")
    chart_rental_median_by_city(rentals, charts_dir)
    chart_resale_median_by_city(resales, charts_dir)
    chart_newdev_unit_price_by_city(newdevs, charts_dir)

    # Weekly trend charts (only meaningful with multi-week data)
    if not rental_sum.empty and rental_sum["week"].nunique() > 1:
        chart_weekly_trend(
            rental_sum, "rent_p50",
            "租金中位數週趨勢 (Top 5 城市)", "租金中位數 (NT$)",
            "rental_median_trend.png", charts_dir,
        )
    if not resale_sum.empty and resale_sum["week"].nunique() > 1:
        chart_weekly_trend(
            resale_sum, "price_p50",
            "中古屋總價中位數週趨勢 (Top 5 城市)", "總價中位數 (萬)",
            "resale_median_trend.png", charts_dir,
        )

    # Generate markdown summary
    logger.info("Generating market summary…")
    md = generate_summary(rentals, resales, newdevs, rental_sum, resale_sum, newdev_sum)
    md_path = Path(out_dir) / "weekly_market_summary.md"
    md_path.write_text(md, encoding="utf-8")
    logger.info("Market summary saved to %s", md_path)

    logger.info("Weekly processing complete.")


if __name__ == "__main__":
    main()
