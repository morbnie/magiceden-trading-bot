"""Small Magic Eden v2 read client used by the analysis scripts.

Public read endpoints only, no API key. Rate limit is about 120 requests per
minute per IP; we stay at 100 QPM. Every response is cached as raw JSON under
<data>/raw/ so a re-run of the analysis does not hit the API again.

Behaviour verified against the live API (see docs/verified.md):
  - `limit` must be within [1, 500] (listings: [1, 100])
  - `offset` is rejected above roughly 15,500
  - `type` is a comma-delimited list; filtering by type gives each type its
    own offset budget, which matters when poolUpdate events dominate a feed
  - history is only kept for roughly 90 days (older events are not returned
    by the collection, token or wallet activity endpoints)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

BASE = "https://api-mainnet.magiceden.dev/v2"
# Data root: analysis/ by default; set BOT_DATA_DIR (e.g. a mounted volume) to move raw/ and out/.
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR") or Path(__file__).resolve().parent)
RAW = DATA_DIR / "raw"
MIN_INTERVAL_S = 60.0 / 100  # 100 requests per minute
MAX_LIMIT = 500
MAX_OFFSET = 15_000  # the API returns 400 above ~15,500

ACTIVITY_TYPES = [
    "buyNow", "buy", "list", "delist", "bid", "cancelBid", "poolUpdate",
    "mint", "transfer",
    "auctionCreated", "auctionUpdated", "auctionCanceled", "auctionSettled", "auctionPlaceBid",
]

log = logging.getLogger("me_api")


class MagicEden:
    def __init__(self, session: requests.Session | None = None, qpm: int = 100):
        self.s = session or requests.Session()
        self.s.headers["accept"] = "application/json"
        self.min_interval = 60.0 / qpm
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, path: str, params: dict | None = None, retries: int = 5):
        url = f"{BASE}{path}"
        for attempt in range(retries):
            self._throttle()
            try:
                r = self.s.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                log.warning("request error %s %s: %s", path, params, e)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                retry_after = float(r.headers.get("retry-after", 2 ** attempt))
                log.warning("429 on %s, sleeping %.1fs", path, retry_after)
                time.sleep(retry_after)
                continue
            if r.status_code == 400:
                # offset past the cap or bad params: caller treats None as "no more"
                log.info("400 on %s %s: %s", path, params, r.text[:200])
                return None
            log.warning("%s on %s %s: %s", r.status_code, path, params, r.text[:200])
            time.sleep(2 ** attempt)
        raise RuntimeError(f"giving up on {path} {params}")

    # ---- paging ---------------------------------------------------------

    def paged(self, path: str, params: dict | None = None, results_key: str | None = None, limit: int = MAX_LIMIT):
        """Yield every record from an offset/limit endpoint until an empty page or the cap.
        `limit` is per endpoint: activities, pools and wallet tokens take 500, listings only 100."""
        params = dict(params or {})
        offset = 0
        while offset <= MAX_OFFSET:
            params.update(offset=offset, limit=limit)
            page = self.get(path, params)
            if page is None:
                return
            if results_key and isinstance(page, dict):
                page = page.get(results_key, [])
            if not page:
                return
            yield from page
            if len(page) < limit:
                return
            offset += limit

    # ---- endpoints ------------------------------------------------------

    def stats(self, symbol: str):
        return self.get(f"/collections/{symbol}/stats")

    def listings(self, symbol: str):
        return list(self.paged(f"/collections/{symbol}/listings", limit=100))  # 400 above 100

    def pools(self, symbol: str):
        return list(self.paged("/mmm/pools", {"collectionSymbol": symbol}, results_key="results"))

    def activities(self, symbol: str, types: list[str] | None = None):
        params = {"type": ",".join(types)} if types else None
        return list(self.paged(f"/collections/{symbol}/activities", params))

    def token_activities(self, mint: str):
        return list(self.paged(f"/tokens/{mint}/activities"))

    def wallet_tokens(self, wallet: str, list_status: str = "both"):
        return list(self.paged(f"/wallets/{wallet}/tokens", {"listStatus": list_status}))

    def wallet_activities(self, wallet: str):
        return list(self.paged(f"/wallets/{wallet}/activities"))


# ---- raw cache helpers ----------------------------------------------------

def save_raw(name: str, data, subdir: str = "") -> Path:
    d = RAW / subdir if subdir else RAW
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps(data, separators=(",", ":")))
    return p


def load_raw(name: str, subdir: str = ""):
    p = (RAW / subdir if subdir else RAW) / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None
