from __future__ import annotations

"""
Stage 2: Resolve tickers and enrich transactions with Polygon company data.

Usage:
  python src/enrich.py [--date YYYY-MM-DD]
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, PolygonClient, load_config, load_json, save_json, setup_logging, get_edgar_user_agent

log = setup_logging("enrich")

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_TICKERS_PATH = DATA_DIR / "company_tickers.json"


def load_cik_map() -> dict[str, str]:
    """Return {cik_str → ticker} map, refreshing if >7 days old."""
    if COMPANY_TICKERS_PATH.exists():
        age = (datetime.now() - datetime.fromtimestamp(COMPANY_TICKERS_PATH.stat().st_mtime)).days
        if age < 7:
            data = load_json(COMPANY_TICKERS_PATH)
            return {str(int(v["cik_str"])): v["ticker"] for v in data.values()}

    log.info("Refreshing company_tickers.json from SEC")
    resp = requests.get(COMPANY_TICKERS_URL, headers={"User-Agent": get_edgar_user_agent()}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    save_json(COMPANY_TICKERS_PATH, data)
    return {str(int(v["cik_str"])): v["ticker"] for v in data.values()}


def _year_ago(from_date: date) -> str:
    return (from_date - timedelta(days=365)).isoformat()


def enrich_ticker(ticker: str, polygon: PolygonClient, run_date: date) -> dict:
    details = polygon.ticker_details(ticker) or {}
    news = polygon.ticker_news(ticker)

    aggs = polygon.aggregates(ticker, _year_ago(run_date), run_date.isoformat())

    current_price = None
    week_52_high = None
    week_52_low = None
    if aggs:
        current_price = aggs[-1].get("c")
        highs = [r.get("h", 0) for r in aggs]
        lows = [r.get("l", float("inf")) for r in aggs]
        week_52_high = max(highs) if highs else None
        week_52_low = min(lows) if lows else None

    return {
        "description": (details.get("description") or "")[:500],
        "sic_description": details.get("sic_description"),
        "market_cap": details.get("market_cap"),
        "total_employees": details.get("total_employees"),
        "homepage_url": details.get("homepage_url"),
        "icon_url": (details.get("branding") or {}).get("icon_url"),
        "current_price": current_price,
        "week_52_high": week_52_high,
        "week_52_low": week_52_low,
        "recent_news": [
            {
                "title": n.get("title"),
                "article_url": n.get("article_url"),
                "publisher": n.get("publisher", {}).get("name"),
                "published_utc": n.get("published_utc"),
            }
            for n in news
        ],
    }


_INVALID_TICKERS = {"NONE", "NA", "N/A", "TBD", "TBA", "-", ""}


def _valid_ticker(t: str) -> bool:
    """Reject placeholder values that slip through the CIK mapping."""
    return bool(t) and t not in _INVALID_TICKERS and t.isalpha() and len(t) <= 5


def preliminary_score(tx: dict) -> float:
    return tx.get("total_value") or 0.0


def run(run_date: date) -> None:
    cfg = load_config()
    poly_cfg = cfg["polygon"]
    max_tickers = poly_cfg["max_enrichment_tickers"]

    raw_path = DATA_DIR / "raw" / f"{run_date.isoformat()}.json"
    if not raw_path.exists():
        log.error("Raw file not found: %s", raw_path)
        return

    raw_data = load_json(raw_path)
    transactions = raw_data.get("transactions", [])
    log.info("Enriching %d transactions", len(transactions))

    cik_map = load_cik_map()

    for tx in transactions:
        if not tx.get("issuer_ticker"):
            cik = str(int(tx.get("issuer_cik", "0") or "0"))
            tx["issuer_ticker"] = cik_map.get(cik, "")

    before = len(transactions)
    transactions = [tx for tx in transactions if _valid_ticker((tx.get("issuer_ticker") or "").upper())]
    dropped = before - len(transactions)
    if dropped:
        log.info("Dropped %d transactions with unresolvable tickers (likely funds/non-public)", dropped)

    sorted_txs = sorted(transactions, key=preliminary_score, reverse=True)
    unique_tickers: list[str] = []
    seen: set[str] = set()
    for tx in sorted_txs:
        t = tx.get("issuer_ticker", "").upper()
        if t and t not in seen and _valid_ticker(t):
            seen.add(t)
            unique_tickers.append(t)

    tickers_to_enrich = unique_tickers[:max_tickers]
    log.info("Enriching %d unique tickers (cap: %d)", len(tickers_to_enrich), max_tickers)

    api_key = os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        log.warning("POLYGON_API_KEY not set — skipping Polygon enrichment")
        company_info: dict[str, dict] = {}
    else:
        polygon = PolygonClient(api_key, poly_cfg)
        company_info = {}
        for i, ticker in enumerate(tickers_to_enrich, 1):
            log.info("[%d/%d] Enriching %s", i, len(tickers_to_enrich), ticker)
            company_info[ticker] = enrich_ticker(ticker, polygon, run_date)

    enriched_txs = []
    for tx in transactions:
        ticker = (tx.get("issuer_ticker") or "").upper()
        info = company_info.get(ticker, {})
        enriched_txs.append({**tx, "company_info": info})

    output = {
        **{k: v for k, v in raw_data.items() if k != "transactions"},
        "transactions": enriched_txs,
    }
    out_path = DATA_DIR / "enriched" / f"{run_date.isoformat()}.json"
    save_json(out_path, output)
    log.info("Saved enriched data to %s", out_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Run date YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    run(run_date)


if __name__ == "__main__":
    main()
