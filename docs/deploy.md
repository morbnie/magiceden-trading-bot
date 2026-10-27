# Deployment

The snapshot job needs a host that is always on. Two supported setups:
**Railway** (a cron service) and a plain **VPS** (`deploy/bootstrap.sh`).

## Railway

What runs: one *cron service* built from `deploy/railway/Dockerfile.snapshot`
that executes `analysis/snapshot_daily.py` once an hour and exits. With
`DATABASE_URL` pointing at a Postgres service, every run writes its summary
rows to `snapshots` and the full payload to `snapshots_raw`, which is the way
to read the series from a laptop. A `/data` volume is optional and keeps the
same data as files.

Railway's `railway.json` / `railway.toml` config files are deprecated (new
services cannot use them; existing ones stop working 2026-12-01). The
replacement is Infrastructure as Code, `.railway/railway.ts`, applied from the
CLI; Railway does not read it at deploy time. Two settings this job needs are
not in the IaC DSL as of October 2026 and stay in the dashboard: the **cron
schedule** and the **restart policy**.

### Option A: dashboard only

1. Push the repo to GitHub, or deploy the working directory with the CLI:
   `railway link` then `railway up`.
2. **New → Service** from the repo. Name it, for example, `trading-bot-snapshot`.
3. **Postgres (recommended).** If the project has no database:
   **Create → Database → PostgreSQL**; Railway names the service `Postgres`.
4. **Variables** on the service. Set them from the CLI, which applies at once
   (an edit in the dashboard's variable editor can sit unapplied):

   ```bash
   railway variables --service trading-bot-snapshot --set RAILWAY_DOCKERFILE_PATH=deploy/railway/Dockerfile.snapshot
   railway variables --service trading-bot-snapshot --set BOT_DATA_DIR=/data
   railway variables --service trading-bot-snapshot --set 'DATABASE_URL=${{Postgres.DATABASE_URL}}'
   railway run --service trading-bot-snapshot .venv/bin/python -c "import os; print(len(os.environ.get('DATABASE_URL','')))"   # non-zero = resolved
   ```

5. **Settings**: *Region* close to your RPC provider; *Cron Schedule*
   `7 * * * *` (UTC; Railway skips a run if the previous one is still active
   and may start a few minutes late); *Restart policy* Never.
6. Optional **Volume**: mount path `/data`, 1 GB. Railway mounts volumes
   owned by root; the image runs as root for that reason. Without a volume
   the files are ephemeral and only Postgres keeps history.
7. Deploy, then trigger one run and read its log:

   ```bash
   railway cron run --yes
   railway cron list            # prints a ready-made `railway logs` line for the latest run
   railway logs -n 20
   ```

   Expect one line per collection with the floor, best bid and listed count,
   plus `postgres: wrote N rows; snapshots now holds M rows, latest …`. A
   `postgres sink failed: …` line names the problem; `postgres: DATABASE_URL
   is unset or empty` means the variable did not reach the container. Cron
   services have no live deployment to redeploy; `railway up --detach`
   creates a fresh one after code or variable changes if a push did not.

### Option B: Infrastructure as Code

`.railway/railway.ts` declares the same service, its variables and the volume
as a **named partial**. A partial only owns what it declares, so applying it
leaves other services in the project alone. It needs Node for the CLI to
evaluate the file.

```bash
npm install                     # railway SDK (package.json)
railway login && railway link
# edit .railway/railway.ts: set PROJECT and SERVICE to your names, pick REGION
railway config plan             # expect: create the service and volume; nothing else
railway config apply
```

Then attach the repo as the service source, set `DATABASE_URL` (the file marks
it `preserve()`, so the value stays on Railway and never in git), and set the
cron schedule and restart policy as in Option A.

The Postgres service is not declared in the file on purpose: declaring
`postgres("...")` would make this partial the owner of a database that may
belong to other work, and removing it later would delete it.

### Reading the data

Keep the database private. Ad-hoc queries go through the Railway CLI's
authenticated shell into the Postgres container, which has `psql`:

```bash
railway ssh --service Postgres -- psql -U postgres -c \
  "select ts, collection, floor_sol, bid1, listed, fillable_bids_within_5pct from snapshots order by ts desc limit 9;"
```

### Caveats

- **Shared egress IPs.** Magic Eden's public endpoints allow ~120 requests a
  minute *per IP* and Railway egress is shared. The snapshot makes a handful
  of requests an hour, so it is unlikely to matter; watch the log for `429`.
- **Cron runs are skipped, not queued**, if the previous run is still going;
  minimum spacing is 5 minutes.
- **Volume and region.** A volume follows its service's region; changing the
  region later migrates the volume with downtime.

## VPS

`deploy/bootstrap.sh` prepares a fresh Ubuntu 24.04 box: non-root user with
your key, key-only sshd, ufw SSH-only, fail2ban, unattended security updates,
Docker + Compose, the repo under `/opt/magiceden-trading-bot` with a venv, and
`/etc/cron.d/me-trading-bot-snapshot` hourly with log rotation.

```bash
scp deploy/bootstrap.sh root@<ip>:/root/
ssh root@<ip> 'BOT_USER=bot SSH_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)" REPO_URL=<git url> bash /root/bootstrap.sh'
```

Without `REPO_URL`, rsync the checkout to `/opt/magiceden-trading-bot` and re-run the
script to build the venv. The script prints the verification commands at the
end. 2 vCPU and 4 GB are plenty.

Sources: Railway docs on [Infrastructure as Code](https://docs.railway.com/infrastructure-as-code),
its [reference](https://docs.railway.com/infrastructure-as-code/reference),
[cron jobs](https://docs.railway.com/reference/cron-jobs),
[Dockerfiles](https://docs.railway.com/guides/dockerfiles),
[volumes](https://docs.railway.com/reference/volumes) and
[regions](https://docs.railway.com/deployments/regions), read 2026-10-27.
