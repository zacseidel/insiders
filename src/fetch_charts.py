from __future__ import annotations

"""
Stage 3: Generate price charts for top-ranked companies.

Usage:
  python src/fetch_charts.py [--date YYYY-MM-DD]
"""
import argparse
import base64
import io
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, PolygonClient, load_config, load_json, save_json, setup_logging

log = setup_logging("fetch_charts")

CHARTS_DIR = DATA_DIR / "charts"

STYLE = {
    "price_color": "#2196F3",
    "sma50_color": "#FF9800",
    "sma200_color": "#9C27B0",
    "volume_color": "#BDBDBD",
    "buy_marker_color": "#F44336",
    "bg_color": "#FFFFFF",
    "grid_color": "#F0F0F0",
    "text_color": "#212121",
    "fig_size": (10, 4.5),
    "dpi": 150,
}


def sma(closes: list[float], n: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        result[i] = sum(closes[i - n + 1 : i + 1]) / n
    return result


def make_chart(
    ticker: str,
    company_name: str,
    bars: list[dict],
    buy_dates: list[str],
    buy_annotations: list[str],
) -> str | None:
    """Return base64-encoded PNG or None on failure."""
    if len(bars) < 20:
        return None

    import matplotlib.dates as mdates
    from datetime import datetime as dt

    dates = [dt.utcfromtimestamp(b["t"] / 1000) for b in bars]
    closes = [b["c"] for b in bars]
    volumes = [b["v"] for b in bars]

    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)

    current = closes[-1]
    title = f"{ticker} — {company_name}    ${current:,.2f}"

    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1,
        figsize=STYLE["fig_size"],
        gridspec_kw={"height_ratios": [4, 1], "hspace": 0.08},
        facecolor=STYLE["bg_color"],
    )

    ax_price.set_facecolor(STYLE["bg_color"])
    ax_vol.set_facecolor(STYLE["bg_color"])

    ax_price.plot(dates, closes, color=STYLE["price_color"], linewidth=1.5, zorder=3)

    valid50 = [(d, v) for d, v in zip(dates, sma50) if v is not None]
    if valid50:
        d50, v50 = zip(*valid50)
        ax_price.plot(d50, v50, color=STYLE["sma50_color"], linewidth=1.0, linestyle="--", alpha=0.8, label="50d SMA")

    valid200 = [(d, v) for d, v in zip(dates, sma200) if v is not None]
    if valid200:
        d200, v200 = zip(*valid200)
        ax_price.plot(d200, v200, color=STYLE["sma200_color"], linewidth=1.0, linestyle="--", alpha=0.8, label="200d SMA")

    buy_dt_set = set(buy_dates)
    annotated = 0
    for buy_date_str in buy_dates:
        try:
            buy_dt = dt.strptime(buy_date_str, "%Y-%m-%d")
        except ValueError:
            continue
        ax_price.axvline(buy_dt, color=STYLE["buy_marker_color"], linewidth=1.2, linestyle="--", alpha=0.9, zorder=4)

    if buy_annotations:
        annotation_text = "\n".join(buy_annotations[:3])
        ax_price.text(
            0.01, 0.97, annotation_text,
            transform=ax_price.transAxes,
            fontsize=7, verticalalignment="top",
            color=STYLE["buy_marker_color"],
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.7, edgecolor=STYLE["buy_marker_color"]),
        )

    ax_price.set_title(title, fontsize=11, color=STYLE["text_color"], pad=8, loc="left")
    ax_price.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax_price.grid(True, color=STYLE["grid_color"], linewidth=0.5)
    ax_price.spines["top"].set_visible(False)
    ax_price.spines["right"].set_visible(False)
    ax_price.tick_params(colors=STYLE["text_color"], labelsize=8)
    ax_price.xaxis.set_ticklabels([])

    if valid50 or valid200:
        ax_price.legend(fontsize=8, loc="upper left", framealpha=0.7)

    ax_vol.bar(dates, volumes, color=STYLE["volume_color"], width=1.0, alpha=0.6)
    ax_vol.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x/1e6:.1f}M" if x >= 1e6 else f"{x/1e3:.0f}K"))
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax_vol.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax_vol.grid(True, axis="y", color=STYLE["grid_color"], linewidth=0.5)
    ax_vol.spines["top"].set_visible(False)
    ax_vol.spines["right"].set_visible(False)
    ax_vol.tick_params(colors=STYLE["text_color"], labelsize=7)
    plt.setp(ax_vol.xaxis.get_majorticklabels(), rotation=0)

    fig.patch.set_facecolor(STYLE["bg_color"])
    plt.tight_layout(pad=0.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=STYLE["dpi"], bbox_inches="tight", facecolor=STYLE["bg_color"])
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def run(run_date: date) -> None:
    cfg = load_config()
    poly_cfg = cfg["polygon"]
    lookback = poly_cfg["chart_lookback_days"]
    top_n = cfg["report"]["top_individual_detail_count"]

    enriched_path = DATA_DIR / "enriched" / f"{run_date.isoformat()}.json"
    if not enriched_path.exists():
        log.error("Enriched file not found: %s", enriched_path)
        return

    data = load_json(enriched_path)
    transactions = data.get("transactions", [])

    # Sort by preliminary score to find tickers that will get full cards
    scored_rough = sorted(transactions, key=lambda t: t.get("total_value") or 0, reverse=True)

    # Detect clusters
    from collections import defaultdict
    insiders_by_cik: dict[str, set] = defaultdict(set)
    for tx in transactions:
        cik = tx.get("issuer_cik", "")
        insider = tx.get("insider_cik", "")
        if cik and insider:
            insiders_by_cik[cik].add(insider)
    cluster_ciks = {cik for cik, ins in insiders_by_cik.items() if len(ins) >= 2}

    # Determine which tickers need charts
    tickers_needing_charts: set[str] = set()
    for tx in transactions:
        if tx.get("issuer_cik") in cluster_ciks:
            t = (tx.get("issuer_ticker") or "").upper()
            if t:
                tickers_needing_charts.add(t)
    for tx in scored_rough[:top_n]:
        t = (tx.get("issuer_ticker") or "").upper()
        if t:
            tickers_needing_charts.add(t)

    log.info("Generating charts for %d tickers", len(tickers_needing_charts))

    # Group buy events by ticker for annotation
    buys_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for tx in transactions:
        t = (tx.get("issuer_ticker") or "").upper()
        if t in tickers_needing_charts:
            buys_by_ticker[t].append(tx)

    api_key = os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        log.warning("POLYGON_API_KEY not set — skipping chart generation")
        return

    polygon = PolygonClient(api_key, poly_cfg)
    charts: dict[str, str] = {}

    chart_from = (run_date - __import__("datetime").timedelta(days=lookback)).isoformat()
    chart_to = run_date.isoformat()

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    for i, ticker in enumerate(sorted(tickers_needing_charts), 1):
        log.info("[%d/%d] Chart: %s", i, len(tickers_needing_charts), ticker)

        cache_path = CHARTS_DIR / f"{ticker}-{run_date.isoformat()}.png"

        if cache_path.exists():
            log.info("  Cache hit: %s", cache_path.name)
            b64 = base64.b64encode(cache_path.read_bytes()).decode("ascii")
            charts[ticker] = b64
            continue

        bars = polygon.aggregates(ticker, chart_from, chart_to)
        if not bars:
            log.warning("  No price data for %s", ticker)
            continue

        ticker_txs = buys_by_ticker.get(ticker, [])
        buy_dates = list({tx.get("transaction_date", "") for tx in ticker_txs if tx.get("transaction_date")})
        buy_annotations = []
        for tx in sorted(ticker_txs, key=lambda t: t.get("total_value") or 0, reverse=True)[:3]:
            name = tx.get("insider_name", "Unknown")
            shares = tx.get("shares", 0)
            price = tx.get("price_per_share")
            if price:
                buy_annotations.append(f"{name}: {shares:,.0f}sh @ ${price:,.2f}")
            else:
                buy_annotations.append(f"{name}: {shares:,.0f}sh")

        company_info = ticker_txs[0].get("company_info", {}) if ticker_txs else {}
        company_name = ticker_txs[0].get("issuer_name", ticker) if ticker_txs else ticker

        try:
            b64 = make_chart(ticker, company_name, bars, buy_dates, buy_annotations)
        except Exception as e:
            log.warning("  Chart generation failed for %s: %s", ticker, e)
            continue

        if b64:
            cache_path.write_bytes(base64.b64decode(b64))
            charts[ticker] = b64
            log.info("  Saved chart for %s (%d bars)", ticker, len(bars))

    out_path = DATA_DIR / "charts" / f"{run_date.isoformat()}.json"
    save_json(out_path, {"run_date": run_date.isoformat(), "charts": charts})
    log.info("Saved %d charts to %s", len(charts), out_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Run date YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    run(run_date)


if __name__ == "__main__":
    main()
