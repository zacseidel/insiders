from __future__ import annotations

"""
Stage 4: Score and rank insider purchases.

Usage:
  python src/score_and_rank.py [--date YYYY-MM-DD]
"""
import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, load_config, load_json, save_json, setup_logging

log = setup_logging("score_and_rank")

C_SUITE_TITLES = {"ceo", "cfo", "coo", "cto", "ciso", "president", "chief"}


def is_c_suite(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in C_SUITE_TITLES)


def score_transaction(tx: dict, cfg: dict, cluster_ciks: set) -> tuple[int, dict]:
    scoring = cfg["scoring"]
    breakdown = {}
    total = 0

    value = tx.get("total_value") or 0
    for thresh in scoring["value_thresholds"]:
        if value >= thresh["min"]:
            pts = thresh["points"]
            breakdown["value"] = pts
            total += pts
            break

    pct = tx.get("percentage_increase") or 0
    for thresh in scoring["pct_increase_thresholds"]:
        if pct >= thresh["min"]:
            pts = thresh["points"]
            breakdown["pct_increase"] = pts
            total += pts
            break

    role_pts = scoring["role_points"]
    title = tx.get("insider_title", "")
    if tx.get("is_officer") and is_c_suite(title):
        breakdown["role"] = role_pts["c_suite"]
        total += role_pts["c_suite"]
    elif tx.get("is_officer"):
        breakdown["role"] = role_pts["officer"]
        total += role_pts["officer"]
    elif tx.get("is_director"):
        breakdown["role"] = role_pts["director"]
        total += role_pts["director"]

    issuer_cik = tx.get("issuer_cik", "")
    if issuer_cik in cluster_ciks:
        breakdown["cluster"] = scoring["cluster_bonus"]
        total += scoring["cluster_bonus"]

    return total, breakdown


def detect_clusters(transactions: list) -> dict[str, str]:
    """Return map of issuer_cik → cluster_id for CIKs with >=2 distinct insider buys."""
    insiders_by_cik: dict[str, set] = defaultdict(set)
    for tx in transactions:
        cik = tx.get("issuer_cik", "")
        insider = tx.get("insider_cik", "")
        if cik and insider:
            insiders_by_cik[cik].add(insider)

    cluster_map = {}
    cluster_counter = 1
    for cik, insiders in insiders_by_cik.items():
        if len(insiders) >= 2:
            cluster_map[cik] = f"C{cluster_counter:03d}"
            cluster_counter += 1
    return cluster_map


def run(run_date: date) -> None:
    cfg = load_config()
    enriched_path = DATA_DIR / "enriched" / f"{run_date.isoformat()}.json"

    if not enriched_path.exists():
        log.error("Enriched file not found: %s", enriched_path)
        return

    data = load_json(enriched_path)
    transactions = data.get("transactions", [])
    log.info("Scoring %d transactions", len(transactions))

    cluster_map = detect_clusters(transactions)
    cluster_ciks = set(cluster_map.keys())
    log.info("Detected %d cluster companies", len(cluster_ciks))

    scored = []
    for tx in transactions:
        issuer_cik = tx.get("issuer_cik", "")
        cluster_id = cluster_map.get(issuer_cik)
        score, breakdown = score_transaction(tx, cfg, cluster_ciks)
        scored.append({
            **tx,
            "score": score,
            "score_breakdown": breakdown,
            "cluster_id": cluster_id,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    output = {
        **{k: v for k, v in data.items() if k != "transactions"},
        "transactions": scored,
        "cluster_companies": {
            cik: {
                "cluster_id": cid,
                "transactions": [t for t in scored if t.get("issuer_cik") == cik],
            }
            for cik, cid in cluster_map.items()
        },
    }

    out_path = DATA_DIR / "scored" / f"{run_date.isoformat()}.json"
    save_json(out_path, output)
    log.info("Saved scored data to %s", out_path)

    top5 = scored[:5]
    for tx in top5:
        log.info(
            "  Score %2d | %s | %s | $%s",
            tx["score"],
            tx.get("issuer_ticker", "?"),
            tx.get("insider_name", "?"),
            f"{tx.get('total_value', 0):,.0f}" if tx.get("total_value") else "N/A",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Run date YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    run(run_date)


if __name__ == "__main__":
    main()
