#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 24.04 VPS for the Magic Eden trading bot.
#
# Does what the Phase 1 checklist in PLAN.md asks for:
#   - non-root user with your SSH key, sudo without password for that user
#   - sshd: key-only, no root login
#   - ufw: SSH only (plus nothing else; the bot makes outbound calls only)
#   - unattended security updates
#   - Docker Engine + Compose plugin (for the Phase 1 bot), python3 venv, git
#   - the repo under /opt/magiceden-trading-bot, a venv with the analysis requirements
#   - hourly snapshot cron (analysis/snapshot_daily.py) with log rotation
#
# Run once, as root, on the new server:
#
#   scp deploy/bootstrap.sh root@<ip>:/root/ && \
#   ssh root@<ip> 'BOT_USER=bot SSH_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)" bash /root/bootstrap.sh'
#
# Pass REPO_URL to clone from git; without it the script expects the code to be
# rsynced to /opt/magiceden-trading-bot afterwards (see docs/deploy.md). Re-running is safe.

set -euo pipefail

BOT_USER="${BOT_USER:-bot}"
SSH_PUBKEY="${SSH_PUBKEY:-}"          # required unless the user already exists with a key
REPO_URL="${REPO_URL:-}"              # optional: git URL to clone
APP_DIR="${APP_DIR:-/opt/magiceden-trading-bot}"
SNAPSHOT_MINUTE="${SNAPSHOT_MINUTE:-7}"   # run the snapshot at :07 every hour
SSH_PORT="${SSH_PORT:-22}"

log() { printf '\n==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || echo "warning: tested on Ubuntu 24.04, found ${PRETTY_NAME:-unknown}" >&2

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------- packages
log "base packages"
apt-get update -q
apt-get install -y -q --no-install-recommends \
  ca-certificates curl gnupg git ufw unattended-upgrades apt-listchanges \
  python3 python3-venv python3-pip logrotate cron rsync fail2ban

timedatectl set-timezone UTC || true

# ---------------------------------------------------------------- user
log "user ${BOT_USER}"
if ! id -u "$BOT_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$BOT_USER"
fi
usermod -aG sudo "$BOT_USER"
echo "${BOT_USER} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-"$BOT_USER"
chmod 440 /etc/sudoers.d/90-"$BOT_USER"

install -d -m 700 -o "$BOT_USER" -g "$BOT_USER" /home/"$BOT_USER"/.ssh
AUTH=/home/"$BOT_USER"/.ssh/authorized_keys
touch "$AUTH"
if [[ -n "$SSH_PUBKEY" ]]; then
  grep -qxF "$SSH_PUBKEY" "$AUTH" || echo "$SSH_PUBKEY" >> "$AUTH"
elif [[ ! -s "$AUTH" ]] && [[ -s /root/.ssh/authorized_keys ]]; then
  # provider installed your key for root; reuse it
  cat /root/.ssh/authorized_keys >> "$AUTH"
fi
[[ -s "$AUTH" ]] || { echo "no SSH public key for ${BOT_USER}; set SSH_PUBKEY" >&2; exit 1; }
chmod 600 "$AUTH"
chown "$BOT_USER":"$BOT_USER" "$AUTH"

# ---------------------------------------------------------------- sshd
log "sshd hardening (key-only, no root)"
install -d /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/10-trading-bot.conf <<EOF
Port ${SSH_PORT}
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
X11Forwarding no
AllowUsers ${BOT_USER}
EOF
sshd -t
systemctl reload ssh || systemctl reload sshd

# ---------------------------------------------------------------- firewall
log "ufw: allow ${SSH_PORT}/tcp only"
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}"/tcp comment 'ssh'
ufw --force enable

# fail2ban with defaults is enough for an SSH-only box
systemctl enable --now fail2ban

# ---------------------------------------------------------------- updates
log "unattended security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
# reboot automatically for kernel updates at 04:30 UTC; the bot restarts via compose/systemd
sed -i 's|^//\?Unattended-Upgrade::Automatic-Reboot .*|Unattended-Upgrade::Automatic-Reboot "true";|' /etc/apt/apt.conf.d/50unattended-upgrades
sed -i 's|^//\?Unattended-Upgrade::Automatic-Reboot-Time .*|Unattended-Upgrade::Automatic-Reboot-Time "04:30";|' /etc/apt/apt.conf.d/50unattended-upgrades
systemctl enable --now unattended-upgrades

# ---------------------------------------------------------------- docker
log "docker engine + compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
usermod -aG docker "$BOT_USER"
systemctl enable --now docker

# ---------------------------------------------------------------- app
log "app dir ${APP_DIR}"
install -d -o "$BOT_USER" -g "$BOT_USER" "$APP_DIR"
if [[ -n "$REPO_URL" ]]; then
  if [[ -d "$APP_DIR/.git" ]]; then
    sudo -u "$BOT_USER" git -C "$APP_DIR" pull --ff-only
  else
    sudo -u "$BOT_USER" git clone "$REPO_URL" "$APP_DIR"
  fi
fi

if [[ -f "$APP_DIR/analysis/requirements.txt" ]]; then
  log "python venv"
  sudo -u "$BOT_USER" python3 -m venv "$APP_DIR/.venv"
  sudo -u "$BOT_USER" "$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/analysis/requirements.txt"
  install -d -o "$BOT_USER" -g "$BOT_USER" "$APP_DIR/analysis/out" "$APP_DIR/analysis/raw/snapshots"
else
  echo "note: ${APP_DIR}/analysis/requirements.txt not found; rsync the repo and re-run this script to build the venv" >&2
fi

# secrets file placeholder, never world-readable
if [[ ! -f "$APP_DIR/config/secrets.env" ]]; then
  install -d -o "$BOT_USER" -g "$BOT_USER" "$APP_DIR/config"
  install -m 600 -o "$BOT_USER" -g "$BOT_USER" /dev/null "$APP_DIR/config/secrets.env"
fi

# ---------------------------------------------------------------- cron
log "hourly snapshot cron"
cat > /etc/cron.d/me-trading-bot-snapshot <<EOF
# Magic Eden trading bot: order-book snapshot every hour (analysis/snapshot_daily.py)
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""
${SNAPSHOT_MINUTE} * * * * ${BOT_USER} cd ${APP_DIR} && flock -n /tmp/me-trading-bot-snapshot.lock .venv/bin/python analysis/snapshot_daily.py >> analysis/out/snapshot.log 2>&1
EOF
chmod 644 /etc/cron.d/me-trading-bot-snapshot

cat > /etc/logrotate.d/trading-bot <<EOF
${APP_DIR}/analysis/out/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
    su ${BOT_USER} ${BOT_USER}
}
EOF

# ---------------------------------------------------------------- done
log "done"
cat <<EOF

Next, from your laptop:
  ssh -p ${SSH_PORT} ${BOT_USER}@<ip>                      # root login is now disabled
$( [[ -z "$REPO_URL" ]] && echo "  rsync -az --exclude .venv --exclude analysis/raw --exclude analysis/out ./ ${BOT_USER}@<ip>:${APP_DIR}/   # then re-run this script to build the venv" )
  ssh ${BOT_USER}@<ip> 'cd ${APP_DIR} && .venv/bin/python analysis/snapshot_daily.py'   # first snapshot by hand
  ssh ${BOT_USER}@<ip> 'tail -n 3 ${APP_DIR}/analysis/out/snapshots.csv'

The cron entry is /etc/cron.d/me-trading-bot-snapshot (hourly at :${SNAPSHOT_MINUTE}). Logs: ${APP_DIR}/analysis/out/snapshot.log
EOF
