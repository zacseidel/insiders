from __future__ import annotations

"""
Backfill: generate reports for all Tue/Fri run dates over the last quarter.

Run this once locally before the first push to seed the archive.

Usage:
  POLYGON_API_KEY=xxx EDGAR_USER_AGENT="..." python src/backfill.py [--weeks 13] [--dry-run]
"""
import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import ROOT, setup_logging

log = setup_logging("backfill")

DOCS_REPORTS = ROOT / "docs" / "reports"


def get_backfill_dates(weeks: int = 13) -> list[date]:
    today = date.today()
    dates = []
    d = today
    while d > today - timedelta(weeks=weeks):
        if d.weekday() in (1, 4):  # Tuesday=1, Friday=4
            dates.append(d)
        d -= timedelta(days=1)
    return sorted(dates)


def run_stage(script: str, run_date: date, extra_args: list[str] | None = None) -> bool:
    cmd = [sys.executable, str(ROOT / "src" / script), "--date", run_date.isoformat()]
    if extra_args:
        cmd.extend(extra_args)
    log.info("  Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        log.error("  FAILED: %s for %s", script, run_date)
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=4, help="How many weeks back to backfill")
    parser.add_argument("--dry-run", action="store_true", help="Print dates without running")
    args = parser.parse_args()

    backfill_dates = get_backfill_dates(args.weeks)
    log.info("Backfill plan: %d dates over %d weeks", len(backfill_dates), args.weeks)
    for d in backfill_dates:
        log.info("  %s (%s)", d.isoformat(), d.strftime("%A"))

    if args.dry_run:
        log.info("Dry run — exiting without running pipeline")
        return

    DOCS_REPORTS.mkdir(parents=True, exist_ok=True)

    for run_date in backfill_dates:
        report_path = DOCS_REPORTS / f"{run_date.isoformat()}.html"
        if report_path.exists():
            log.info("[%s] Report already exists — skipping", run_date)
            continue

        log.info("=" * 60)
        log.info("Backfilling: %s", run_date.isoformat())

        window_end = run_date
        window_start = run_date - __import__("datetime").timedelta(days=7)

        ok = run_stage("fetch_form4s.py", run_date, ["--start", window_start.isoformat(), "--end", window_end.isoformat()])
        if not ok:
            log.warning("fetch_form4s failed for %s — continuing", run_date)
            continue

        ok = run_stage("enrich.py", run_date)
        if not ok:
            log.warning("enrich failed for %s — continuing", run_date)

        ok = run_stage("fetch_charts.py", run_date)
        if not ok:
            log.warning("fetch_charts failed for %s — continuing", run_date)

        ok = run_stage("score_and_rank.py", run_date)
        if not ok:
            log.warning("score_and_rank failed for %s — continuing", run_date)
            continue

        ok = run_stage("generate_report.py", run_date)
        if not ok:
            log.warning("generate_report failed for %s — continuing", run_date)
        else:
            log.info("[%s] Done ✓", run_date)

    # Run performance tracker once after all reports are generated
    log.info("=" * 60)
    log.info("Updating performance tracker...")
    run_stage("update_performance.py", backfill_dates[-1])

    log.info("Backfill complete")


if __name__ == "__main__":
    main()
