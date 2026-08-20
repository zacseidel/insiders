from __future__ import annotations

"""Build the four-strategy insider-buying performance report.

Each report creates an equal-dollar cohort of up to five securities. Securities
are eligible on their first appearance, then become eligible again only after a
previous position in that strategy has reached its scheduled exit date.

Usage:
  python src/update_performance.py [--date YYYY-MM-DD]
"""

import argparse
import calendar
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, ROOT, RateLimiter, load_config, load_json, save_json, setup_logging


log = setup_logging("update_performance")

DOCS_DIR = ROOT / "docs"
TEMPLATES_DIR = ROOT / "templates"
PRICE_CACHE_DIR = DATA_DIR / "cache" / "performance"
POLYGON_BASE = "https://api.polygon.io"
BENCHMARK_TICKER = "SPY"
MIN_KELLY_TRADES = 5

STRATEGIES = (
    {
        "id": "size_1m",
        "name": "Top 5 by Size · 1 Month",
        "rank_by": "size",
        "holding_months": 1,
    },
    {
        "id": "score_1m",
        "name": "Top 5 by Score · 1 Month",
        "rank_by": "score",
        "holding_months": 1,
    },
    {
        "id": "size_3m",
        "name": "Top 5 by Size · 3 Months",
        "rank_by": "size",
        "holding_months": 3,
    },
    {
        "id": "score_3m",
        "name": "Top 5 by Score · 3 Months",
        "rank_by": "score",
        "holding_months": 3,
    },
    {
        "id": "size_6m",
        "name": "Top 5 by Size · 6 Months",
        "rank_by": "size",
        "holding_months": 6,
    },
    {
        "id": "score_6m",
        "name": "Top 5 by Score · 6 Months",
        "rank_by": "score",
        "holding_months": 6,
    },
)

WINDOWS = (
    {"id": "3m", "name": "Last 3 Months", "months": 3},
    {"id": "12m", "name": "Last 12 Months", "months": 12},
)


def add_months(day: date, months: int) -> date:
    """Add calendar months, clamping month-end dates when necessary."""
    month_index = day.year * 12 + day.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def last_completed_market_candidate(run_date: date) -> date:
    """Return a conservative completed-session cutoff for a pre-market run."""
    candidate = run_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def load_scored_reports() -> list[dict]:
    reports = []
    for path in sorted((DATA_DIR / "scored").glob("*.json")):
        try:
            payload = load_json(path)
            report_date = date.fromisoformat(payload.get("run_date") or path.stem)
        except (OSError, ValueError, TypeError):
            log.warning("Skipping malformed scored report: %s", path)
            continue
        reports.append({
            "report_date": report_date,
            "transactions": payload.get("transactions", []),
        })
    return reports


def aggregate_securities(transactions: Iterable[dict]) -> dict[str, dict]:
    """Collapse report transactions to security-level ranking candidates."""
    securities: dict[str, dict] = {}
    for tx in transactions:
        ticker = (tx.get("issuer_ticker") or "").upper()
        if not ticker:
            continue
        candidate = securities.setdefault(ticker, {
            "ticker": ticker,
            "company_name": tx.get("issuer_name") or ticker,
            "combined_value": 0.0,
            "combined_score": 0.0,
            "transaction_count": 0,
        })
        candidate["combined_value"] += tx.get("total_value") or 0.0
        candidate["combined_score"] += tx.get("score") or 0.0
        candidate["transaction_count"] += 1
    return securities


