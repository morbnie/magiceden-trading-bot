#!/usr/bin/env python
"""Market report for the configured collection. Reads <data>/out/*.csv and
<data>/raw/, writes analysis/report.md, charts under <data>/out/, and
config/recommended.yaml with the selling parameters the data supports.

Run analysis/fetch_history.py first.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

from me_api import DATA_DIR, load_raw
from settings import COLLECTION, COLLECTIONS, CONFIG, EVENTS, FIAT, LABELS, PHASES, SIGNAL_COLLECTIONS, WATCH_WALLETS

ROOT = Path(__file__).resolve().parent.parent
OUT = DATA_DIR / "out"
REPORT = ROOT / "analysis" / "report.md"
RECOMMENDED = ROOT / "config" / "recommended.yaml"
P = COLLECTION
NAME = LABELS[P]
FIAT_COL = f"sol_{FIAT.lower()}"

# Categorical palette (colour-blind safe), light surface.
C = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a", "yellow": "#eda100",
     "magenta": "#e87ba4", "violet": "#6250d6", "red": "#e34948"}
PALETTE = [C["blue"], C["orange"], C["aqua"], C["yellow"], C["magenta"], C["violet"]]
SERIES = {s: PALETTE[i % len(PALETTE)] for i, s in enumerate(COLLECTIONS)}
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "axes.edgecolor": "#c3c2b7",
    "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
    "axes.spines.right": False, "font.size": 10, "legend.frameon": False, "figure.dpi": 130,
})


def shade_events(ax) -> None:
    for ev in EVENTS:
        try:
            ax.axvspan(pd.Timestamp(ev["start"], tz="UTC"), pd.Timestamp(ev["end"], tz="UTC"),
                       color=C["yellow"], alpha=0.12, lw=0, label=ev.get("label"))
        except (KeyError, ValueError):
            continue


def savefig(fig, name: str) -> str:
    p = OUT / name
    fig.tight_layout()
    fig.savefig(p)
    plt.close(fig)
    try:
        return str(p.relative_to(REPORT.parent))
    except ValueError:
        return str(p)


# ------------------------------------------------------------------ loading

def load() -> dict:
    acts = pd.read_csv(OUT / "activities.csv", parse_dates=["ts"])
    acts["ts"] = pd.to_datetime(acts["ts"], utc=True)
    acts["date"] = acts["ts"].dt.floor("D")
    acts["week"] = acts["date"] - pd.to_timedelta(acts["date"].dt.weekday, unit="D")  # Monday
    prices = pd.read_csv(OUT / "sol_prices.csv", parse_dates=["date"])
    prices["date"] = pd.to_datetime(prices["date"], utc=True)
    prices = prices.set_index("date").sort_index()
    raw = {s: {k: load_raw(k, s) for k in ("stats", "listings", "pools")} for s in COLLECTIONS}
    cg = {s: load_raw(f"nft_{s}", "coingecko") for s in COLLECTIONS}
    holdings = load_raw("buyer_holdings", P) or {}
    watch_tokens = {w: load_raw(f"tokens_{w}", "wallets") or [] for w in WATCH_WALLETS}
    pulled_at = (DATA_DIR / "raw" / "pulled_at.txt").read_text().strip()
    return dict(acts=acts, prices=prices, raw=raw, cg=cg, holdings=holdings, watch_tokens=watch_tokens, pulled_at=pulled_at)


# ------------------------------------------------------- floor reconstruction

def reconstruct_floor(acts: pd.DataFrame, listings: list[dict]) -> pd.DataFrame:
    """Lowest active listing over time, replayed from list/delist/buyNow events and
    anchored on the current listing set (mints listed before the window with no
    list event are assumed active at their current price from the start)."""
    ev = acts[acts["type"].isin(["list", "delist", "buyNow"])].sort_values("blockTime")
    listed_in_window = set(ev.loc[ev["type"] == "list", "tokenMint"])
    active: dict[str, float] = {}
    for L in listings:
        if L["tokenMint"] not in listed_in_window:
            active[L["tokenMint"]] = float(L["price"])
    rows = []
    for e in ev.itertuples():
        others = [v for k, v in active.items() if k != e.tokenMint]
        floor_ex = min(others) if others else np.nan  # competing floor, before this event, excluding this mint
        if e.type == "list":
            active[e.tokenMint] = float(e.price)
        else:
            active.pop(e.tokenMint, None)
        vals = sorted(active.values())
        rows.append((e.ts, vals[0] if vals else np.nan, len(vals), vals[1] if len(vals) > 1 else np.nan,
                     e.signature, e.type, e.tokenMint, floor_ex))
    df = pd.DataFrame(rows, columns=["ts", "floor", "n_active", "second", "signature", "type", "tokenMint", "floor_ex"])
    current = {L["tokenMint"]: float(L["price"]) for L in listings}
    df.attrs["end_mismatch"] = len(set(active) ^ set(current))
    df.attrs["end_actual"] = len(current)
    return df


def daily_floor(fl: pd.DataFrame, start, end) -> pd.Series:
    s = fl.set_index("ts")["floor"].resample("D").last().ffill()
    return s.reindex(pd.date_range(start, end, freq="D", tz="UTC")).ffill()


# ------------------------------------------------------------------ sections

def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    D = load()
    acts, prices = D["acts"], D["prices"]
    sales = acts[acts["type"] == "buyNow"].copy()
    w = acts[acts["collection"] == P]
    ws = sales[sales["collection"] == P].copy()
    if ws.empty:
        raise SystemExit(f"no sales found for {P}; run fetch_history.py first")
    end = acts["ts"].max().floor("D")
    sale_start = ws["ts"].min()
    ndays = (end - sale_start.floor("D")).days + 1
    md: list[str] = []
    rec: dict = {}

    def h(level, text):
        md.append(f"\n{'#' * level} {text}\n")

    def p(text):
        md.append(textwrap.dedent(text).strip() + "\n")

    def table(df: pd.DataFrame, floatfmt=".2f"):
        md.append(df.to_markdown(index=False, floatfmt=floatfmt) + "\n")

    stats = {s: D["raw"][s]["stats"] or {} for s in COLLECTIONS}
    floor_now = (stats[P].get("floorPrice") or 0) / 1e9
    fx_now = float(prices[FIAT_COL].dropna().iloc[-1])
    sol_usd_now = float(prices["sol_usd"].dropna().iloc[-1])

    # ---------------------------------------------------------------- header
    md.append(f"# Market report: {NAME} (`{P}`)\n")
    p(f"""
    Data pulled {D['pulled_at'][:16]} UTC from Magic Eden's public v2 API, Coinbase daily candles and
    CoinGecko's NFT summary. Generated by `analysis/report.py`; every number below is recomputed from
    `out/activities.csv` and the raw cache.
    """)
    h(2, "Data coverage")
    cov = []
    for s in COLLECTIONS:
        a = acts[acts["collection"] == s]
        sl = a[a["type"] == "buyNow"]
        cov.append({"collection": LABELS[s], "symbol": s, "sales (buyNow)": len(sl),
                    "first sale in feed": sl["ts"].min().date().isoformat() if len(sl) else "-",
                    "list events": int((a["type"] == "list").sum()), "delist events": int((a["type"] == "delist").sum()),
                    "poolUpdate events (capped)": int((a["type"] == "poolUpdate").sum()),
                    "floor now (SOL)": (stats[s].get("floorPrice") or 0) / 1e9, "listed now": stats[s].get("listedCount")})
    table(pd.DataFrame(cov))
    p(f"""
    **Magic Eden's public API returns about 90 days of activity** (every type stops near {sale_start.date()}; the
    per-token and per-wallet endpoints are cut at the same point, and `offset` is rejected above ~15,500).
    Everything below describes the {ndays} days from {sale_start.date()} to {end.date()}. Order-book history
    (listings and pools over time) is not available from any public source; `analysis/snapshot_daily.py`
    records it going forward. Configured events{' (none)' if not EVENTS else ''} are shaded in the time charts
    when they fall inside the window.
    """)

    # ------------------------------------------------------- 1 sales volume
    h(2, "1. Sales per week")
    weekly = ws.groupby("week").size()
    full_weeks = weekly.iloc[1:-1] if len(weekly) > 2 else weekly
    per_day = len(ws) / ndays
    fig, ax = plt.subplots(figsize=(10, 3.8))
    wk_all = sales.groupby(["week", "collection"]).size().unstack(1).fillna(0)
    xs = np.arange(len(wk_all))
    n_series = len(COLLECTIONS)
    width = 0.8 / n_series
    for i, s in enumerate(COLLECTIONS):
        if s in wk_all:
            ax.bar(xs + (i - (n_series - 1) / 2) * width, wk_all[s], width=width * 0.92, color=SERIES[s], label=LABELS[s], linewidth=0)
    ax.set_xticks(xs)
    ax.set_xticklabels([d.strftime("%d %b") for d in wk_all.index], rotation=0, fontsize=8)
    ax.set_ylabel("sales per week (Mon–Sun)")
    ax.set_title("Weekly sales (first and last week partial)", loc="left", color=INK2)
    if n_series > 1:
        ax.legend(loc="upper left", ncol=min(n_series, 4))
    ax.grid(axis="x", visible=False)
    f1 = savefig(fig, "weekly_sales.png")
    md.append(f"![weekly sales]({f1})\n")
    cg = D["cg"].get(P) or {}
    extra = ""
    if SIGNAL_COLLECTIONS:
        extra = " Signal collections sold " + ", ".join(f"{LABELS[s]} {len(sales[sales.collection == s])}" for s in SIGNAL_COLLECTIONS) + " in the same span."
    p(f"""
    {NAME} cleared **{len(ws)} units in {ndays} days: {per_day:.2f} a day, {per_day * 7:.1f} a week.** Full weeks ranged
    {full_weeks.min():.0f} to {full_weeks.max():.0f} (median {full_weeks.median():.0f}).{extra}
    {f"CoinGecko reports {int(cg.get('one_day_sales') or 0)} sales in the last 24 hours and {int(cg.get('number_of_unique_addresses') or 0)} owners." if cg else ""}
    """)
    rec["weekly_sales"] = round(per_day * 7, 1)

    # ------------------------------------------------------ 2 floor path
    h(2, f"2. Floor path in SOL and {FIAT}")
    fl = {s: reconstruct_floor(acts[acts["collection"] == s], D["raw"][s]["listings"] or []) for s in COLLECTIONS}
    flp = fl[P]
    dfloor = daily_floor(flp, sale_start.floor("D"), end)
    sale_daily = ws[ws["source"] == "magiceden_v2"].groupby("date")["price"].agg(["min", "median", "count"]).reindex(dfloor.index)
    px = prices.reindex(dfloor.index).ffill()
    floor_fiat = dfloor * px[FIAT_COL]
    floor_usd = dfloor * px["sol_usd"]
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True)
    ax = axes[0]
    ax.plot(dfloor.index, dfloor, color=SERIES[P], lw=2, label="reconstructed floor (lowest active ask)")
    ax.scatter(sale_daily.index, sale_daily["min"], s=18, color=C["orange"], zorder=3, label="lowest listing sale that day")
    ws_mmm = ws[ws["source"] == "mmm"]
    ax.scatter(ws_mmm["date"], ws_mmm["price"], s=14, color=C["violet"], marker="v", zorder=3, label="pool fill (bid hit)")
    shade_events(ax)
    ax.set_ylabel("SOL")
    ax.set_title(f"{NAME} floor, SOL", loc="left", color=INK2)
    ax.legend(loc="upper left", ncol=3, fontsize=8.5)
    ax = axes[1]
    ax.plot(floor_fiat.index, floor_fiat, color=SERIES[P], lw=2)
    shade_events(ax)
    ax.set_ylabel(FIAT)
    ax.set_title(f"{NAME} floor, {FIAT} (floor × daily SOL/{FIAT} close, Coinbase)", loc="left", color=INK2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    f2 = savefig(fig, "floor_path.png")
    md.append(f"![floor path]({f2})\n")
    first_good = flp["ts"].min() + pd.Timedelta(days=14)
    lw = ws[ws.source == "magiceden_v2"]
    has_prior_list = lw["tokenMint"].isin(set(w.loc[w["type"] == "list", "tokenMint"]))
    p(f"""
    Method: list, delist and buyNow events replayed in order, anchored on the {flp.attrs['end_actual']} listings live at pull
    time (end-state mismatch after replay: {flp.attrs['end_mismatch']} mints). Listings that were already live before
    {sale_start.date()} and left again inside the window never show a price, so the first two weeks of the curve are
    an upper bound; {int(has_prior_list.sum())} of {len(lw)} listing sales ({has_prior_list.mean():.0%}) have a visible
    prior list event. Pool fills (triangles) sit below the ask floor by construction.

    | | SOL | {FIAT} | USD |
    | --- | --- | --- | --- |
    | floor at pull time | {floor_now:.2f} | {floor_now * fx_now:,.0f} | {floor_now * sol_usd_now:,.0f} |
    | 30-day median daily floor | {dfloor.iloc[-30:].median():.2f} | {floor_fiat.iloc[-30:].median():,.0f} | {floor_usd.iloc[-30:].median():,.0f} |
    | {ndays}-day min / max daily floor | {dfloor.min():.2f} / {dfloor.max():.2f} | {floor_fiat.min():,.0f} / {floor_fiat.max():,.0f} | {floor_usd.min():,.0f} / {floor_usd.max():,.0f} |
    | SOL/{FIAT}, SOL/USD at pull | | {fx_now:.2f} | {sol_usd_now:.2f} |

    The floor moved {dfloor.min():.2f}–{dfloor.max():.2f} SOL ({(dfloor.max() / dfloor.min() - 1):.0%} range) while
    SOL/{FIAT} moved {px[FIAT_COL].min():.0f}–{px[FIAT_COL].max():.0f} ({(px[FIAT_COL].max() / px[FIAT_COL].min() - 1):.0%} range).
    """)
    rec["floor_30d_median_sol"] = round(float(dfloor.iloc[-30:].median()), 2)
    rec[f"floor_30d_median_{FIAT.lower()}"] = round(float(floor_fiat.iloc[-30:].median()), 0)
    rec["floor_window_min_sol"] = round(float(dfloor.min()), 2)

    # ---------------------------------------------- 3 pool fills vs listing buys
    h(2, "3. Pool fills versus listing buys")
    src = ws.groupby("source").agg(n=("price", "size"), avg_price=("price", "mean"), median_price=("price", "median"))
    src["share"] = src["n"] / src["n"].sum()
    src = src.rename(index={"mmm": "pool fill (seller hit a bid)", "magiceden_v2": "listing buy (buyer took an ask)"}).reset_index()
    table(src, ".3f")
    wk_src = ws.groupby(["week", "source"]).size().unstack(1).fillna(0)
    key = ["signature", "type", "tokenMint"]
    ws = ws.merge(flp[key + ["floor_ex"]].drop_duplicates(key), on=key, how="left").rename(columns={"floor_ex": "floor_before"})
    ws["disc"] = ws["price"] / ws["floor_before"] - 1  # vs the competing floor at that moment, excluding the unit itself
    mm = ws[(ws.source == "mmm") & (ws.ts >= first_good)].dropna(subset=["disc"])
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), gridspec_kw={"width_ratios": [1.6, 1]})
    ax = axes[0]
    xs = np.arange(len(wk_src))
    ax.bar(xs, wk_src.get("magiceden_v2", 0), width=0.6, color=SERIES[P], label="listing buy", linewidth=0)
    ax.bar(xs, wk_src.get("mmm", 0), width=0.6, bottom=wk_src.get("magiceden_v2", 0), color=C["violet"], label="pool fill", linewidth=0)
    ax.set_xticks(xs)
    ax.set_xticklabels([d.strftime("%d %b") for d in wk_src.index], fontsize=8)
    ax.set_title("Sales per week by path", loc="left", color=INK2)
    ax.legend(loc="upper left")
    ax.grid(axis="x", visible=False)
    ax = axes[1]
    if len(mm):
        ax.hist(mm["disc"] * 100, bins=np.arange(-12, 2.5, 1), color=C["violet"], linewidth=0)
    ax.set_xlabel("pool fill price vs ask floor at the time, %")
    ax.set_title("Discount sellers accepted on bids", loc="left", color=INK2)
    ax.grid(axis="x", visible=False)
    f3 = savefig(fig, "sales_by_path.png")
    md.append(f"![sales by path]({f3})\n")
    share_v2 = (ws.source == "magiceden_v2").mean()
    if len(mm):
        disc_q = (-mm["disc"]).quantile([0.25, 0.5, 0.75]) * 100  # positive = below the ask floor
        bid_text = (f"Sellers who hit bids sold a median **{disc_q[0.5]:.1f}% below the ask floor** at that moment "
                    f"(quartiles {disc_q[0.25]:.1f}% to {disc_q[0.75]:.1f}%, n={len(mm)}, first two weeks excluded, measured against the "
                    f"cheapest listing other than the unit itself); {(mm['disc'] >= 0).mean():.0%} of fills were at or above that floor. "
                    f"A take-bid band of 2% would have covered {(mm['disc'] >= -0.02).mean():.0%} of those fills, a 3% band {(mm['disc'] >= -0.03).mean():.0%}.")
        rec["pool_fill_median_discount_pct"] = round(float(disc_q[0.5]), 2)
        rec["band_2pct_coverage_pct"] = round(100 * float((mm['disc'] >= -0.02).mean()), 1)
    else:
        bid_text = "No pool fills in the window, so the bid-side discount could not be measured."
        rec["pool_fill_median_discount_pct"] = None
    p(f"""
    **{share_v2:.0%} of sales were buyers taking an ask; {1 - share_v2:.0%} were sellers hitting a pool bid.**
    Only the first group is reachable by listing at the floor, so the organic flow a floor listing competes for is about
    {share_v2 * per_day * 7:.1f} units a week. {bid_text}
    """)
    rec["share_listing_buys"] = round(float(share_v2), 3)

    # ------------------------------------------------- 4 time listing -> sale
    h(2, "4. Time from listing to sale")
    lists = w[w["type"] == "list"].sort_values("blockTime")
    lists = lists.merge(flp[key + ["floor_ex"]].drop_duplicates(key), on=key, how="left").rename(columns={"floor_ex": "floor_before"})
    lists["premium"] = lists["price"] / lists["floor_before"].where(lists["floor_before"] > 0) - 1
    lists.loc[lists["premium"] < 0, "premium"] = 0  # a new floor is premium 0
    lw2 = lw.sort_values("blockTime")
    m = pd.merge_asof(lw2[["ts", "tokenMint", "seller", "price", "blockTime"]].rename(columns={"ts": "sale_ts", "price": "sale_price"}),
                      lists[["ts", "tokenMint", "price", "premium", "blockTime"]].rename(columns={"ts": "list_ts", "price": "list_price"}),
                      on="blockTime", by="tokenMint", direction="backward", allow_exact_matches=False)
    m = m.dropna(subset=["list_ts"])
    m["hours"] = (m["sale_ts"] - m["list_ts"]).dt.total_seconds() / 3600
    bins = [-0.001, 0.01, 0.04, 0.10, np.inf]
    labels = ["at floor (≤1%)", "1–4% above", "4–10% above", ">10% above"]
    m["bucket"] = pd.cut(m["premium"], bins=bins, labels=labels)
    lists["bucket"] = pd.cut(lists["premium"], bins=bins, labels=labels)
    tts = m.groupby("bucket", observed=False)["hours"].agg(n="size", median_h="median", p75_h=lambda x: x.quantile(0.75)).reset_index()
    tts["median_days"] = tts["median_h"] / 24
    table(tts[["bucket", "n", "median_h", "median_days", "p75_h"]], ".1f")
    fig, ax = plt.subplots(figsize=(8, 3.4))
    data = [m.loc[m.bucket == b, "hours"].clip(upper=24 * 30).values for b in labels]
    ax.boxplot(data, tick_labels=labels, orientation="horizontal", widths=0.5, patch_artist=True,
               boxprops=dict(facecolor=SERIES[P], alpha=0.35, linewidth=1), medianprops=dict(color=SERIES[P], lw=2),
               whiskerprops=dict(color=MUTED), capprops=dict(color=MUTED), flierprops=dict(marker="o", markersize=3, markerfacecolor=MUTED, markeredgecolor="none"))
    ax.set_xscale("log")
    ax.set_xlabel("hours from last (re)listing to sale, log scale, capped at 30 days")
    ax.set_title("Listing-to-sale time by premium over the floor at listing time", loc="left", color=INK2)
    f4 = savefig(fig, "time_to_sale.png")
    md.append(f"![time to sale]({f4})\n")
    at_floor = m[m.bucket == labels[0]]
    med_floor_h = float(at_floor["hours"].median()) if len(at_floor) else float("nan")
    p(f"""
    Measured from the last list or reprice event of that mint to its sale (n={len(m)} listing sales with a visible list
    event; {len(lw) - len(m)} were listed before the window). A unit listed at the floor sold after a median
    **{med_floor_h:.0f} hours** ({med_floor_h / 24:.1f} days); a quarter took longer than
    {(at_floor['hours'].quantile(0.75) / 24) if len(at_floor) else float('nan'):.1f} days. Note the selection: these are the listings
    that did sell. Section 8 gives the sell-through rate, which is the other half of the answer.
    """)
    rec["median_hours_floor_to_sale"] = None if np.isnan(med_floor_h) else round(med_floor_h, 1)

    # --------------------------------------------------- 5 buyers and signal
    h(2, "5. Distinct buyers" + (" and the cross-collection signal" if SIGNAL_COLLECTIONS else ""))
    organic = lw.copy()
    bw = organic.groupby("week")["buyer"].nunique()
    pool_owners = set(ws.loc[ws.source == "mmm", "buyer"])
    buyers = organic["buyer"].unique()
    fig, axes = plt.subplots(1, 2 if SIGNAL_COLLECTIONS else 1, figsize=(10 if SIGNAL_COLLECTIONS else 7, 3.4),
                             gridspec_kw={"width_ratios": [1.6, 1]} if SIGNAL_COLLECTIONS else None, squeeze=False)
    ax = axes[0][0]
    xs = np.arange(len(bw))
    ax.bar(xs, bw.values, width=0.6, color=SERIES[P], linewidth=0)
    ax.set_xticks(xs[::2])
    ax.set_xticklabels([d.strftime("%d %b") for d in bw.index[::2]], fontsize=8)
    ax.set_title("Distinct wallets buying listings per week (week starting)", loc="left", color=INK2)
    ax.grid(axis="x", visible=False)
    signal_text = ""
    if SIGNAL_COLLECTIONS:
        other = sales[sales.collection.isin(SIGNAL_COLLECTIONS)]
        cross_before = 0
        cross_any_wallets = set()
        for b, t in organic[["buyer", "ts"]].itertuples(index=False):
            o = other[other.buyer == b]
            if len(o):
                cross_any_wallets.add(b)
                if ((o.ts < t) & (o.ts >= t - pd.Timedelta(days=30))).any():
                    cross_before += 1
        hold_rows = []
        for b in buyers:
            toks = D["holdings"].get(b)
            if toks is None:
                continue
            cols = {t["collection"] for t in toks}
            hold_rows.append({"buyer": b, "all": all(s in cols for s in SIGNAL_COLLECTIONS),
                              "none": not any(s in cols for s in SIGNAL_COLLECTIONS)})
        hr = pd.DataFrame(hold_rows)
        ax = axes[0][1]
        if len(hr):
            cats = ["holds all signal collections", "holds some", "holds none"]
            vals = [int(hr["all"].sum()), int((~hr["all"] & ~hr["none"]).sum()), int(hr["none"].sum())]
            ax.barh(cats[::-1], vals[::-1], color=SERIES[P], linewidth=0, height=0.6)
            for i, v in enumerate(vals[::-1]):
                ax.text(v + 0.5, i, str(v), va="center", color=INK2, fontsize=9)
            ax.set_title("What buyers hold today", loc="left", color=INK2)
            ax.grid(axis="y", visible=False)
        both = int(hr["all"].sum()) if len(hr) else 0
        signal_text = (f"\n\nCross-collection flow inside the window: **{cross_before} of {len(lw)} listing buys "
                       f"({cross_before / max(len(lw), 1):.0%}) came from a wallet that had bought a signal collection in the previous 30 days**; "
                       f"{len(cross_any_wallets)} of the {len(buyers)} wallets bought one at some point in the window. Holdings today "
                       f"({len(hr)} buyers checked): **{both} of {len(hr)} ({both / max(len(hr), 1):.0%}) hold every signal collection.**")
        rec["buyers_holding_all_signals_pct"] = round(100 * both / max(len(hr), 1), 1)
        rec["cross_buy_30d_pct"] = round(100 * cross_before / max(len(lw), 1), 1)
    f5 = savefig(fig, "buyers.png")
    md.append(f"![buyers]({f5})\n")
    p(f"""
    {len(buyers)} distinct wallets bought a listing in {ndays} days ({len(lw)} purchases; median {bw.median():.0f} buyers a week).
    Pool fills were bought by {len(pool_owners)} pool-owner wallets, which are market makers rather than end buyers.{signal_text}
    """)

    # ------------------------------------------------------ 6 fiat anchoring
    h(2, f"6. {FIAT} anchoring test")
    good = dfloor.index >= first_good
    df6 = pd.DataFrame({"floor_sol": dfloor, "fx": px[FIAT_COL], "sol_usd": px["sol_usd"]})[good].dropna()
    df6["floor_fiat"] = df6["floor_sol"] * df6["fx"]
    wk6 = df6.resample("W").last().dropna()
    lr = np.log(wk6).diff().dropna()
    corr_lvl = df6["floor_sol"].corr(df6["sol_usd"])
    corr_chg = lr["floor_sol"].corr(lr["fx"]) if len(lr) > 2 else np.nan
    beta = np.polyfit(lr["fx"], lr["floor_sol"], 1)[0] if len(lr) > 3 else np.nan
    cv_sol = df6["floor_sol"].std() / df6["floor_sol"].mean()
    cv_fiat = df6["floor_fiat"].std() / df6["floor_fiat"].mean()
    span_sol = df6["floor_sol"].iloc[-1] / df6["floor_sol"].iloc[0] - 1
    span_fiat = df6["floor_fiat"].iloc[-1] / df6["floor_fiat"].iloc[0] - 1
    span_fx = df6["fx"].iloc[-1] / df6["fx"].iloc[0] - 1
    beta_span = np.log1p(span_sol) / np.log1p(span_fx) if abs(span_fx) > 0.05 else np.nan
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    ax = axes[0]
    base = df6.iloc[0]
    ax.plot(df6.index, df6["floor_sol"] / base["floor_sol"] * 100, color=SERIES[P], lw=2, label="floor in SOL")
    ax.plot(df6.index, df6["floor_fiat"] / base["floor_fiat"] * 100, color=C["orange"], lw=2, label=f"floor in {FIAT}")
    ax.plot(df6.index, df6["fx"] / base["fx"] * 100, color=MUTED, lw=1.5, ls="--", label=f"SOL/{FIAT}")
    ax.set_ylabel(f"index, {df6.index[0].date()} = 100")
    ax.set_title("Which denomination is the floor stable in?", loc="left", color=INK2)
    ax.legend(loc="upper left", fontsize=8.5)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax = axes[1]
    ax.scatter(lr["fx"] * 100, lr["floor_sol"] * 100, s=28, color=SERIES[P])
    if not np.isnan(beta):
        xx = np.linspace(lr["fx"].min() * 100, lr["fx"].max() * 100, 10)
        ax.plot(xx, beta * xx + np.polyfit(lr["fx"], lr["floor_sol"], 1)[1] * 100, color=C["orange"], lw=1.5)
    ax.axhline(0, color=GRID)
    ax.axvline(0, color=GRID)
    ax.set_xlabel(f"weekly change in SOL/{FIAT}, %")
    ax.set_ylabel("weekly change in floor (SOL), %")
    ax.set_title(f"Elasticity {beta:.2f} ({FIAT}-anchored = −1, SOL-anchored = 0)", loc="left", color=INK2)
    f6 = savefig(fig, "fiat_anchoring.png")
    md.append(f"![fiat anchoring]({f6})\n")
    score = int(corr_chg <= -0.5) + int(cv_fiat < cv_sol) + int((not np.isnan(beta)) and beta <= -0.5)
    verdict = FIAT if score >= 2 else "SOL"
    absorbed = -beta_span if not np.isnan(beta_span) else np.nan
    p(f"""
    Over {len(df6)} days ({len(lr)} weekly changes): correlation of floor-in-SOL with SOL/USD levels **{corr_lvl:+.2f}**, correlation of
    weekly log changes floor-vs-SOL/{FIAT} **{corr_chg:+.2f}**, elasticity **{beta:+.2f}** (a floor that fully re-prices to a fixed {FIAT}
    value shows −1). Coefficient of variation of the floor: **{cv_sol:.1%} in SOL versus {cv_fiat:.1%} in {FIAT}**.

    Over the whole span SOL/{FIAT} moved {span_fx:+.0%} while the floor moved {span_sol:+.0%} in SOL and {span_fiat:+.0%} in {FIAT}, so the
    SOL floor absorbed about {absorbed:.0%} of the currency move (a {FIAT}-anchored market absorbs 100%, a SOL-anchored one 0%).

    Verdict on this sample: **closer to {verdict}** ({score} of 3 tests point to {FIAT}: weekly-change correlation ≤ −0.5,
    lower variation in {FIAT}, elasticity ≤ −0.5). A reservation price should be kept in both units; the binding one is
    whichever is higher at the time. Treat the verdict as provisional on fewer than six months of data.
    """)
    rec["denomination"] = verdict.lower()
    rec["fiat_elasticity"] = None if np.isnan(beta) else round(float(beta), 2)
    rec["cv_floor_sol_pct"] = round(100 * float(cv_sol), 1)
    rec[f"cv_floor_{FIAT.lower()}_pct"] = round(100 * float(cv_fiat), 1)

    # ------------------------------------------------------- 7 market makers
    h(2, "7. Market makers and bots")
    pu = w[w["type"] == "poolUpdate"].copy()
    pu["wallet"] = pu["seller"].fillna("").where(pu["seller"].fillna("") != "", pu["buyer"].fillna(""))
    pool_buyers = ws[ws.source == "mmm"].groupby("buyer").size().sort_values(ascending=False)
    tagged = {wal: note for wal, note in WATCH_WALLETS.items()}
    for wal in pool_buyers.index[:5]:
        tagged.setdefault(wal, "auto: frequent pool-fill buyer")
    rows = []
    for wal, note in tagged.items():
        bought = ws[ws.buyer == wal]
        sold = ws[ws.seller == wal]
        rt = bought.merge(sold, on="tokenMint", suffixes=("_b", "_s"))
        rt = rt[rt.ts_s > rt.ts_b]
        hold_days = ((rt.ts_s - rt.ts_b).dt.total_seconds() / 86400).median() if len(rt) else np.nan
        spread = ((rt.price_s / rt.price_b - 1).median() * 100) if len(rt) else np.nan
        toks = D["watch_tokens"].get(wal, [])
        inv = sum(t.get("collection") == P for t in toks) if toks else None
        pu_w = pu[pu.wallet == wal]
        pu_days = max((pu["ts"].max() - pu["ts"].min()).total_seconds() / 86400, 1e-9) if len(pu) else np.nan
        rows.append({"wallet": wal[:6] + "…", "note": note, "bought": len(bought), "of which pool fills": int((bought.source == "mmm").sum()),
                     "sold": len(sold), "round trips": len(rt), "median hold (days)": hold_days, "median spread %": spread,
                     "held now": inv if inv is not None else "-", "pool updates per day": (len(pu_w) / pu_days) if len(pu_w) else np.nan})
    comp = pd.DataFrame(rows)
    if len(comp):
        table(comp, ".1f")
    inv_share = (ws.buyer.isin(tagged) | ws.seller.isin(tagged)).mean()
    if len(pu):
        pu_daily = pu.groupby(["date", "wallet"]).size().unstack(1).fillna(0)
        top_pu = pu_daily.sum().sort_values(ascending=False).head(5).index
        fig, ax = plt.subplots(figsize=(10, 3.2))
        for i, wal in enumerate(top_pu):
            ax.plot(pu_daily.index, pu_daily[wal], lw=2, color=PALETTE[i % len(PALETTE)], label=wal[:6] + "…")
        ax.set_ylabel("pool updates per day")
        ax.set_title("Who moves the bid side: pool updates per wallet per day (feed capped at 15,500 events)", loc="left", color=INK2)
        ax.legend(loc="upper left", ncol=5, fontsize=8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        f7 = savefig(fig, "market_makers.png")
        md.append(f"![market makers]({f7})\n")
    rt_all = ws.merge(ws, on="tokenMint", suffixes=("_b", "_s"))
    rt_all = rt_all[(rt_all.buyer_b == rt_all.seller_s) & (rt_all.ts_s > rt_all.ts_b) & rt_all.buyer_b.isin(pool_owners)]
    rt_hold = ((rt_all.ts_s - rt_all.ts_b).dt.total_seconds() / 86400)
    rt_spread = (rt_all.price_s / rt_all.price_b - 1) * 100
    flip_share = len(rt_all) / max(len(lw), 1)
    p(f"""
    The tagged wallets were on one side of **{inv_share:.0%} of all sales**. Across all pool owners, {len(rt_all)} round trips
    (bought from a bid, resold through a listing) closed inside the window, median hold **{rt_hold.median() if len(rt_all) else float('nan'):.1f} days**,
    median gross spread **{rt_spread.median() if len(rt_all) else float('nan'):.1f}%** before fees. That is **{flip_share:.0%} of all listing sales**:
    the asks sitting at the floor are, to that extent, resales of units bought from bids, which is what a seller at the floor
    competes with.
    """)
    rec["market_maker_share_of_sales_pct"] = round(100 * float(inv_share), 1)
    rec["flip_share_of_listing_sales_pct"] = round(100 * float(flip_share), 1)

    # ------------------------------------------------- 8 depth above floor
    h(2, "8. Depth above the floor: do units priced above the floor sell?")
    sold_mints = set(zip(m["tokenMint"], m["list_ts"]))
    lists["sold"] = [(mint, ts) in sold_mints for mint, ts in zip(lists["tokenMint"], lists["ts"])]
    lw_good = lists[(lists.ts >= first_good) & (lists.ts <= end - pd.Timedelta(days=7))]
    st = lw_good.groupby("bucket", observed=False).agg(listings=("sold", "size"), sold_within_window=("sold", "sum")).reset_index()
    st["sell_through"] = st["sold_within_window"] / st["listings"].replace(0, np.nan)
    table(st, ".2f")
    prem_sales = m.groupby("bucket", observed=False).size().rename("sales").reset_index()
    prem_sales["share"] = prem_sales["sales"] / max(prem_sales["sales"].sum(), 1)
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.4))
    ax = axes[0]
    ax.bar(prem_sales["bucket"].astype(str), prem_sales["sales"], color=SERIES[P], width=0.6, linewidth=0)
    ax.set_title("Listing sales by premium over floor at listing time", loc="left", color=INK2)
    ax.grid(axis="x", visible=False)
    ax.tick_params(axis="x", labelsize=8)
    ax = axes[1]
    ax.bar(st["bucket"].astype(str), st["sell_through"].fillna(0) * 100, color=C["orange"], width=0.6, linewidth=0)
    ax.set_ylabel("% of listings that sold")
    ax.set_title("Sell-through by premium (listings ≥7 days old)", loc="left", color=INK2)
    ax.grid(axis="x", visible=False)
    ax.tick_params(axis="x", labelsize=8)
    f8 = savefig(fig, "depth_above_floor.png")
    md.append(f"![depth above floor]({f8})\n")
    sh = dict(zip(prem_sales["bucket"].astype(str), prem_sales["share"]))
    stt = dict(zip(st["bucket"].astype(str), st["sell_through"]))
    p(f"""
    **{sh.get(labels[0], 0):.0%} of listing sales were units at the floor, {sh.get(labels[1], 0):.0%} were 1–4% above,
    {sh.get(labels[2], 0):.0%} were 4–10% above and {sh.get(labels[3], 0):.0%} more than 10% above.** Sell-through of listings older
    than a week: {stt.get(labels[0], np.nan):.0%} at the floor, {stt.get(labels[1], np.nan):.0%} at 1–4%, {stt.get(labels[2], np.nan):.0%} at
    4–10%, {stt.get(labels[3], np.nan):.0%} above 10%. Premium is measured against the floor when the unit was listed; a unit listed
    above the floor often sells later because the floor rose to meet it, which is how a second ladder slot earns its place.
    """)
    rec["share_sales_at_floor_pct"] = round(100 * float(sh.get(labels[0], 0)), 1)
    rec["share_sales_1_4_pct"] = round(100 * float(sh.get(labels[1], 0)), 1)
    rec["share_sales_4_10_pct"] = round(100 * float(sh.get(labels[2], 0)), 1)

    # ------------------------------------------------ current book snapshot
    h(2, "Order book at pull time")
    L = sorted(float(x["price"]) for x in D["raw"][P]["listings"] or [])
    bids = []
    for pl in D["raw"][P]["pools"] or []:
        if pl.get("poolType") not in ("buy_sided", "two_sided"):
            continue
        spot = (pl.get("spotPrice") or 0) / 1e9
        esc = pl.get("buysidePaymentAmount") or 0
        shared = (pl.get("sharedEscrowInfo") or {}).get("sharedEscrowBuysidePaymentAmount")
        esc = min(esc, shared) if shared is not None else esc
        esc /= 1e9
        if spot > 0 and esc >= spot:
            bids.append((spot, esc, int(esc // spot), pl["poolOwner"][:6] + "…"))
    bids.sort(reverse=True)
    within5 = [b for b in bids if b[0] >= floor_now * 0.95]
    p(f"""
    Asks ({len(L)}): {', '.join(f'{x:g}' for x in L[:12])}{' …' if len(L) > 12 else ''}. Units within 2% of the floor: {sum(1 for x in L if x <= floor_now * 1.02)}.

    Funded bids ({len(bids)} pools): top five {', '.join(f'{b[0]:.3f} ({b[3]}, {b[2]} fill{"s" if b[2] != 1 else ""})' for b in bids[:5]) or 'none'}.
    Escrow within 5% of the floor: **{sum(b[1] for b in within5):.1f} SOL across {len(within5)} pools, enough for {sum(b[2] for b in within5)} fills.**
    """)

    # --------------------------------------------------- recommendations
    h(2, "Recommended parameters")
    med30 = rec["floor_30d_median_sol"]
    med30_fiat = rec[f"floor_30d_median_{FIAT.lower()}"]
    r_fiat = round(0.9 * med30_fiat / 10) * 10
    r_sol = round(0.9 * med30, 2)
    r_sol_min = round(0.85 * med30, 2)
    band = float(np.clip(round(rec["pool_fill_median_discount_pct"] or 2.0, 1), 1.0, 3.0))
    st_map = dict(zip(st["bucket"].astype(str), st["sell_through"].fillna(0)))
    ladder = [0, 3]
    if st_map.get(labels[1], 0) >= st_map.get(labels[0], 0) - 0.10:
        ladder = [0, 4]
    if st_map.get(labels[2], 0) >= st_map.get(labels[0], 0) - 0.10:
        ladder = [0, 6]
    phases = {name: dict(v) for name, v in PHASES.items()}
    first_phase = next(iter(phases), None)
    if first_phase:
        phases[first_phase]["ladder_pct"] = ladder
    recommended = {
        "generated": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%MZ"),
        "collection": P,
        "data_window": f"{sale_start.date()} to {end.date()} (Magic Eden public API keeps ~90 days)",
        "objective": {
            "denomination": rec["denomination"],
            f"reservation_{FIAT.lower()}": r_fiat,
            "reservation_sol": r_sol,
            "reservation_rule_pct_of_30d_median": 90,
            "reservation_sol_min": r_sol_min,
        },
        "phases": phases,
        "bids": {"take_bid_band_pct": band},
        "sell": {"tick_sol": CONFIG.get("sell", {}).get("tick_sol", 0.0001), "reprice_threshold_pct": CONFIG.get("sell", {}).get("reprice_threshold_pct", 0.5)},
        "breakers": {"floor_drop_24h_pct": CONFIG.get("breakers", {}).get("floor_drop_24h_pct", 15)},
        "evidence": rec,
        "justification": {
            "denomination": f"Section 6: elasticity {rec['fiat_elasticity']}, CV floor {rec['cv_floor_sol_pct']}% in SOL vs {rec[f'cv_floor_{FIAT.lower()}_pct']}% in {FIAT}; provisional.",
            f"reservation_{FIAT.lower()}": f"90% of the 30-day median floor in {FIAT} ({med30_fiat:,.0f}), rounded to 10.",
            "reservation_sol": f"90% of the 30-day median floor in SOL ({med30}). The engine uses max(reservation in the chosen unit, reservation_sol_min).",
            "reservation_sol_min": f"85% of the 30-day median floor in SOL ({med30}); the window's minimum daily floor was {rec['floor_window_min_sol']}. Backstop only.",
            "ladder": f"Section 8: second slot placed at the highest premium bucket whose sell-through stayed within 10 points of the at-floor rate ({ladder[1]}%).",
            "take_bid_band_pct": (f"Sections 3/4: sellers who hit bids sold a median {rec['pool_fill_median_discount_pct']}% below the floor; a floor listing takes a median "
                                  f"{rec['median_hours_floor_to_sale']} hours. The band sits at the median observed discount, clipped to 1–3%."),
            "floor_drop_24h_pct": f"Largest 24h floor drop in the window was {abs(float(dfloor.pct_change().min())) * 100:.1f}%.",
        },
    }
    RECOMMENDED.write_text("# Written by analysis/report.py. Review before copying values into your config.\n"
                           + yaml.safe_dump(recommended, sort_keys=False, allow_unicode=True, width=120))
    p(f"""
    Written to `config/recommended.yaml`. Summary:

    | Parameter | Value | Basis |
    | --- | --- | --- |
    | denomination | {rec['denomination']} | section 6, provisional |
    | reservation_{FIAT.lower()} | {r_fiat:,} | 90% of 30-day median floor in {FIAT} ({med30_fiat:,.0f}) |
    | reservation_sol | {r_sol} | 90% of 30-day median floor in SOL ({med30}) |
    | reservation_sol_min | {r_sol_min} | 85% of 30-day median floor in SOL |
    | ladder (first phase) | {ladder} | section 8 sell-through |
    | take_bid_band_pct | {band} | median discount of observed bid hits (section 3) |
    """)

    REPORT.write_text("\n".join(md))
    print(f"wrote {REPORT} and {RECOMMENDED}")


if __name__ == "__main__":
    main()
