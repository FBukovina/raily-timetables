#!/usr/bin/env bash
# Deploy or update Raily Live on an Ubuntu 24.04 host, such as an Oracle Cloud
# Always Free Ampere A1 VM. Re-running it updates the code and restarts.
#
#   deploy/live-relay/deploy.sh ubuntu@<public-ip> [host-name]
#   RAILY_GOLEMIO_KEY_FILE=<file> deploy/live-relay/deploy.sh ...
#
# Prague realtime (/v1/pid) asks Golemio with Raily's key, kept on the host in
# /etc/raily/golemio-key. Pass the file holding it to install or replace it;
# without one the installed key stays, and with none the app asks Golemio
# itself.
#
# Without a host name it serves https://<ip-with-dashes>.sslip.io: sslip.io
# resolves that name to the IP, so Caddy can obtain a Let's Encrypt
# certificate without a domain of our own.
set -euo pipefail
TARGET=${1:?usage: deploy.sh user@host [host-name]}
IP=${TARGET#*@}
HOST=${2:-${IP//./-}.sslip.io}
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# A new VM's host key is recorded on first contact and checked afterwards.
SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"

rsync -az --delete --exclude __pycache__ -e "$SSH" "$ROOT/scripts/" "$TARGET:/tmp/raily-scripts/"
rsync -az -e "$SSH" "$ROOT/deploy/live-relay/raily-live.service" "$TARGET:/tmp/raily-live.service"
if [[ -n ${RAILY_GOLEMIO_KEY_FILE:-} ]]; then
  $SSH "$TARGET" 'umask 077; cat > /tmp/raily-golemio-key' < "$RAILY_GOLEMIO_KEY_FILE"
fi
$SSH "$TARGET" sudo bash -s "$HOST" <<'REMOTE'
set -euo pipefail
HOST=$1
export DEBIAN_FRONTEND=noninteractive
# A fresh VM runs its first-boot updates; wait for them and the apt lock.
cloud-init status --wait >/dev/null 2>&1 || true
apt-get -o DPkg::Lock::Timeout=600 update -q
apt-get -o DPkg::Lock::Timeout=600 install -y -q python3 tzdata caddy rsync
id raily >/dev/null 2>&1 || useradd --system --home /srv/raily --shell /usr/sbin/nologin raily
if [[ -f /tmp/raily-golemio-key ]]; then
  install -d -m 750 -o root -g raily /etc/raily
  install -m 640 -o root -g raily /tmp/raily-golemio-key /etc/raily/golemio-key
  rm -f /tmp/raily-golemio-key
fi
mkdir -p /srv/raily/cache
rsync -a --delete /tmp/raily-scripts/ /srv/raily/scripts/
chown -R raily /srv/raily/cache
install -m 644 /tmp/raily-live.service /etc/systemd/system/raily-live.service
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
	encode gzip
	reverse_proxy 127.0.0.1:8080
	# No access log: requests name the runs a person looks at.
	log {
		output discard
	}
}
CADDY
# Oracle's Ubuntu images accept only SSH in iptables. Open HTTP (certificate
# challenges) and HTTPS; the VCN security list must allow them too.
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport "$port" -j ACCEPT
done
if command -v netfilter-persistent >/dev/null; then netfilter-persistent save; fi
systemctl daemon-reload
systemctl enable raily-live caddy
systemctl restart raily-live caddy
REMOTE
echo "Deployed to https://$HOST — /v1/health turns ok after the first fetch, a few minutes after start."
