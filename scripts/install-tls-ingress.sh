#!/usr/bin/env bash
# Install/refresh the box's TLS ingress for Twilio Media Streams (WS5 of the
# phone-lane robustification; docs/INGRESS-BAKEOFF.md "no tunnels in
# production"). Idempotent — safe to re-run on every deploy.
#
#   scripts/install-tls-ingress.sh --domain <domain>        # e.g. earshot-ops.net
#
# What it does (Ubuntu box, run as ubuntu with sudo):
#   1. installs caddy from the official apt repo if absent
#   2. renders deploy/caddy/Caddyfile with EARSHOT_DOMAIN=<domain>
#   3. validates + reloads caddy (systemd)
#   4. prints the DNS + security-group prerequisites it can't do itself
#
# After DNS resolves and caddy holds a cert, pin Q-TLS in VERIFICATIONS.md by
# running one live call through wss://media.<domain>/e/<port> to the TTS
# rehearsal number (+17627212978) BEFORE any event uses this path.
set -euo pipefail
cd "$(dirname "$0")/.."

DOMAIN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$DOMAIN" ]] || { echo "usage: $0 --domain <domain>" >&2; exit 2; }
[[ "$DOMAIN" =~ ^[a-z0-9.-]+$ ]] || { echo "suspicious domain: $DOMAIN" >&2; exit 2; }

if ! command -v caddy >/dev/null 2>&1; then
  echo "installing caddy (official apt repo)…"
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update && sudo apt-get install -y caddy
fi

# Render: the Caddyfile uses {$EARSHOT_DOMAIN}; give the systemd unit the env.
sudo mkdir -p /etc/caddy /var/log/caddy
sudo cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile
sudo mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironment=EARSHOT_DOMAIN=%s\n' "$DOMAIN" \
  | sudo tee /etc/systemd/system/caddy.service.d/earshot.conf >/dev/null
sudo chown -R caddy:caddy /var/log/caddy 2>/dev/null || true

# sudo: validation opens the log writer, whose dir is caddy-owned. It also
# CREATES the log file as root, so re-chown afterwards or the caddy user
# can't write it and the service exits 1 (bit us on first install).
sudo EARSHOT_DOMAIN="$DOMAIN" caddy validate --config /etc/caddy/Caddyfile
sudo chown -R caddy:caddy /var/log/caddy
sudo systemctl daemon-reload
sudo systemctl enable --now caddy
sudo systemctl reload caddy || sudo systemctl restart caddy
echo "caddy up for media.${DOMAIN}"

cat <<EOF

── prerequisites this script cannot do ─────────────────────────────────────
1. DNS: A record  media.${DOMAIN} → 3.212.174.94  (the box's Elastic IP —
   docs/history/RUNBOOK.md; re-check after any instance move)
2. Security group: inbound 80/tcp + 443/tcp from 0.0.0.0/0 (80 is needed for
   the Let's Encrypt HTTP challenge; Twilio media arrives on 443)
3. Q-TLS pin: one live TTS-number call through
   wss://media.${DOMAIN}/e/<port> BEFORE any event uses this path
   (launch-event.sh --tls-host media.${DOMAIN})
EOF
