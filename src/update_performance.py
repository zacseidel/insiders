from __future__ import annotations

"""
Performance tracker: for every flagged insider buy, record the stock price
on the report date and compare it to the current price.

Uses the Polygon grouped-daily endpoint — one API call per date gives closing
prices for all US stocks, so cost = (unique report dates + 1) calls total.

Usage:
  python src/update_performance.py [--date YYYY-MM-DD]
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, ROOT, load_config, load_json, save_json, setup_logging, RateLimiter

log = setup_logging("update_performance")

GROUPED_CACHE_DIR = DATA_DIR / "cache" / "grouped"
DOCS_DIR = ROOT / "docs"
TEMPLATES_DIR = ROOT / "templates"
POLYGON_BASE = "https://api.polygon.io"


def fetch_grouped_daily(api_key: str, day: date, limiter: RateLimiter) -> dict[str, float]:
    """
    Return {TICKER: close_price} for all US stocks on `day`.
    Caches the result permanently (past dates never change).
    Falls back up to 3 previous days if `day` is not a trading day.
    """
    for delta in range(4):
        target = day - timedelta(days=delta)
        cache_path = GROUPED_CACHE_DIR / f"{target.isoformat()}.json"

        if cache_path.exists():
            data = load_json(cache_path)
            if data:  # non-empty means it was a trading day
                log.debug("Grouped daily cache hit: %s", target)
                return data
            elif delta < 3:
                continue  # empty file = non-trading day, try previous

        limiter.wait()
        url = f"{POLYGON_BASE}/v2/aggs/grouped/locale/us/market/stocks/{target.isoformat()}"
        try:
            resp = requests.get(url, params={"adjusted": "true", "apiKey": api_key}, timeout=30)
            if resp.status_code == 429:
                log.warning("Rate limited fetching grouped daily %s, sleeping 60s", target)
                time.sleep(60)
                resp = requests.get(url, params={"adjusted": "true", "apiKey": api_key}, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            log.warning("Failed to fetch grouped daily %s: %s", target, e)
            continue

        results = payload.get("results") or []
        if not results:
            # Market was closed this day — cache empty so we skip it next time
            GROUPED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text("{}")
            log.debug("No data for %s (non-trading day)", target)
            continue

        prices = {r["T"]: r["c"] for r in results if "T" in r and "c" in r}
        GROUPED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        save_json(cache_path, prices)
        log.info("Fetched grouped daily %s: %d tickers", target, len(prices))
        return prices

    log.warning("Could not find trading day data near %s", day)
    return {}


def most_recent_trading_day() -> date:
    """Return today or the most recent weekday (simple heuristic — grouped daily handles the rest)."""
    d = date.today()
    # Move back if weekend
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def collect_flagged_transactions() -> list[dict]:
    """Read all scored JSON files and return unique (report_date, ticker, dedup_key) entries."""
    scored_dir = DATA_DIR / "scored"
    entries = []
    seen_dedup = set()

    for path in sorted(scored_dir.glob("*.json")):
        try:
            data = load_json(path)
        except Exception:
            continue
        report_date = data.get("run_date") or path.stem
        for tx in data.get("transactions", []):
            key = tx.get("dedup_key", "")
            if key in seen_dedup:
                continue
            seen_dedup.add(key)
            ticker = (tx.get("issuer_ticker") or "").upper()
            if not ticker:
                continue
            entries.append({
                "report_date": report_date,
                "ticker": ticker,
                "company_name": tx.get("issuer_name", ticker),
                "insider_name": tx.get("insider_name", ""),
                "insider_title": tx.get("insider_title", ""),
                "buy_price": tx.get("price_per_share"),
                "shares": tx.get("shares"),
                "total_value": tx.get("total_value"),
                "score": tx.get("score", 0),
                "cluster_id": tx.get("cluster_id"),
                "dedup_key": key,
            })

    return entries


def run(current_date: date | None = None) -> None:
    cfg = load_config()
    api_key = os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        log.warning("POLYGON_API_KEY not set — skipping performance update")
        return

    limiter = RateLimiter(cfg["polygon"]["rate_limit_calls_per_min"])
    GROUPED_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    entries = collect_flagged_transactions()
    if not entries:
        log.info("No flagged transactions found")
        return
    log.info("Collected %d flagged transactions across %d report dates",
             len(entries),
             len({e["report_date"] for e in entries}))

    # Fetch grouped daily for each unique report date
    unique_report_dates = sorted({e["report_date"] for e in entries})
    price_map: dict[str, dict[str, float]] = {}
    for rd in unique_report_dates:
        log.info("Fetching prices for report date %s", rd)
        price_map[rd] = fetch_grouped_daily(api_key, date.fromisoformat(rd), limiter)

    # Fetch current prices
    today = current_date or most_recent_trading_day()
    log.info("Fetching current prices (%s)", today)
    # Don't use cache for today so it's always fresh — delete if exists
    today_cache = GROUPED_CACHE_DIR / f"{today.isoformat()}.json"
    if today_cache.exists():
        today_cache.unlink()
    current_prices = fetch_grouped_daily(api_key, today, limiter)

    # SPY is an ETF but is included in the US stocks market grouped daily response —
    # no separate endpoint needed; we extract it from data already fetched.
    spy_current = current_prices.get("SPY")
    if not spy_current:
        log.warning("SPY not found in current prices — benchmark comparison unavailable")

    # Build performance records
    records = []
    for e in entries:
        ticker = e["ticker"]
        rd = e["report_date"]
        price_at_report = price_map.get(rd, {}).get(ticker)
        current_price = current_prices.get(ticker)

        change_pct = None
        change_abs = None
        if price_at_report and current_price and price_at_report > 0:
            change_abs = current_price - price_at_report
            change_pct = (change_abs / price_at_report) * 100

        # SPY over the same period (report date → today)
        spy_at_report = price_map.get(rd, {}).get("SPY")
        spy_change_pct = None
        if spy_at_report and spy_current and spy_at_report > 0:
            spy_change_pct = ((spy_current - spy_at_report) / spy_at_report) * 100

        alpha = None
        if change_pct is not None and spy_change_pct is not None:
            alpha = change_pct - spy_change_pct

        records.append({
            **e,
            "price_at_report": price_at_report,
            "current_price": current_price,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "change_abs": round(change_abs, 2) if change_abs is not None else None,
            "spy_at_report": spy_at_report,
            "spy_current": spy_current,
            "spy_change_pct": round(spy_change_pct, 2) if spy_change_pct is not None else None,
            "alpha": round(alpha, 2) if alpha is not None else None,
        })

    save_json(DATA_DIR / "performance.json", {
        "last_updated": today.isoformat(),
        "current_date": today.isoformat(),
        "records": records,
    })
    log.info("Saved performance data: %d records", len(records))

    _render(records, today)


def _fmt_value(v: float | None) -> str:
    if v is None:
        return "N/A"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,.0f}"


def _render(records: list[dict], current_date: date) -> None:
    from jinja2 import Environment, FileSystemLoader

    # Group by report_date, sort by score desc within each group
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[r["report_date"]].append(r)

    grouped = []
    for rd in sorted(groups.keys(), reverse=True):
        txs = sorted(groups[rd], key=lambda x: (x["score"] or 0), reverse=True)
        # Summary stats for this date
        with_data = [t for t in txs if t["change_pct"] is not None]
        avg_change = (sum(t["change_pct"] for t in with_data) / len(with_data)) if with_data else None
        winners = sum(1 for t in with_data if t["change_pct"] > 0)
        with_alpha = [t for t in txs if t["alpha"] is not None]
        avg_alpha = (sum(t["alpha"] for t in with_alpha) / len(with_alpha)) if with_alpha else None
        grouped.append({
            "report_date": rd,
            "transactions": txs,
            "count": len(txs),
            "avg_change_pct": round(avg_change, 1) if avg_change is not None else None,
            "avg_alpha": round(avg_alpha, 1) if avg_alpha is not None else None,
            "winners": winners,
            "with_data": len(with_data),
        })

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    tmpl = env.get_template("performance.html")
    html = tmpl.render(
        grouped=grouped,
        current_date=current_date.isoformat(),
        current_date_fmt=current_date.strftime("%b %d, %Y"),
        fmt_value=_fmt_value,
    )
    out = DOCS_DIR / "performance.html"
    out.write_text(html, encoding="utf-8")
    log.info("Wrote performance page → %s", out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Use this date as 'today' for current prices (YYYY-MM-DD)")
    args = parser.parse_args()
    current = date.fromisoformat(args.date) if args.date else None
    run(current)


if __name__ == "__main__":
    main()
