from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from update_performance import (  # noqa: E402
    STRATEGIES,
    add_months,
    aggregate_securities,
    build_strategy_positions,
    calculate_half_kelly,
    calculate_sharpe,
    resolve_position,
)


def transaction(ticker: str, value: float, score: float) -> dict:
    return {
        "issuer_ticker": ticker,
        "issuer_name": f"{ticker} Company",
        "total_value": value,
        "score": score,
    }


def bar(day: str, open_price: float, close_price: float) -> dict:
    stamp = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return {"t": int(stamp.timestamp() * 1000), "o": open_price, "c": close_price}


class CalendarTests(unittest.TestCase):
    def test_add_months_clamps_month_end(self) -> None:
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2026, 3, 31), -1), date(2026, 2, 28))


class SelectionTests(unittest.TestCase):
    def test_strategy_matrix_includes_all_three_holding_periods(self) -> None:
        matrix = {(strategy["rank_by"], strategy["holding_months"]) for strategy in STRATEGIES}
        self.assertEqual(matrix, {
            ("size", 1),
            ("score", 1),
            ("size", 3),
            ("score", 3),
            ("size", 6),
            ("score", 6),
        })

    def test_security_level_aggregation(self) -> None:
        candidates = aggregate_securities([
            transaction("ABC", 100_000, 3),
            transaction("ABC", 250_000, 5),
        ])
        self.assertEqual(candidates["ABC"]["combined_value"], 350_000)
        self.assertEqual(candidates["ABC"]["combined_score"], 8)
        self.assertEqual(candidates["ABC"]["transaction_count"], 2)

    def test_score_and_size_rankings_are_independent(self) -> None:
        reports = [{
            "report_date": date(2026, 1, 2),
            "transactions": [
                transaction("BIG", 1_000_000, 1),
                transaction("SIGNAL", 10_000, 10),
            ],
        }]
        size = build_strategy_positions(
            reports, {"id": "size", "rank_by": "size", "holding_months": 1}
        )
        score = build_strategy_positions(
            reports, {"id": "score", "rank_by": "score", "holding_months": 1}
        )
        self.assertEqual(size[0]["ticker"], "BIG")
        self.assertEqual(score[0]["ticker"], "SIGNAL")

    def test_reentry_only_after_exit_and_unselected_security_stays_ineligible(self) -> None:
        first_report = [
            transaction("AAA", 600, 6),
            transaction("BBB", 500, 5),
            transaction("CCC", 400, 4),
            transaction("DDD", 300, 3),
            transaction("EEE", 200, 2),
            transaction("FFF", 100, 1),
        ]
        reports = [
            {"report_date": date(2026, 1, 2), "transactions": first_report},
            {
                "report_date": date(2026, 1, 16),
                "transactions": [transaction("AAA", 900, 9), transaction("FFF", 800, 8)],
            },
            {
                "report_date": date(2026, 2, 2),
                "transactions": [transaction("AAA", 900, 9), transaction("FFF", 800, 8)],
            },
        ]
        strategy = {"id": "size_1m", "rank_by": "size", "holding_months": 1}

        positions = build_strategy_positions(reports, strategy)
        aaa_entries = [p for p in positions if p["ticker"] == "AAA"]
        fff_entries = [p for p in positions if p["ticker"] == "FFF"]

        self.assertEqual([p["report_date"] for p in aaa_entries], ["2026-01-02", "2026-02-02"])
        self.assertEqual(fff_entries, [])


class PositionResolutionTests(unittest.TestCase):
    def test_next_session_open_and_first_exit_session_close(self) -> None:
        position = {
            "ticker": "AAA",
            "report_date": "2026-01-05",
            "target_exit_date": "2026-02-05",
        }
        stock = [
            bar("2026-01-05", 9, 9),
            bar("2026-01-06", 10, 11),
            bar("2026-02-06", 11, 12),
        ]
        spy = [
            bar("2026-01-05", 99, 99),
            bar("2026-01-06", 100, 101),
            bar("2026-02-05", 103, 104),
            bar("2026-02-06", 104, 105),
        ]

        resolved = resolve_position(position, stock, spy, date(2026, 2, 10))

        self.assertEqual(resolved["status"], "closed")
        self.assertEqual(resolved["entry_date"], "2026-01-06")
        self.assertEqual(resolved["valuation_date"], "2026-02-06")
        self.assertAlmostEqual(resolved["return_pct"], 20.0)
        self.assertAlmostEqual(resolved["benchmark_return_pct"], 5.0)
        self.assertAlmostEqual(resolved["excess_return_pct"], 15.0)

    def test_missing_future_entry_is_reported_as_pending(self) -> None:
        position = {
            "ticker": "AAA",
            "report_date": "2026-01-05",
            "target_exit_date": "2026-02-05",
        }
        resolved = resolve_position(position, [], [], date(2026, 1, 5))
        self.assertEqual(resolved["status"], "pending")
        self.assertIsNone(resolved["return_pct"])


class KellyTests(unittest.TestCase):
    def test_half_kelly_uses_absolute_winners_and_losers(self) -> None:
        positions = [
            {"return_pct": 10.0},
            {"return_pct": 10.0},
            {"return_pct": 10.0},
            {"return_pct": -5.0},
            {"return_pct": -5.0},
        ]
        result = calculate_half_kelly(positions)
        self.assertEqual(result["winners"], 3)
        self.assertEqual(result["losers"], 2)
        self.assertAlmostEqual(result["value_pct"], 20.0)

    def test_half_kelly_waits_for_enough_completed_positions(self) -> None:
        result = calculate_half_kelly([{"return_pct": 4.0}, {"return_pct": -2.0}])
        self.assertIsNone(result["value_pct"])
        self.assertIn("Needs 5", result["reason"])


class SharpeTests(unittest.TestCase):
    def test_sharpe_uses_daily_position_history(self) -> None:
        positions = [{
            "ticker": "AAA",
            "report_date": "2026-01-02",
            "entry_date": "2026-01-05",
            "valuation_date": "2026-01-12",
            "entry_price": 100.0,
        }]
        histories = {"AAA": [
            bar("2026-01-05", 100, 101),
            bar("2026-01-06", 101, 103),
            bar("2026-01-07", 103, 102),
            bar("2026-01-08", 102, 106),
            bar("2026-01-09", 106, 105),
            bar("2026-01-12", 105, 110),
        ]}
        sharpe, observations = calculate_sharpe(positions, histories)
        self.assertEqual(observations, 6)
        self.assertIsNotNone(sharpe)


if __name__ == "__main__":
    unittest.main()
