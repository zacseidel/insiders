from __future__ import annotations

"""
Stage 1: Fetch Form 4 filings from SEC EDGAR and extract open-market purchases.

Usage:
  python src/fetch_form4s.py [--date YYYY-MM-DD] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""
import argparse
import concurrent.futures
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from utils import DATA_DIR, load_config, save_json, setup_logging, get_window, update_checkpoint, edgar_session, get_edgar_user_agent

_PARALLEL_WORKERS = 10


class _RateLimiter:
    """Thread-safe token issuer: at most max_per_sec tokens per second."""
    def __init__(self, max_per_sec: int = 10):
        self._lock = threading.Lock()
        self._last = 0.0
        self._interval = 1.0 / max_per_sec

    def wait(self) -> None:
        with self._lock:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

log = setup_logging("fetch_form4s")

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_BASE = "https://www.sec.gov"


def xml_url_from_hit(hit: dict) -> tuple[str, str, str] | None:
    """
    Return (xml_url, archive_cik, adsh) from an EFTS hit.

    The _id field is '{adsh}:{xml_filename}'. EDGAR stores Form 4s under both the
    insider CIK and the issuer CIK, so ciks[0] from _source always works as the
    archive path — even when the accession was filed by a third-party filing agent.
    """
    hit_id = hit.get("_id", "")
    if ":" not in hit_id:
        return None
    adsh, xml_filename = hit_id.split(":", 1)
    if not adsh or not xml_filename.endswith(".xml"):
        return None

    src = hit.get("_source", {})
    ciks = src.get("ciks", [])
    if ciks:
        archive_cik = str(int(ciks[0]))  # strip leading zeros; both insider+issuer CIKs work
    else:
        archive_cik = str(int(adsh.split("-")[0]))  # fall back to accession prefix

    acc_no_dashes = adsh.replace("-", "")
    xml_url = f"{EDGAR_BASE}/Archives/edgar/data/{archive_cik}/{acc_no_dashes}/{xml_filename}"
    return xml_url, archive_cik, adsh


def filing_index_url(adsh: str, archive_cik: str) -> str:
    acc_no_dashes = adsh.replace("-", "")
    return f"{EDGAR_BASE}/Archives/edgar/data/{archive_cik}/{acc_no_dashes}/{adsh}-index.htm"


def xml_val(elem, tag: str) -> str | None:
    node = elem.find(f".//{tag}/value")
    if node is not None and node.text:
        return node.text.strip()
    node = elem.find(f".//{tag}")
    if node is not None and node.text:
        return node.text.strip()
    return None


def parse_form4(xml_text: str, accession: str, filing_date: str, filing_url: str) -> list[dict]:
    transactions = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("XML parse error for %s: %s", accession, e)
        return []

    issuer_cik = xml_val(root, "issuerCik") or ""
    issuer_name = xml_val(root, "issuerName") or ""
    issuer_ticker = xml_val(root, "issuerTradingSymbol") or ""

    owner_cik = xml_val(root, "rptOwnerCik") or ""
    owner_name = xml_val(root, "rptOwnerName") or ""

    rel = root.find(".//reportingOwnerRelationship")
    is_director = False
    is_officer = False
    is_ten_pct = False
    officer_title = ""
    if rel is not None:
        is_director = (xml_val(rel, "isDirector") or "0") == "1"
        is_officer = (xml_val(rel, "isOfficer") or "0") == "1"
        is_ten_pct = (xml_val(rel, "isTenPercentOwner") or "0") == "1"
        officer_title = xml_val(rel, "officerTitle") or ""

    for idx, tx in enumerate(root.findall(".//nonDerivativeTransaction")):
        code = xml_val(tx, "transactionCode")
        acquired = xml_val(tx, "transactionAcquiredDisposedCode")
        if code != "P" or acquired != "A":
            continue

        shares_str = xml_val(tx, "transactionShares")
        price_str = xml_val(tx, "transactionPricePerShare")
        tx_date = xml_val(tx, "transactionDate")
        shares_after_str = xml_val(tx, "sharesOwnedFollowingTransaction")

        try:
            shares = float(shares_str) if shares_str else None
        except ValueError:
            shares = None

        try:
            price = float(price_str) if price_str else None
        except ValueError:
            price = None

        try:
            shares_after = float(shares_after_str) if shares_after_str else None
        except ValueError:
            shares_after = None

        if shares is None:
            continue

        total_value = (shares * price) if price is not None else None
        pct_increase = None
        if shares is not None and shares_after is not None and shares_after > shares:
            shares_before = shares_after - shares
            if shares_before > 0:
                pct_increase = round((shares / shares_before) * 100, 2)

        transactions.append({
            "filing_accession": accession,
            "transaction_index": idx,
            "dedup_key": f"{accession}:{idx}",
            "filing_date": filing_date,
            "filing_url": filing_url,
            "issuer_cik": issuer_cik,
            "issuer_name": issuer_name,
            "issuer_ticker": issuer_ticker,
            "insider_name": owner_name,
            "insider_cik": owner_cik,
            "insider_title": officer_title,
            "is_director": is_director,
            "is_officer": is_officer,
            "is_ten_pct_owner": is_ten_pct,
            "transaction_date": tx_date,
            "shares": shares,
            "price_per_share": price,
            "total_value": total_value,
            "shares_owned_after": shares_after,
            "percentage_increase": pct_increase,
        })

    return transactions


def _fetch_xml(record: dict, user_agent: str, limiter: _RateLimiter) -> list[dict]:
    """Worker: fetch one Form 4 XML and return its transactions. Thread-safe."""
    src = record.get("_source", {})
    filing_date = src.get("file_date", "")

    parsed = xml_url_from_hit(record)
    if not parsed:
        return []

    xml_url, archive_cik, adsh = parsed
    filing_url = filing_index_url(adsh, archive_cik)

    limiter.wait()
    try:
        resp = requests.get(xml_url, headers={"User-Agent": user_agent}, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Failed to fetch XML %s: %s", xml_url, e)
        return []

    return parse_form4(resp.text, adsh, filing_date, filing_url)


def run(run_date: date, start: date, end: date) -> None:
    cfg = load_config()
    edgar_cfg = cfg["edgar"]
    filters = cfg["filters"]
    min_value = filters.get("min_transaction_value", 0)
    exclude_10pct_only = filters.get("exclude_10pct_only", True)

    session = edgar_session()
    user_agent = get_edgar_user_agent()

    start_str = start.isoformat()
    end_str = end.isoformat()
    log.info("Fetching Form 4 filings %s → %s", start_str, end_str)

    output_path = DATA_DIR / "raw" / f"{run_date.isoformat()}.json"
    existing: dict[str, dict] = {}

    if output_path.exists():
        import json
        with open(output_path) as f:
            prev = json.load(f)
        for tx in prev.get("transactions", []):
            existing[tx["dedup_key"]] = tx
        log.info("Loaded %d existing transactions from %s", len(existing), output_path.name)

    # Phase 1: paginate EFTS index (fast — metadata only, no XML fetching)
    page_size = 100
    from_ = 0
    total_filings = None
    all_records: list[dict] = []

    while True:
        params = {
            "forms": "4",
            "dateRange": "custom",
            "startdt": start_str,
            "enddt": end_str,
            "from": from_,
        }
        result = None
        for attempt in range(edgar_cfg["max_retries"]):
            try:
                resp = session.get(EFTS_URL, params=params, timeout=30)
                if resp.status_code >= 500:
                    wait = 15 * (attempt + 1)
                    log.warning("EFTS 5xx at offset %d (attempt %d), retrying in %ds", from_, attempt + 1, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                result = resp.json()
                break
            except Exception as e:
                log.warning("EFTS query error at offset %d (attempt %d): %s", from_, attempt + 1, e)
                if attempt < edgar_cfg["max_retries"] - 1:
                    time.sleep(15 * (attempt + 1))
        if result is None:
            log.error("EFTS query failed after %d attempts at offset %d — stopping pagination", edgar_cfg["max_retries"], from_)
            break

        hits = result.get("hits", {})
        if total_filings is None:
            total_filings = hits.get("total", {}).get("value", 0)
            log.info("Total filings to process: %d", total_filings)

        records = hits.get("hits", [])
        if not records:
            break

        all_records.extend(records)
        from_ += page_size
        if from_ >= (total_filings or 0):
            break
        log.info("Paginated: %d / %d", len(all_records), total_filings)

    log.info(
        "Collected %d EFTS hits; starting parallel XML fetch (%d workers, 10 req/s)",
        len(all_records), _PARALLEL_WORKERS,
    )

    # Phase 2: parallel XML fetch at ≤10 req/s (SEC fair-access policy)
    limiter = _RateLimiter(max_per_sec=10)
    new_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=_PARALLEL_WORKERS) as pool:
        futures = {pool.submit(_fetch_xml, rec, user_agent, limiter): rec for rec in all_records}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            if done % 500 == 0:
                log.info("XML fetch progress: %d / %d", done, len(all_records))
            try:
                txs = fut.result()
            except Exception as e:
                log.warning("Worker exception: %s", e)
                continue
            for tx in txs:
                if tx["total_value"] is not None and tx["total_value"] < min_value:
                    continue
                if exclude_10pct_only and tx["is_ten_pct_owner"] and not tx["is_officer"] and not tx["is_director"]:
                    continue
                key = tx["dedup_key"]
                if key not in existing:
                    existing[key] = tx
                    new_count += 1

    all_transactions = list(existing.values())
    log.info("Total transactions after fetch: %d (%d new)", len(all_transactions), new_count)

    payload = {
        "run_date": run_date.isoformat(),
        "window_start": start_str,
        "window_end": end_str,
        "last_updated": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "transactions": all_transactions,
    }
    save_json(output_path, payload)
    log.info("Saved to %s", output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Run date YYYY-MM-DD (default: today)")
    parser.add_argument("--start", help="Window start override YYYY-MM-DD")
    parser.add_argument("--end", help="Window end override YYYY-MM-DD")
    args = parser.parse_args()

    cfg = load_config()
    lookback = cfg["pipeline"]["lookback_days"]

    run_date = date.fromisoformat(args.date) if args.date else date.today()

    if args.start and args.end:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    else:
        start, end = get_window(run_date, lookback)

    run(run_date, start, end)
    update_checkpoint(run_date)


if __name__ == "__main__":
    main()
