#!/usr/bin/env bash
# Install Pickup Poll on a fresh Ubuntu VM.   sudo ./deploy/setup.sh poll-name.region.cloudapp.azure.com
set -euo pipefail
HOST="${1:?Usage: sudo ./deploy/setup.sh <the public host name of this VM>}"
[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update -y
apt-get install -y python3 sqlite3 debian-keyring debian-archive-keyring apt-transport-https curl gpg
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

id pickup >/dev/null 2>&1 || useradd --system --home /var/lib/pickup-poll --shell /usr/sbin/nologin pickup
mkdir -p /opt/pickup-poll /var/lib/pickup-poll /var/backups/pickup-poll
cp -r "$SRC"/app.py "$SRC"/*.js "$SRC"/index.html "$SRC"/styles.css /opt/pickup-poll/
chown -R root:root /opt/pickup-poll
chown pickup:pickup /var/lib/pickup-poll; chmod 700 /var/lib/pickup-poll

if [ ! -f /etc/pickup-poll.env ]; then
  PIN="$(python3 -c 'import secrets; print("".join(secrets.choice("0123456789") for _ in range(8)))')"
  printf 'PICKUP_POLL_PIN=%s\nPOLL_HOST=%s\n' "$PIN" "$HOST" > /etc/pickup-poll.env
  chmod 600 /etc/pickup-poll.env
  echo "Organizer PIN (write it down, it is only stored in /etc/pickup-poll.env): $PIN"
fi

cp "$SRC/deploy/pickup-poll.service" /etc/systemd/system/
sed "s/POLL_HOST_PLACEHOLDER/$HOST/" "$SRC/deploy/Caddyfile.template" > /etc/caddy/Caddyfile

# Daily copy of the database, keeping 14 days.
cat > /etc/cron.daily/pickup-poll-backup <<'CRON'
#!/bin/sh
for f in /var/lib/pickup-poll/*.db /var/lib/pickup-poll/*.sqlite*; do
  [ -f "$f" ] && sqlite3 "$f" ".backup /var/backups/pickup-poll/$(basename "$f").$(date +%F)"
done
find /var/backups/pickup-poll -type f -mtime +14 -delete
CRON
chmod 755 /etc/cron.daily/pickup-poll-backup

systemctl daemon-reload
systemctl enable --now pickup-poll
systemctl restart caddy
sleep 2
systemctl is-active pickup-poll caddy
echo "Open https://$HOST/"
