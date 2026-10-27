# Magic Eden trading bot

A trading bot, in progress, for selling units of one Solana NFT collection on
Magic Eden patiently and at a good price rather than dumping them into the
bid side. The market-data and analysis half is done and read-only; the
execution half (listing, repricing, taking bids through Magic Eden's
`/instructions` API) is designed and waiting on an API key. Everything is
keyed by the collection's Magic Eden symbol and configured in one YAML file.

What is here today, all read-only and working against the public API:

- **Market data pull** (`analysis/fetch_history.py`): the ~90 days of
  activity Magic Eden's public API keeps, listings, pools, and daily SOL
  prices in EUR and USD.
- **Hourly order-book snapshot** (`analysis/snapshot_daily.py`): floor, ask
  ladder, funded bids and escrow depth, stored as files and optionally in
  Postgres. Magic Eden keeps no order-book history, so this is how a seller
  builds their own.
- **Market report** (`analysis/report.py`): sales rate, reconstructed floor
  path in SOL and fiat, listing buys versus pool fills, time from listing to
  sale, buyer counts, a fiat-anchoring test, market-maker activity and
  sell-through by premium over the floor. It writes `config/recommended.yaml`
  with the selling parameters the data supports and a justification for each.
- **Deployment** for the snapshot job on Railway or a small VPS (`docs/deploy.md`).

What is planned (see "Roadmap"): a listing manager that keeps a small number
of units listed one tick above the competing floor, reprices when the floor
moves, and accepts a collection bid when it is within a configured band of
the floor, using Magic Eden's `/instructions` endpoints. That part needs a
Magic Eden API key and does not exist yet.

## Layout

```
README.md
config/default.yaml        all settings: collection symbol(s), labels, events, engine parameters
config/recommended.yaml    written by analysis/report.py (gitignored)
docs/verified.md           API behaviour checked against live calls, with evidence
docs/deploy.md             running the snapshot job on Railway or a VPS
deploy/
  railway/                 Dockerfile for the hourly snapshot cron service
  bootstrap.sh             VPS alternative: Ubuntu hardening, Docker, venv, cron
.railway/railway.ts        Railway Infrastructure as Code partial (service + volume)
analysis/
  settings.py              loads the config
  me_api.py                Magic Eden v2 read client: throttle, retries, raw cache
  fetch_history.py         data pull (Magic Eden, Coinbase, CoinGecko)
  snapshot_daily.py        hourly snapshot: listings, pools, stats, SOL spot
  report.py                builds analysis/report.md, charts, config/recommended.yaml
  raw/                     cached raw JSON (gitignored)
  out/                     normalised CSVs and charts (gitignored)
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r analysis/requirements.txt
# edit config/default.yaml: set `collection` to your Magic Eden symbol
.venv/bin/python analysis/fetch_history.py        # a few minutes, public endpoints only
.venv/bin/python analysis/report.py               # writes analysis/report.md and config/recommended.yaml
```

`fetch_history.py --offline` rebuilds the tables from the cache without
touching the network. Set `BOT_CONFIG` to use a different config file and
`BOT_DATA_DIR` to move the raw cache and outputs (for example to a mounted
volume).

Optional: `COINGECKO_API_KEY` (a free Demo key) adds 365 days of daily floor
and volume from CoinGecko's NFT market chart for the collections that have a
`coingecko_nft_ids` entry.

## Hourly snapshot

`analysis/snapshot_daily.py` runs once and exits; schedule it hourly on an
always-on host. [docs/deploy.md](docs/deploy.md) covers a Railway cron
service and a VPS. Locally:

```
7 * * * * cd /path/to/repo && .venv/bin/python analysis/snapshot_daily.py >> analysis/out/snapshot.log 2>&1
```

Rows accumulate in `<data>/out/snapshots.csv`, raw payloads under
`<data>/raw/snapshots/`. With `DATABASE_URL` set the rows also go to Postgres
(`snapshots`, plus the full payload in `snapshots_raw`).

## Rate limits and API usage

The scripts stay under Magic Eden's public limit of 120 requests a minute:
the client throttles to 100 a minute, retries on 429 with the server's
`retry-after`, and caches every response. A full history pull is a few
hundred requests; an hourly snapshot is about three per collection.

## Roadmap

1. **Listing manager (dry-run first).** One process, one 60-second tick:
   build a market snapshot, decide, reconcile the desired listings against
   the actual ones, and only then sign. Everything that touches a wallet sits
   behind a `dry_run` flag that defaults to on, and a `manual_confirm` mode
   proposes each action for approval before it is sent. Needs Magic Eden's
   `/instructions/sell`, `sell_change_price`, `sell_cancel` and
   `mmm/sol-fulfill-buy`, hence an API key.
2. **Fee model from a real transaction.** `config/fees.yaml` is written from
   the balance changes of the first sale, not from documentation.
3. **Alerts and a CLI**: daily summary, floor moves, bids inside the band,
   pause / resume / panic.

## Principles

- Never undercut the floor; sit one tick above the cheapest competing ask and
  let one-unit sellers clear first.
- Reprice only when the target moves more than a threshold, so the bot does
  not spam transactions chasing penny-jumpers.
- A reservation price in fiat with a SOL backstop; units below it are held.
- No fee or API parameter is trusted until it is in `docs/verified.md` with
  evidence from a live call or a real transaction.
- Secrets (`config/secrets.env`, key files) never enter the repository.
