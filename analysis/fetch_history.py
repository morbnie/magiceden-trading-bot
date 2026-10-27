#!/usr/bin/env python
"""Market data pull for the configured collection(s). Caches raw JSON under
<data>/raw/ and writes normalised tables under <data>/out/.

Usage:
    .venv/bin/python analysis/fetch_history.py            # pull everything
    .venv/bin/python analysis/fetch_history.py --offline  # rebuild tables from cache
    .venv/bin/python analysis/fetch_history.py --only buyers|listings

Sources (all public, no key):
  - Magic Eden v2: stats, listings, pools, activities per type for the
    collection and its signal collections, plus tokens and activities of the
    configured watch wallets. Activity history only reaches back ~90 days.
  - Coinbase Exchange daily candles for SOL-EUR and SOL-USD since 2022-06
    (300 candles per request, no key). CoinGecko's public tier is capped at
    365 days, so Coinbase is the long-history source and CoinGecko the
    cross-check.
  - CoinGecko: current NFT collection snapshot (floor, sales, owners) for the
    ids in config. The historical NFT market_chart endpoint needs a (free)
    Demo key; set COINGECKO_API_KEY to pull it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import time

import requests

from me_api import ACTIVITY_TYPES, DATA_DIR, RAW, MagicEden, load_raw, save_raw
from settings import COINGECKO_NFT_IDS, COLLECTION, COLLECTIONS, WATCH_WALLETS

OUT = DATA_DIR / "out"
PRICE_START = "2022-06-01"

log = logging.getLogger("fetch")


# ---------------------------------------------------------------- Magic Eden

def pull_magic_eden(me: MagicEden) -> None:
    for symbol in COLLECTIONS:
        log.info("[%s] stats / listings / pools", symbol)
        save_raw("stats", me.stats(symbol), symbol)
        save_raw("listings", me.listings(symbol), symbol)
        save_raw("pools", me.pools(symbol), symbol)
        for t in ACTIVITY_TYPES:
            rows = me.activities(symbol, [t])
            log.info("[%s] activities type=%s: %d", symbol, t, len(rows))
            save_raw(f"activities_{t}", rows, symbol)
    for wallet in WATCH_WALLETS:
        log.info("[watch wallet %s] tokens / activities", wallet[:6])
        save_raw(f"tokens_{wallet}", me.wallet_tokens(wallet), "wallets")
        save_raw(f"activities_{wallet}", me.wallet_activities(wallet), "wallets")


def pull_buyer_holdings(me: MagicEden) -> None:
    """Current holdings, within the configured collections, of every wallet that
    bought a unit of the primary collection in the window. Used for the
    cross-collection demand-signal check when signal collections are set."""
    buyers = sorted({a["buyer"] for a in load_raw("activities_buyNow", COLLECTION) or [] if a.get("buyer")})
    log.info("buyer holdings for %d wallets", len(buyers))
    out = {}
    for i, w in enumerate(buyers):
        toks = me.wallet_tokens(w)
        out[w] = [{"mint": t.get("mintAddress"), "collection": t.get("collection"), "listStatus": t.get("listStatus"), "price": t.get("price")}
                  for t in toks if t.get("collection") in COLLECTIONS]
        if i % 20 == 0:
            log.info("  %d/%d", i, len(buyers))
    save_raw("buyer_holdings", out, COLLECTION)


# ------------------------------------------------------------------- prices

def pull_coinbase(product: str, start: str) -> list[list]:
    """Daily candles [time, low, high, open, close, volume], oldest first."""
    url = f"https://api.exchange.coinbase.com/products/{product}/candles"
    out: dict[int, list] = {}
    cur = dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC)
    end = dt.datetime.now(dt.UTC)
    while cur < end:
        nxt = min(cur + dt.timedelta(days=299), end)
        r = requests.get(url, params={"granularity": 86400, "start": cur.isoformat(), "end": nxt.isoformat()}, timeout=30)
        r.raise_for_status()
        for c in r.json():
            out[c[0]] = c
        cur = nxt
        time.sleep(0.4)
    return [out[k] for k in sorted(out)]


def pull_coingecko(key: str | None) -> None:
    s = requests.Session()
    if key:
        s.headers["x-cg-demo-api-key"] = key
    base = "https://api.coingecko.com/api/v3"

    def get(path, params=None):
        for attempt in range(4):
            r = s.get(f"{base}{path}", params=params, timeout=60)
            if r.status_code == 200:
                time.sleep(3)
                return r.json()
            log.warning("coingecko %s on %s: %s", r.status_code, path, r.text[:160])
            time.sleep(15 * (attempt + 1))
        return None

    for vs in ("eur", "usd"):
        d = get("/coins/solana/market_chart", {"vs_currency": vs, "days": 365, "interval": "daily"})
        if d:
            save_raw(f"sol_{vs}_365d", d, "coingecko")
    for symbol, cg_id in COINGECKO_NFT_IDS.items():
        d = get(f"/nfts/{cg_id}")
        if d:
            save_raw(f"nft_{symbol}", d, "coingecko")
        if key:  # needs a Demo/Pro key; public tier returns 401
            d = get(f"/nfts/{cg_id}/market_chart", {"days": 365})
            if d:
                save_raw(f"nft_{symbol}_market_chart", d, "coingecko")


# ---------------------------------------------------------------- normalise

ACTIVITY_COLS = ["collection", "type", "source", "blockTime", "ts", "date", "slot", "signature",
                 "tokenMint", "buyer", "seller", "price"]


def write_activities_csv() -> int:
    seen = set()
    n = 0
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "activities.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ACTIVITY_COLS)
        w.writeheader()
        for symbol in COLLECTIONS:
            for t in ACTIVITY_TYPES:
                for a in load_raw(f"activities_{t}", symbol) or []:
                    key = (a.get("signature"), a.get("type"), a.get("tokenMint"), a.get("buyer"), a.get("seller"), a.get("price"))
                    if key in seen:
                        continue
                    seen.add(key)
                    bt = a.get("blockTime") or 0
                    ts = dt.datetime.fromtimestamp(bt, dt.UTC)
                    w.writerow({
                        "collection": symbol, "type": a.get("type"), "source": a.get("source"),
                        "blockTime": bt, "ts": ts.isoformat(), "date": ts.date().isoformat(),
                        "slot": a.get("slot"), "signature": a.get("signature"),
                        "tokenMint": a.get("tokenMint"), "buyer": a.get("buyer") or "",
                        "seller": a.get("seller") or "", "price": a.get("price"),
                    })
                    n += 1
    return n


def write_prices_csv() -> int:
    eur = {c[0]: c for c in load_raw("coinbase_SOL-EUR", "prices") or []}
    usd = {c[0]: c for c in load_raw("coinbase_SOL-USD", "prices") or []}
    days = sorted(set(eur) | set(usd))
    with (OUT / "sol_prices.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "sol_eur", "sol_usd", "eur_usd_implied"])
        for d in days:
            e = eur.get(d, [None] * 6)[4]
            u = usd.get(d, [None] * 6)[4]
            w.writerow([dt.datetime.fromtimestamp(d, dt.UTC).date().isoformat(), e, u,
                        round(u / e, 4) if e and u else None])
    return len(days)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="rebuild tables from the raw cache only")
    ap.add_argument("--skip-me", action="store_true")
    ap.add_argument("--skip-prices", action="store_true")
    ap.add_argument("--only", choices=["buyers", "listings"], help="run one step and exit")
    args = ap.parse_args()

    if args.only == "buyers":
        pull_buyer_holdings(MagicEden())
        return
    if args.only == "listings":
        me = MagicEden()
        for symbol in COLLECTIONS:
            rows = me.listings(symbol)
            log.info("[%s] listings: %d", symbol, len(rows))
            save_raw("listings", rows, symbol)
        return

    if not args.offline:
        if not args.skip_me:
            me = MagicEden()
            pull_magic_eden(me)
            pull_buyer_holdings(me)
        if not args.skip_prices:
            for product in ("SOL-EUR", "SOL-USD"):
                log.info("coinbase %s", product)
                save_raw(f"coinbase_{product}", pull_coinbase(product, PRICE_START), "prices")
            pull_coingecko(os.environ.get("COINGECKO_API_KEY"))
        RAW.mkdir(parents=True, exist_ok=True)
        (RAW / "pulled_at.txt").write_text(dt.datetime.now(dt.UTC).isoformat())

    n = write_activities_csv()
    m = write_prices_csv()
    log.info("wrote %d activity rows and %d price days to %s", n, m, OUT)


if __name__ == "__main__":
    main()
