from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache" / "polygon"


def _load_dotenv() -> None:
    """Load KEY=value pairs from .env into os.environ (existing env vars win)."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(name)


def load_json(path: Path) -> Any:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def get_window(run_date: date, lookback_days: int = 7) -> tuple[date, date]:
    """Return (start_date, end_date) for a rolling lookback window."""
    return run_date - timedelta(days=lookback_days), run_date


class RateLimiter:
    """Simple fixed-interval rate limiter."""

    def __init__(self, calls_per_minute: float):
        self._interval = 60.0 / calls_per_minute
        self._last_call = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_call = time.monotonic()


class PolygonClient:
    BASE = "https://api.polygon.io"

    def __init__(self, api_key: str, cfg: dict):
        self._key = api_key
        self._cfg = cfg
        self._limiter = RateLimiter(cfg["rate_limit_calls_per_min"])
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "InsiderBuyTracker/1.0"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        if params is None:
            params = {}
        params["apiKey"] = self._key
        self._limiter.wait()
        cfg = load_config()
        max_retries = cfg["edgar"]["max_retries"]
        for attempt in range(max_retries):
            resp = self._session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = 60 * (attempt + 1)
                logging.getLogger("polygon").warning(
                    "Rate limited, sleeping %ds (attempt %d)", wait, attempt + 1
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Max retries exceeded for {url}")

    def _cache_path(self, ticker: str) -> Path:
        return CACHE_DIR / f"{ticker}.json"

    def _cache_valid(self, path: Path, ttl_days: int) -> bool:
        if not path.exists():
            return False
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return (datetime.now() - mtime).days < ttl_days

    def _safe_err(self, e: Exception) -> str:
        """Strip the apiKey from exception messages before logging."""
        return str(e).replace(self._key, "***")

    def ticker_details(self, ticker: str) -> Optional[dict]:
        path = self._cache_path(ticker)
        if self._cache_valid(path, self._cfg["cache_ttl_days"]):
            return load_json(path)
        url = f"{self.BASE}/v3/reference/tickers/{ticker}"
        try:
            data = self._get(url)
            result = data.get("results", {})
            save_json(path, result)
            return result
        except Exception as e:
            logging.getLogger("polygon").warning("ticker_details failed for %s: %s", ticker, self._safe_err(e))
            return None

    def ticker_news(self, ticker: str) -> list:
        url = f"{self.BASE}/v2/reference/news"
        try:
            data = self._get(url, {"ticker": ticker, "limit": self._cfg["news_limit"]})
            return data.get("results", [])
        except Exception as e:
            logging.getLogger("polygon").warning("ticker_news failed for %s: %s", ticker, self._safe_err(e))
            return []

    def aggregates(self, ticker: str, from_date: str, to_date: str) -> list:
        url = f"{self.BASE}/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
        try:
            data = self._get(url, {"adjusted": "true", "sort": "asc"})
            return data.get("results", [])
        except Exception as e:
            logging.getLogger("polygon").warning("aggregates failed for %s: %s", ticker, self._safe_err(e))
            return []


def get_edgar_user_agent() -> str:
    """Prefer EDGAR_USER_AGENT env var; fall back to config.yaml."""
    return os.environ.get("EDGAR_USER_AGENT") or load_config()["edgar"]["user_agent"]


def edgar_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = get_edgar_user_agent()
    s.headers["Accept-Encoding"] = "gzip, deflate"
    return s


def update_checkpoint(run_date: date) -> None:
    path = DATA_DIR / "checkpoint.json"
    save_json(path, {
        "last_run_date": run_date.isoformat(),
        "last_run_ts": datetime.utcnow().isoformat() + "Z",
    })
