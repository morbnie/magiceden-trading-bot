#!/usr/bin/env python
"""Order-book snapshot: stats, listings and pools for the configured
collection(s), CoinGecko's NFT summary when ids are configured, and SOL spot
in EUR/USD.

Magic Eden keeps ~90 days of activity history and no order-book history at
all, so this is the seller's own time series. Run it hourly (about 3 requests
per collection per run):

    7 * * * * cd /path/to/repo && .venv/bin/python analysis/snapshot_daily.py >> analysis/out/snapshot.log 2>&1

Writes:
  <data>/raw/snapshots/<UTC timestamp>.json   full raw payloads
  <data>/out/snapshots.csv                    one summary row per collection per run
  tables `snapshots` and `snapshots_raw`      in Postgres, when DATABASE_URL is set

<data> is analysis/ by default or BOT_DATA_DIR when set (a mounted volume
on a host). With DATABASE_URL set the rows also go to Postgres so the time
series can be read from anywhere without touching the volume.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys

import requests

from me_api import DATA_DIR, RAW, MagicEden
from settings import COINGECKO_NFT_IDS, COLLECTIONS

OUT = DATA_DIR / "out"
SNAP_DIR = RAW / "snapshots"
FIELDS = ["ts", "collection", "floor_sol", "listed", "volume7d_sol", "avg24h_sol",
          "n_listings", "ask1", "ask2", "ask3", "asks_within_2pct", "asks_within_5pct",
          "n_pools", "bid1", "bid2", "bid3", "bids_within_5pct_escrow_sol", "fillable_bids_within_5pct",
          "sol_eur", "sol_usd", "cg_one_day_sales", "cg_owners"]


def sol_spot() -> tuple[float | None, float | None]:
    out = []
    for product in ("SOL-EUR", "SOL-USD"):
        try:
            r = requests.get(f"https://api.exchange.coinbase.com/products/{product}/ticker", timeout=15)
            out.append(float(r.json()["price"]))
        except Exception:
            out.append(None)
    return out[0], out[1]


def pool_bids(pools: list[dict]) -> list[tuple[float, float, int]]:
    """(spot_sol, escrow_sol, fills) for buy-capable pools with funded escrow, best first."""
    bids = []
    for p in pools:
        if p.get("poolType") not in ("buy_sided", "two_sided"):
            continue
        spot = (p.get("spotPrice") or 0) / 1e9
        esc = p.get("buysidePaymentAmount") or 0
        shared = (p.get("sharedEscrowInfo") or {}).get("sharedEscrowBuysidePaymentAmount")
        if shared is not None:
            esc = min(esc, shared)
        esc /= 1e9
        if spot <= 0 or esc < spot:
            continue
        bids.append((spot, esc, int(esc // spot)))
    return sorted(bids, reverse=True)


PG_DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts timestamptz NOT NULL,
    collection text NOT NULL,
    floor_sol double precision, listed integer, volume7d_sol double precision, avg24h_sol double precision,
    n_listings integer, ask1 double precision, ask2 double precision, ask3 double precision,
    asks_within_2pct integer, asks_within_5pct integer,
    n_pools integer, bid1 double precision, bid2 double precision, bid3 double precision,
    bids_within_5pct_escrow_sol double precision, fillable_bids_within_5pct integer,
    sol_eur double precision, sol_usd double precision, cg_one_day_sales double precision, cg_owners double precision,
    PRIMARY KEY (ts, collection)
);
CREATE TABLE IF NOT EXISTS snapshots_raw (
    ts timestamptz PRIMARY KEY,
    payload jsonb NOT NULL
);
"""


