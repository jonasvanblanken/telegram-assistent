#!/bin/bash
# Box für pearl-kit einrichten (einmal, als root, auf einem frischen Ubuntu 24.04).
#   rsync -a --exclude .git ~/Projects/pearl-kit/ root@<box>:/root/pearl-kit/
#   ssh root@<box> bash /root/pearl-kit/box/einrichten.sh
# Danach: /etc/pearl-kit/box.env füllen (Wache, Domain der Einrichtungsseite), /etc/pearl-kit/google-client.json
# ablegen (Google, optional), dieses Skript noch einmal starten, dann je Person box/person-anlegen.sh.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
QUELLE="$(cd "$(dirname "$0")/.." && pwd)"
KIT=/opt/pearl-kit
. "$(dirname "$0")/netz.sh"

echo "== Pakete"
export DEBIAN_FRONTEND=noninteractive
# Beim ersten Start läuft oft noch das automatische Update: bis zu zehn Minuten auf die Sperre warten. Ein hängender
# Spiegel bricht nach 30 s ab, dann gibt es einen zweiten Versuch.
APT=(apt-get -q -o DPkg::Lock::Timeout=600 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=3)
nochmal "${APT[@]}" update
nochmal "${APT[@]}" install -y python3 python3-requests python3-venv git zstd curl rsync ufw unattended-upgrades openssl qrencode \
  xz-utils caddy
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 oder neuer nötig (tomllib)"'

echo "== Sicherheit: Firewall nur SSH (dazu HTTPS, sobald die Einrichtungsseite eine Domain hat), Sicherheitsupdates"
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null

echo "== Kit nach $KIT"
getent group assistenten >/dev/null || groupadd assistenten
install -d -m 755 -o root -g root "$KIT"
rsync -a --delete --exclude .git --exclude .venv-google --exclude __pycache__ --exclude tests/ergebnisse "$QUELLE"/ "$KIT"/
chown -R root:root "$KIT"
chmod -R go-w "$KIT"

if [ -z "${OHNE_GOOGLE:-}" ]; then
  echo "== Google-Dienst (workspace-mcp) in $KIT/.venv-google"
  if [ ! -x "$KIT/.venv-google/bin/python3" ]; then
    python3 -m venv "$KIT/.venv-google"
  fi
  nochmal workspace_mcp "$KIT/.venv-google" "$KIT/box/workspace-mcp.txt"
fi

echo "== ChatGPT (Codex) und Gemini CLI"
bash "$KIT/box/ki-programme.sh"

echo "== Einstellungen in /etc/pearl-kit"
install -d -m 750 -o root -g assistenten /etc/pearl-kit
if [ ! -e /etc/pearl-kit/box.env ]; then
  cat > /etc/pearl-kit/box.env <<'ENV'
# Wache: an wen gehen Störungsmeldungen? Ein eigener Admin-Bot (BotFather) oder ein vorhandener.
WACHE_TG_TOKEN=
WACHE_CHAT_ID=
ENV
  chmod 600 /etc/pearl-kit/box.env
fi
grep -q '^PFORTE_DOMAIN=' /etc/pearl-kit/box.env || cat >> /etc/pearl-kit/box.env <<'ENV'
# Einrichtungsseite: Domain dieser Box (A-Record zeigt hierher), z. B. box.example.de. Leer = ohne Einrichtungsseite,
# dann meldet person-anlegen.sh die KI im Terminal an und Google endet bei „localhost“.
PFORTE_DOMAIN=
ENV
DOMAIN=$(sed -n 's/^PFORTE_DOMAIN=//p' /etc/pearl-kit/box.env | tr -d ' "'"'"'')
[ -e /etc/pearl-kit/google-client.json ] && chown root:assistenten /etc/pearl-kit/google-client.json \
  && chmod 640 /etc/pearl-kit/google-client.json

echo "== Einrichtungsseite (Caddy mit HTTPS)"
install -d -m 755 /etc/caddy/pearl-kit
[ -e /etc/caddy/pearl-kit/_leer.caddy ] || echo "# je Person eine Datei <kurzname>.caddy (person-anlegen.sh)" > /etc/caddy/pearl-kit/_leer.caddy
if [ -n "$DOMAIN" ]; then
  [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$ ]] || { echo "PFORTE_DOMAIN sieht nicht nach einer Domain aus: $DOMAIN" >&2; exit 1; }
  cat > /etc/caddy/Caddyfile <<CADDY
# geschrieben von pearl-kit/box/einrichten.sh, je Person ein Pfad aus /etc/caddy/pearl-kit/
$DOMAIN {
	header -Server
	import /etc/caddy/pearl-kit/*.caddy
	handle {
		respond 404
	}
}
CADDY
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  systemctl enable caddy >/dev/null
  systemctl reload-or-restart caddy
  echo "   https://$DOMAIN"
else
  systemctl disable --now caddy >/dev/null 2>&1 || true
fi

echo "== Wache"
install -d -m 700 /var/lib/pearl-kit
install -m 644 "$KIT/systemd/pearl-kit-wache.service" "$KIT/systemd/pearl-kit-wache.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pearl-kit-wache.timer >/dev/null

echo
echo "Fertig. Noch offen:"
grep -q '^WACHE_TG_TOKEN=.\+' /etc/pearl-kit/box.env || echo "  - /etc/pearl-kit/box.env: WACHE_TG_TOKEN und WACHE_CHAT_ID eintragen"
[ -n "$DOMAIN" ] || echo "  - für die Einrichtungsseite: PFORTE_DOMAIN in /etc/pearl-kit/box.env (README, Abschnitt Domain), dann dieses Skript noch einmal"
[ -e /etc/pearl-kit/google-client.json ] || echo "  - für Google: OAuth-Client nach /etc/pearl-kit/google-client.json (README, Abschnitt Google), dann dieses Skript noch einmal"
echo "  - je Person: bash $KIT/box/person-anlegen.sh <kurzname>"
