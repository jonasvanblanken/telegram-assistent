#!/bin/bash
# Eigener Server für genau eine Person, ohne Betreuer. Läuft beim ersten Start von selbst (Cloud-config, die die
# Anleitung im Browser baut) und braucht nur zwei Werte aus /root/assistent.env:
#   TG_TOKEN=<Token des Bots von @BotFather>
#   EINLADUNG=<Code aus dem t.me-Link, den die Anleitung schon angezeigt hat>
# Den Fortschritt schreibt das Skript in die Beschreibung des Bots, die Anleitung liest sie im Browser mit:
#   „⏳ 2/3 …“ läuft, „⚠️ …“ ist schiefgegangen, „Dein persönlicher Assistent …“ (setzt assistent.py) heißt fertig.
# HTTPS ohne eigene Domain: <ip-mit-strichen>.sslip.io, klappt das Zertifikat dort nicht, nip.io.
# Protokoll: /var/log/assistent-installation.log
set -Eeuo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
KIT_QUELLE="$(cd "$(dirname "$0")/.." && pwd)"
ENVDATEI=${ENVDATEI:-/root/assistent.env}
KURZ=ich
exec > >(tee -a /var/log/assistent-installation.log) 2>&1

TG_TOKEN=$(sed -n 's/^TG_TOKEN=//p' "$ENVDATEI" | tr -d ' "'"'"'\r')
EINLADUNG=$(sed -n 's/^EINLADUNG=//p' "$ENVDATEI" | tr -d ' "'"'"'\r')
[[ "$TG_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || { echo "TG_TOKEN fehlt oder sieht falsch aus" >&2; exit 1; }
[[ "$EINLADUNG" =~ ^[a-f0-9]{16,64}$ ]] || { echo "EINLADUNG fehlt oder sieht falsch aus" >&2; exit 1; }
API="${TELEGRAM_API:-https://api.telegram.org}/bot$TG_TOKEN"

stand() {  # Beschreibung des Bots setzen, Fehler hier halten die Einrichtung nicht auf
  echo "== $1"
  curl -fsS -m 15 --data-urlencode "description=$1" "$API/setMyDescription" >/dev/null 2>&1 || true
}
SCHRITT="Start"
schief() {
  stand "⚠️ Die Einrichtung ist bei „$SCHRITT“ hängen geblieben. Lösch den Server bei Hetzner und leg ihn mit derselben Cloud-config neu an."
}
trap schief ERR

SCHRITT="Programme"
stand "⏳ 1/3 Der Server lädt die Programme. Das dauert ein paar Minuten."
IP=$(curl -fsS -m 10 http://169.254.169.254/hetzner/v1/metadata/public-ipv4 2>/dev/null || curl -4 -fsS -m 10 https://api.ipify.org)
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Keine öffentliche IPv4-Adresse gefunden ($IP)" >&2; false; }
DOMAIN="${IP//./-}.sslip.io"
install -d -m 750 /etc/pearl-kit
cat > /etc/pearl-kit/box.env <<ENV
# Eigener Server (box/selbst.sh). Technik-Hinweise der Wache gehen über den eigenen Bot an die Person.
PFORTE_DOMAIN=$DOMAIN
WACHE_SELBST=a-$KURZ
ENV
chmod 600 /etc/pearl-kit/box.env
OHNE_GOOGLE=1 bash "$KIT_QUELLE/box/einrichten.sh"

SCHRITT="Sichere Verbindung"
stand "⏳ 2/3 Der Server holt sich ein Zertifikat für die sichere Verbindung."
zertifikat() {  # wartet, bis https://$1 mit gültigem Zertifikat antwortet (404 ist gut, die Wurzel hat keine Seite)
  local i
  for i in $(seq 1 24); do
    if curl -sS -m 10 -o /dev/null "https://$1/" 2>/dev/null; then return 0; fi
    sleep 5
  done
  return 1
}
if ! zertifikat "$DOMAIN"; then
  ALT="${IP//./-}.nip.io"
  echo "   $DOMAIN bekommt kein Zertifikat, versuche $ALT"
  sed -i "s/^$DOMAIN {/$ALT {/" /etc/caddy/Caddyfile
  sed -i "s/^PFORTE_DOMAIN=.*/PFORTE_DOMAIN=$ALT/" /etc/pearl-kit/box.env
  systemctl reload caddy
  DOMAIN=$ALT
  zertifikat "$DOMAIN"
fi
echo "   https://$DOMAIN"

SCHRITT="Assistent"
stand "⏳ 3/3 Der Server richtet deinen Assistenten ein."
SELBST=1 NEU_TG_TOKEN="$TG_TOKEN" NEU_EINLADUNG="$EINLADUNG" bash /opt/pearl-kit/box/person-anlegen.sh "$KURZ" </dev/null

SCHRITT="Abschluss"
rm -f "$ENVDATEI"
install -m 644 /opt/pearl-kit/systemd/assistent-aktualisieren.service /opt/pearl-kit/systemd/assistent-aktualisieren.timer \
  /opt/pearl-kit/systemd/assistent-whatsapp.path /opt/pearl-kit/systemd/assistent-whatsapp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now assistent-aktualisieren.timer >/dev/null   # neue Fassungen kommen ab jetzt von selbst
systemctl enable --now assistent-whatsapp.path >/dev/null         # /whatsapp im Chat: box/whatsapp.sh
echo "Fertig: $(date -Is)"
# Die Beschreibung „Dein persönlicher Assistent …“ hat assistent.py beim Start gesetzt, das ist das Fertig-Signal.
# Falls Telegram das beim Start gedrosselt hat, hier noch einmal:
sleep 2
if ! curl -fsS -m 15 "$API/getMyDescription" | grep -q 'Dein persönlicher Assistent'; then
  stand "Dein persönlicher Assistent. Tipp auf Starten, dann richtest du ihn in drei Minuten ein."
fi
