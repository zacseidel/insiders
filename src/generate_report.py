from __future__ import annotations

"""
Stage 5: Render HTML reports from scored data.

Usage:
  python src/generate_report.py [--date YYYY-MM-DD]
"""
import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, ROOT, load_config, load_json, save_json, setup_logging

log = setup_logging("generate_report")

DOCS_DIR = ROOT / "docs"
REPORTS_DIR = DOCS_DIR / "reports"
TEMPLATES_DIR = ROOT / "templates"

GITHUB_USER = "zacseidel"


def fmt_value(v: float | None) -> str:
    if v is None:
        return "N/A"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,.0f}"


def fmt_mktcap(v: float | None) -> str:
    if v is None:
        return ""
    if v >= 1_000_000_000_000:
        return f"${v / 1_000_000_000_000:.2f}T"
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.0f}M"
    return f"${v:,.0f}"


def fmt_shares(s: float | None) -> str:
    if s is None:
        return "—"
    return f"{s:,.0f}"


def fmt_price(p: float | None) -> str:
    if p is None:
        return "N/A"
    return f"{p:,.2f}"


def prepare_tx(tx: dict, charts: dict) -> dict:
    ticker = (tx.get("issuer_ticker") or "").upper()
    info = tx.get("company_info", {})
    return {
        **tx,
        "shares_fmt": fmt_shares(tx.get("shares")),
        "price_fmt": fmt_price(tx.get("price_per_share")),
        "total_value_fmt": fmt_value(tx.get("total_value")),
        "company_info": {
            **info,
            "market_cap_fmt": fmt_mktcap(info.get("market_cap")),
        },
        "chart_b64": charts.get(ticker, ""),
    }


def build_cluster_cards(cluster_companies: dict, charts: dict) -> list:
    cards = []
    for cik, cluster in cluster_companies.items():
        txs = cluster.get("transactions", [])
        if not txs:
            continue
        ticker = (txs[0].get("issuer_ticker") or "").upper()
        company_name = txs[0].get("issuer_name", ticker)
        info = txs[0].get("company_info", {})
        combined_value = sum(t.get("total_value") or 0 for t in txs)
        combined_score = sum(t.get("score", 0) for t in txs)
        cards.append({
            "cluster_id": cluster.get("cluster_id"),
            "ticker": ticker,
            "company_name": company_name,
            "company_info": {
                **info,
                "market_cap_fmt": fmt_mktcap(info.get("market_cap")),
            },
            "combined_value": combined_value,
            "combined_value_fmt": fmt_value(combined_value),
            "combined_score": combined_score,
            "chart_b64": charts.get(ticker, ""),
            "transactions": [prepare_tx(t, charts) for t in sorted(txs, key=lambda x: x.get("total_value") or 0, reverse=True)],
        })
    return sorted(cards, key=lambda c: c["combined_value"], reverse=True)


def run(run_date: date) -> None:
    from jinja2 import Environment, FileSystemLoader

    cfg = load_config()
    top_n = cfg["report"]["top_individual_detail_count"]

    scored_path = DATA_DIR / "scored" / f"{run_date.isoformat()}.json"
    if not scored_path.exists():
        log.error("Scored file not found: %s", scored_path)
        return

    data = load_json(scored_path)
    transactions = data.get("transactions", [])
    cluster_companies = data.get("cluster_companies", {})

    charts_path = DATA_DIR / "charts" / f"{run_date.isoformat()}.json"
    charts: dict[str, str] = {}
    if charts_path.exists():
        charts_data = load_json(charts_path)
        charts = charts_data.get("charts", {})
    log.info("Loaded %d charts", len(charts))

    cluster_ciks = set(cluster_companies.keys())
    non_cluster_txs = [t for t in transactions if t.get("issuer_cik") not in cluster_ciks]
    top_individuals = [prepare_tx(t, charts) for t in non_cluster_txs[:top_n]]
    all_txs_rendered = [prepare_tx(t, charts) for t in transactions]

    window_start = date.fromisoformat(data.get("window_start", ""))
    window_end = date.fromisoformat(data.get("window_end", ""))
    window_label = f"{window_start.strftime('%b %d')} – {window_end.strftime('%b %d, %Y')}"

    total_value = sum(t.get("total_value") or 0 for t in transactions)
    unique_companies = len({t.get("issuer_cik") for t in transactions if t.get("issuer_cik")})

    stats = {
        "total_count": len(transactions),
        "total_value_fmt": fmt_value(total_value),
        "company_count": unique_companies,
        "cluster_count": len(cluster_companies),
    }

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    report_tmpl = env.get_template("report.html")

    cluster_cards = build_cluster_cards(cluster_companies, charts)

    report_html = report_tmpl.render(
        run_date=run_date.isoformat(),
        run_date_fmt=run_date.strftime("%a %b %d, %Y"),
        window_label=window_label,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        stats=stats,
        clusters=cluster_cards,
        top_individuals=top_individuals,
        all_transactions=all_txs_rendered,
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{run_date.isoformat()}.html"
    report_path.write_text(report_html, encoding="utf-8")
    log.info("Wrote report to %s", report_path)

    _rebuild_index(env)


def _collect_archive_entries() -> list[dict]:
    entries = []
    for html_file in sorted(REPORTS_DIR.glob("*.html"), reverse=True):
        run_date_str = html_file.stem
        try:
            d = date.fromisoformat(run_date_str)
        except ValueError:
            continue

        scored_path = DATA_DIR / "scored" / f"{run_date_str}.json"
        total_count = 0
        total_value_fmt = "N/A"
        window_label = ""
        top_buy = ""

        if scored_path.exists():
            try:
                data = load_json(scored_path)
                txs = data.get("transactions", [])
                total_count = len(txs)
                total_value = sum(t.get("total_value") or 0 for t in txs)
                total_value_fmt = fmt_value(total_value)
                ws = data.get("window_start", "")
                we = data.get("window_end", "")
                if ws and we:
                    ws_d = date.fromisoformat(ws)
                    we_d = date.fromisoformat(we)
                    window_label = f"{ws_d.strftime('%b %d')} – {we_d.strftime('%b %d')}"
                if txs:
                    top = txs[0]
                    ticker = top.get("issuer_ticker", "?")
                    title = top.get("insider_title", "Insider")
                    val = fmt_value(top.get("total_value"))
                    top_buy = f"{title} @ {ticker}  {val}"
            except Exception as e:
                log.warning("Could not read scored data for %s: %s", run_date_str, e)

        entries.append({
            "run_date": run_date_str,
            "filename": html_file.name,
            "window_label": window_label,
            "total_count": total_count,
            "total_value_fmt": total_value_fmt,
            "top_buy": top_buy,
        })

    return entries


def _rebuild_index(env) -> None:
    entries = _collect_archive_entries()
    index_tmpl = env.get_template("index.html")
    index_html = index_tmpl.render(reports=entries, github_user=GITHUB_USER)
    index_path = DOCS_DIR / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    log.info("Rebuilt index with %d entries → %s", len(entries), index_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Run date YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    run(run_date)


if __name__ == "__main__":
    main()