def write_postgres(rows: list[dict], raw: dict, url: str) -> None:
    """Append the summary rows and the full raw payload to Postgres. Failures are
    printed, not raised, so the CSV and raw JSON on disk are always written first."""
    try:
        import psycopg  # psycopg[binary] in requirements
    except ImportError:
        print("psycopg not installed; skipping Postgres sink", file=sys.stderr)
        return
    cols = FIELDS
    sql = f"INSERT INTO snapshots ({', '.join(cols)}) VALUES ({', '.join('%s' for _ in cols)}) ON CONFLICT DO NOTHING"
    try:
        with psycopg.connect(url, connect_timeout=15) as conn:
            conn.execute(PG_DDL)
            with conn.cursor() as cur:
                cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
                cur.execute("INSERT INTO snapshots_raw (ts, payload) VALUES (%s, %s::jsonb) ON CONFLICT DO NOTHING",
                            (raw["ts"], json.dumps(raw, separators=(",", ":"))))
            conn.commit()
            total = conn.execute("SELECT count(*), max(ts) FROM snapshots").fetchone()
            print(f"postgres: wrote {len(rows)} rows; snapshots now holds {total[0]} rows, latest {total[1]}")
    except Exception as e:  # noqa: BLE001
        print(f"postgres sink failed: {e}", file=sys.stderr)


def main() -> None:
    me = MagicEden()
    ts = dt.datetime.now(dt.UTC).replace(microsecond=0)
    sol_eur, sol_usd = sol_spot()
    raw = {"ts": ts.isoformat(), "sol_eur": sol_eur, "sol_usd": sol_usd, "collections": {}}
    rows = []
    for symbol in COLLECTIONS:
        stats = me.stats(symbol) or {}
        listings = me.listings(symbol)
        pools = me.pools(symbol)
        cg = None
        if symbol in COINGECKO_NFT_IDS:
            try:
                r = requests.get(f"https://api.coingecko.com/api/v3/nfts/{COINGECKO_NFT_IDS[symbol]}", timeout=30)
                cg = r.json() if r.status_code == 200 else None
            except Exception:
                pass
        raw["collections"][symbol] = {"stats": stats, "listings": listings, "pools": pools, "coingecko": cg}

        asks = sorted(float(x["price"]) for x in listings if x.get("price"))
        floor = (stats.get("floorPrice") or 0) / 1e9 or (asks[0] if asks else None)
        bids = pool_bids(pools)
        within5 = [b for b in bids if floor and b[0] >= floor * 0.95]
        rows.append({
            "ts": ts.isoformat(), "collection": symbol, "floor_sol": floor,
            "listed": stats.get("listedCount"), "volume7d_sol": (stats.get("volume7d") or 0) / 1e9,
            "avg24h_sol": (stats.get("avgPrice24hr") or 0) / 1e9 or None,
            "n_listings": len(asks), "ask1": asks[0] if asks else None,
            "ask2": asks[1] if len(asks) > 1 else None, "ask3": asks[2] if len(asks) > 2 else None,
            "asks_within_2pct": sum(1 for a in asks if floor and a <= floor * 1.02),
            "asks_within_5pct": sum(1 for a in asks if floor and a <= floor * 1.05),
            "n_pools": len(bids), "bid1": bids[0][0] if bids else None,
            "bid2": bids[1][0] if len(bids) > 1 else None, "bid3": bids[2][0] if len(bids) > 2 else None,
            "bids_within_5pct_escrow_sol": round(sum(b[1] for b in within5), 3),
            "fillable_bids_within_5pct": sum(b[2] for b in within5),
            "sol_eur": sol_eur, "sol_usd": sol_usd,
            "cg_one_day_sales": (cg or {}).get("one_day_sales"),
            "cg_owners": (cg or {}).get("number_of_unique_addresses"),
        })

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / f"{ts.strftime('%Y-%m-%dT%H%M%SZ')}.json").write_text(json.dumps(raw, separators=(",", ":")))
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "snapshots.csv"
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        write_postgres(rows, raw, db_url)
    else:
        print("postgres: DATABASE_URL is unset or empty, sink skipped", file=sys.stderr)
    for r in rows:
        print(f"{r['ts']} {r['collection']:<26} floor={r['floor_sol']} bid1={r['bid1']} listed={r['listed']} "
              f"fillable<=5%={r['fillable_bids_within_5pct']} sol_eur={sol_eur}")


if __name__ == "__main__":
    main()