def build_strategy_positions(reports: list[dict], strategy: dict) -> list[dict]:
    """Select report cohorts for one strategy without using future prices."""
    seen_tickers: set[str] = set()
    prior_exit: dict[str, date] = {}
    positions = []

    for report in sorted(reports, key=lambda item: item["report_date"]):
        report_date = report["report_date"]
        securities = aggregate_securities(report["transactions"])

        eligible = []
        for ticker, candidate in securities.items():
            first_appearance = ticker not in seen_tickers
            repeat_after_exit = ticker in prior_exit and report_date >= prior_exit[ticker]
            if first_appearance or repeat_after_exit:
                eligible.append(candidate)

        rank_field = "combined_value" if strategy["rank_by"] == "size" else "combined_score"
        eligible.sort(
            key=lambda item: (item[rank_field], item["combined_value"], item["ticker"]),
            reverse=True,
        )

        for rank, candidate in enumerate(eligible[:5], start=1):
            ticker = candidate["ticker"]
            target_exit = add_months(report_date, strategy["holding_months"])
            prior_exit[ticker] = target_exit
            positions.append({
                "id": f"{strategy['id']}:{report_date.isoformat()}:{ticker}",
                "strategy_id": strategy["id"],
                "ticker": ticker,
                "company_name": candidate["company_name"],
                "report_date": report_date.isoformat(),
                "target_exit_date": target_exit.isoformat(),
                "rank": rank,
                "rank_metric": strategy["rank_by"],
                "rank_value": candidate[rank_field],
                "combined_value": candidate["combined_value"],
                "combined_score": candidate["combined_score"],
                "transaction_count": candidate["transaction_count"],
            })

        # First appearance is determined from the full report, not just its top five.
        seen_tickers.update(securities)

    return positions


def _bar_date(bar: dict) -> date | None:
    try:
        return datetime.fromtimestamp(bar["t"] / 1000, timezone.utc).date()
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _merge_bars(*groups: Iterable[dict]) -> list[dict]:
    by_date = {}
    for bars in groups:
        for bar in bars:
            day = _bar_date(bar)
            if day is not None:
                by_date[day] = bar
    return [by_date[day] for day in sorted(by_date)]


def _cache_path(ticker: str) -> Path:
    safe_ticker = ticker.replace("/", "-")
    return PRICE_CACHE_DIR / f"{safe_ticker}.json"


def load_cached_bars(ticker: str) -> list[dict]:
    path = _cache_path(ticker)
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except (OSError, ValueError):
        log.warning("Ignoring malformed price cache: %s", path)
        return []
    return payload.get("bars", []) if isinstance(payload, dict) else []


def _request_bars(
    ticker: str,
    start: date,
    end: date,
    api_key: str,
    limiter: RateLimiter,
    max_retries: int,
) -> list[dict]:
    if start > end:
        return []
    url = (
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/"
        f"{start.isoformat()}/{end.isoformat()}"
    )
    for attempt in range(max_retries):
        limiter.wait()
        try:
            response = requests.get(
                url,
                params={
                    "adjusted": "true",
                    "sort": "asc",
                    "limit": 50000,
                    "apiKey": api_key,
                },
                timeout=30,
            )
            if response.status_code == 429:
                log.warning("Polygon rate limit for %s (attempt %d)", ticker, attempt + 1)
                continue
            response.raise_for_status()
            return response.json().get("results") or []
        except Exception as exc:
            safe_error = str(exc).replace(api_key, "***")
            log.warning("Price history failed for %s (attempt %d): %s", ticker, attempt + 1, safe_error)
    return []


def get_price_history(
    ticker: str,
    start: date,
    end: date,
    api_key: str,
    limiter: RateLimiter,
    max_retries: int,
) -> list[dict]:
    """Load cached adjusted bars and fetch only missing leading/trailing ranges."""
    cached = _merge_bars(load_cached_bars(ticker))
    fetched: list[dict] = []

    if api_key:
        cached_dates = [day for day in (_bar_date(bar) for bar in cached) if day]
        if not cached_dates:
            fetched.extend(_request_bars(ticker, start, end, api_key, limiter, max_retries))
        else:
            earliest, latest = min(cached_dates), max(cached_dates)
            if start < earliest:
                fetched.extend(_request_bars(
                    ticker, start, earliest - timedelta(days=1), api_key, limiter, max_retries
                ))
            if latest < end:
                fetched.extend(_request_bars(
                    ticker, latest + timedelta(days=1), end, api_key, limiter, max_retries
                ))

    merged = _merge_bars(cached, fetched)
    if fetched:
        PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        save_json(_cache_path(ticker), {
            "ticker": ticker,
            "adjusted": True,
            "bars": merged,
        })
    return merged


def index_bars(bars: Iterable[dict], through: date) -> dict[date, dict]:
    indexed = {}
    for bar in bars:
        day = _bar_date(bar)
        if day is not None and day <= through:
            indexed[day] = bar
    return indexed


def _first_bar(indexed: dict[date, dict], predicate) -> tuple[date, dict] | None:
    for day in sorted(indexed):
        if predicate(day):
            return day, indexed[day]
    return None


def resolve_position(
    position: dict,
    stock_bars: Iterable[dict],
    benchmark_bars: Iterable[dict],
    as_of: date,
) -> dict:
    """Attach execution prices and matched-benchmark performance to a position."""
    report_date = date.fromisoformat(position["report_date"])
    target_exit = date.fromisoformat(position["target_exit_date"])
    stocks = index_bars(stock_bars, as_of)
    benchmark = index_bars(benchmark_bars, as_of)

    entry = _first_bar(stocks, lambda day: day > report_date and day in benchmark)
    result = {
        **position,
        "status": "pending",
        "entry_date": None,
        "entry_price": None,
        "valuation_date": None,
        "exit_price": None,
        "return_pct": None,
        "benchmark_return_pct": None,
        "excess_return_pct": None,
    }
    if not entry:
        result["status_reason"] = "Entry-session price not available"
        return result

    entry_date, entry_bar = entry
    entry_price = entry_bar.get("o")
    benchmark_entry = benchmark[entry_date].get("o")
    result.update({
        "entry_date": entry_date.isoformat(),
        "entry_price": entry_price,
    })
    if not entry_price or not benchmark_entry:
        result["status_reason"] = "Entry open not available"
        return result

    should_be_closed = target_exit <= as_of
    valuation = None
    if should_be_closed:
        valuation = _first_bar(stocks, lambda day: day >= target_exit and day in benchmark)
        if not valuation:
            result["status"] = "unavailable"
            result["status_reason"] = "Exit-session price not available"
            return result
        result["status"] = "closed"
    else:
        common_dates = sorted(set(stocks) & set(benchmark))
        if common_dates:
            valuation_date = common_dates[-1]
            valuation = valuation_date, stocks[valuation_date]
        if not valuation or valuation[0] < entry_date:
            result["status_reason"] = "Current mark not available"
            return result
        result["status"] = "open"

    valuation_date, valuation_bar = valuation
    exit_price = valuation_bar.get("c")
    benchmark_exit = benchmark[valuation_date].get("c")
    if not exit_price or not benchmark_exit:
        result["status"] = "unavailable"
        result["status_reason"] = "Closing price not available"
        return result

    security_return = ((exit_price / entry_price) - 1.0) * 100
    benchmark_return = ((benchmark_exit / benchmark_entry) - 1.0) * 100
    result.update({
        "valuation_date": valuation_date.isoformat(),
        "exit_price": exit_price,
        "return_pct": round(security_return, 4),
        "benchmark_return_pct": round(benchmark_return, 4),
        "excess_return_pct": round(security_return - benchmark_return, 4),
    })
    return result


def _mean_cohort_return(positions: list[dict], field: str) -> float | None:
    cohorts: dict[str, list[float]] = defaultdict(list)
    for position in positions:
        value = position.get(field)
        if value is not None:
            cohorts[position["report_date"]].append(value)
    cohort_returns = [statistics.mean(values) for values in cohorts.values() if values]
    return statistics.mean(cohort_returns) if cohort_returns else None


def calculate_sharpe(
    positions: list[dict],
    histories: dict[str, list[dict]],
) -> tuple[float | None, int]:
    """Annualized 0%-risk-free Sharpe from equal-weight daily cohort returns."""
    cohort_daily: dict[tuple[str, date], list[float]] = defaultdict(list)

    for position in positions:
        if not position.get("entry_date") or not position.get("valuation_date"):
            continue
        entry_date = date.fromisoformat(position["entry_date"])
        end_date = date.fromisoformat(position["valuation_date"])
        bars = index_bars(histories.get(position["ticker"], []), end_date)
        active_days = [day for day in sorted(bars) if entry_date <= day <= end_date]
        previous = position.get("entry_price")
        for day in active_days:
            close = bars[day].get("c")
            if previous and close:
                cohort_daily[(position["report_date"], day)].append((close / previous) - 1.0)
                previous = close

    strategy_daily: dict[date, list[float]] = defaultdict(list)
    for (_, day), returns in cohort_daily.items():
        if returns:
            strategy_daily[day].append(statistics.mean(returns))
    daily_returns = [statistics.mean(strategy_daily[day]) for day in sorted(strategy_daily)]

    if len(daily_returns) < 5:
        return None, len(daily_returns)
    deviation = statistics.stdev(daily_returns)
    if deviation == 0:
        return None, len(daily_returns)
    return (statistics.mean(daily_returns) / deviation) * math.sqrt(252), len(daily_returns)


def calculate_half_kelly(closed_positions: list[dict]) -> dict:
    completed = [p for p in closed_positions if p.get("return_pct") is not None]
    winners = [p["return_pct"] for p in completed if p["return_pct"] > 0]
    losers = [p["return_pct"] for p in completed if p["return_pct"] <= 0]
    result = {
        "value_pct": None,
        "completed": len(completed),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate_pct": round((len(winners) / len(completed)) * 100, 1) if completed else None,
        "average_win_pct": round(statistics.mean(winners), 2) if winners else None,
        "average_loss_pct": round(abs(statistics.mean(losers)), 2) if losers else None,
        "reason": None,
    }
    if len(completed) < MIN_KELLY_TRADES:
        result["reason"] = f"Needs {MIN_KELLY_TRADES} completed positions"
        return result
    if not winners or not losers:
        result["reason"] = "Needs at least one winner and one loser"
        return result

    average_win = statistics.mean(winners)
    average_loss = abs(statistics.mean(losers))
    if average_loss == 0:
        result["reason"] = "Average loss is zero"
        return result
    win_probability = len(winners) / len(completed)
    loss_probability = 1.0 - win_probability
    payoff_ratio = average_win / average_loss
    half_kelly = 0.5 * (win_probability - (loss_probability / payoff_ratio))
    result["value_pct"] = round(max(0.0, half_kelly) * 100, 2)
    return result


def summarize_window(
    all_positions: list[dict],
    window: dict,
    run_date: date,
    histories: dict[str, list[dict]],
) -> dict:
    cutoff = add_months(run_date, -window["months"])
    positions = [
        position for position in all_positions
        if date.fromisoformat(position["report_date"]) >= cutoff
        and date.fromisoformat(position["report_date"]) <= run_date
    ]
    priced = [position for position in positions if position.get("return_pct") is not None]
    open_positions = [position for position in positions if position["status"] == "open"]
    closed_positions = [position for position in positions if position["status"] == "closed"]
    pending_positions = [
        position for position in positions if position["status"] in {"pending", "unavailable"}
    ]

    strategy_return = _mean_cohort_return(priced, "return_pct")
    benchmark_return = _mean_cohort_return(priced, "benchmark_return_pct")
    sharpe, sharpe_days = calculate_sharpe(priced, histories)

    return {
        **window,
        "cutoff_date": cutoff.isoformat(),
        "position_count": len(positions),
        "priced_count": len(priced),
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "pending_count": len(pending_positions),
        "return_pct": round(strategy_return, 2) if strategy_return is not None else None,
        "benchmark_return_pct": round(benchmark_return, 2) if benchmark_return is not None else None,
        "excess_return_pct": (
            round(strategy_return - benchmark_return, 2)
            if strategy_return is not None and benchmark_return is not None else None
        ),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sharpe_days": sharpe_days,
        "half_kelly": calculate_half_kelly(closed_positions),
        "open_positions": sorted(open_positions, key=lambda item: item["report_date"], reverse=True),
        "closed_positions": sorted(closed_positions, key=lambda item: item["valuation_date"], reverse=True),
        "pending_positions": sorted(pending_positions, key=lambda item: item["report_date"], reverse=True),
    }


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.0f}"


def _render(model: dict) -> None:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("performance.html")
    html = template.render(model=model, fmt_money=_fmt_money)
    output = DOCS_DIR / "performance.html"
    output.write_text(html, encoding="utf-8")
    log.info("Wrote performance page → %s", output)


def run(current_date: date | None = None) -> dict:
    run_date = current_date or date.today()
    price_cutoff = last_completed_market_candidate(run_date)
    reports = load_scored_reports()
    cfg = load_config()
    api_key = os.environ.get("POLYGON_API_KEY", "")
    limiter = RateLimiter(cfg["polygon"]["rate_limit_calls_per_min"])
    max_retries = cfg["edgar"]["max_retries"]

    selected: dict[str, list[dict]] = {
        strategy["id"]: build_strategy_positions(reports, strategy)
        for strategy in STRATEGIES
    }
    history_cutoff = add_months(run_date, -12)
    relevant_positions = [
        position
        for positions in selected.values()
        for position in positions
        if date.fromisoformat(position["report_date"]) >= history_cutoff
    ]
    tickers = sorted({position["ticker"] for position in relevant_positions} | {BENCHMARK_TICKER})
    earliest_report = min(
        (date.fromisoformat(position["report_date"]) for position in relevant_positions),
        default=history_cutoff,
    )

    if not api_key:
        log.warning("POLYGON_API_KEY not set — rendering from cached price history only")

    histories = {}
    for index, ticker in enumerate(tickers, start=1):
        log.info("[%d/%d] Performance history: %s", index, len(tickers), ticker)
        histories[ticker] = get_price_history(
            ticker,
            earliest_report,
            price_cutoff,
            api_key,
            limiter,
            max_retries,
        )

    benchmark_dates = [
        day for day in (_bar_date(bar) for bar in histories.get(BENCHMARK_TICKER, []))
        if day and day <= price_cutoff
    ]
    price_as_of = max(benchmark_dates) if benchmark_dates else price_cutoff
    benchmark_bars = histories.get(BENCHMARK_TICKER, [])

    strategy_models = []
    for strategy in STRATEGIES:
        resolved = [
            resolve_position(position, histories.get(position["ticker"], []), benchmark_bars, price_as_of)
            for position in selected[strategy["id"]]
        ]
        strategy_models.append({
            **strategy,
            "windows": [
                summarize_window(resolved, window, run_date, histories)
                for window in WINDOWS
            ],
        })

    model = {
        "version": 2,
        "generated_date": run_date.isoformat(),
        "generated_date_fmt": run_date.strftime("%b %d, %Y"),
        "price_as_of": price_as_of.isoformat(),
        "price_as_of_fmt": price_as_of.strftime("%b %d, %Y"),
        "benchmark": BENCHMARK_TICKER,
        "strategies": strategy_models,
        "assumptions": {
            "entry": "Next trading day's adjusted open after the report",
            "exit": "Adjusted close on the first trading day on or after the calendar holding period",
            "allocation": "Equal dollars per security and equal weight per report cohort",
            "window": "Cohorts whose report date falls within the trailing window",
            "sharpe": "Annualized from equal-weight daily cohort returns with a 0% risk-free rate",
            "kelly": f"Half-Kelly from absolute winners and losers; minimum {MIN_KELLY_TRADES} closed positions",
        },
    }
    save_json(DATA_DIR / "performance.json", model)
    _render(model)
    return model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Evaluation date YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    current = date.fromisoformat(args.date) if args.date else None
    run(current)


if __name__ == "__main__":
    main()
