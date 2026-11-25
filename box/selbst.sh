#!/bin/bash
# Eigener Server für genau eine Person, ohne Betreuer. Läuft beim ersten Start von selbst (Cloud-config, die die
# Anleitung im Browser baut) und braucht nur zwei Werte aus /root/assistent.env:
#   TG_TOKEN=<Token des Bots von @BotFather>
#   EINLADUNG=<Code aus dem t.me-Link, den die Anleitung schon angezeigt hat>
# Dazu optional AUSSTATTUNG=<vorab gewählte Kits, z. B. whatsapp,ablage oder ohne>. Fehlt die Zeile, bietet die
# Einrichtungsseite alle Kits an. Optional SUPPORT=<WhatsApp-Nummer mit Ländervorwahl, nur Ziffern> für den Knopf
# „Support schreiben“ in den Einstellungen; fehlt die Zeile, gibt es den Knopf nicht.
# Den Fortschritt schreibt das Skript in die Beschreibung des Bots, die Anleitung liest sie im Browser mit:
#   „⏳ 2/3 …“ läuft, „⚠️ …“ ist schiefgegangen, „Dein persönlicher Assistent …“ (setzt assistent.py) heißt fertig.
# HTTPS ohne eigene Domain: <ip-mit-strichen>.sslip.io, klappt das Zertifikat dort nicht, nip.io.
# Läuft bei Hetzner und DigitalOcean (oder jedem Anbieter mit öffentlicher IPv4 und Cloud-init), unter 3 GB
# Arbeitsspeicher legt es 2 GB Swap an.
# Protokoll: /var/log/assistent-installation.log
set -Eeuo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
KIT_QUELLE="$(cd "$(dirname "$0")/.." && pwd)"
ENVDATEI=${ENVDATEI:-/root/assistent.env}
KURZ=ich
touch /var/log/assistent-installation.log && chmod 600 /var/log/assistent-installation.log   # nur root liest das Protokoll
exec > >(tee -a /var/log/assistent-installation.log) 2>&1
# Solange die Einrichtung läuft, spielt box/selbst-aktualisieren.sh nichts ein (der Timer steht schon vor dem Selbsttest).
LAEUFT=/run/pearl-kit-einrichtung
echo $$ > "$LAEUFT"
trap 'rm -f "$LAEUFT"' EXIT

TG_TOKEN=$(sed -n 's/^TG_TOKEN=//p' "$ENVDATEI" | tr -d ' "'"'"'\r')
EINLADUNG=$(sed -n 's/^EINLADUNG=//p' "$ENVDATEI" | tr -d ' "'"'"'\r')
[[ "$TG_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || { echo "TG_TOKEN fehlt oder sieht falsch aus" >&2; exit 1; }
[[ "$EINLADUNG" =~ ^[a-f0-9]{16,64}$ ]] || { echo "EINLADUNG fehlt oder sieht falsch aus" >&2; exit 1; }
AUSSTATTUNG=$(sed -n 's/^AUSSTATTUNG=//p' "$ENVDATEI" | tr -d ' "'"'"'\r')
[[ -z "$AUSSTATTUNG" || "$AUSSTATTUNG" =~ ^[a-z]+(,[a-z]+)*$ ]] || { echo "AUSSTATTUNG sieht falsch aus, ich biete alle Kits an"; AUSSTATTUNG=""; }
SUPPORT=$(sed -n 's/^SUPPORT=//p' "$ENVDATEI" | tr -d ' "'"'"'\r+')
[[ -z "$SUPPORT" || "$SUPPORT" =~ ^[0-9]{8,15}$ ]] || { echo "SUPPORT sieht falsch aus, ohne Support-Knopf"; SUPPORT=""; }
API="${TELEGRAM_API:-https://api.telegram.org}/bot$TG_TOKEN"

stand() {  # Beschreibung des Bots setzen, Fehler hier halten die Einrichtung nicht auf
  echo "== $1"
  curl -fsS -m 15 --data-urlencode "description=$1" "$API/setMyDescription" >/dev/null 2>&1 || true
}
SCHRITT="Start"
schief() {
  # Läuft der Assistent schon, hat er „Dein persönlicher Assistent …“ gesetzt und die Person kann loslegen. Ein späterer
  # Fehler überschreibt das nicht mit ⚠️, er steht im Protokoll, und die Wache meldet, was davon übrig bleibt.
  if systemctl --user -M "a-$KURZ@" is-active -q assistent.service 2>/dev/null; then
    echo "== Fehler bei „$SCHRITT“, der Assistent läuft aber, die Beschreibung des Bots bleibt"
    return 0
  fi
  stand "⚠️ Die Einrichtung ist bei „$SCHRITT“ hängen geblieben. Lösch den Server bei deinem Anbieter und leg ihn mit derselben Cloud-config neu an."
}
trap schief ERR

auslagerung() {  # unter 3 GB Arbeitsspeicher (DigitalOcean 2 GB): 2 GB Swap, damit npm, pip und Claude Platz haben
  local kb
  kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
  [[ "$kb" =~ ^[0-9]+$ ]] && [ "$kb" -lt $((3 * 1024 * 1024)) ] || return 0
  [ -z "$(swapon --noheadings --show=NAME 2>/dev/null)" ] || return 0   # Swap läuft schon
  echo "   $((kb / 1024)) MB Arbeitsspeicher, lege 2 GB Swap an"
  rm -f /swapfile   # Rest eines abgebrochenen Laufs
  if ! { { fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none; } \
         && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; }; then
    rm -f /swapfile
    return 1
  fi
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab || return 1
  echo 'vm.swappiness=10' > /etc/sysctl.d/90-pearl-kit-swap.conf && sysctl -q -w vm.swappiness=10
}
oeffentlich() {  # oeffentlich <adresse>: eine IPv4 außerhalb der privaten Netze
  [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
    && ! [[ "$1" =~ ^(0\.|10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]
}
ip_finden() {  # öffentliche IPv4: Metadaten (Hetzner, DigitalOcean), sonst die Adresse am Standardweg, sonst von außen.
  # Gibt nichts aus, wenn nichts passt, und endet immer mit 0: die ERR-Falle soll hier nicht auslösen.
  local a q
  for q in http://169.254.169.254/hetzner/v1/metadata/public-ipv4 \
           http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address; do
    a=$(curl -fsS -m 5 "$q" 2>/dev/null | tr -d '[:space:]') && oeffentlich "$a" && { echo "$a"; return 0; }
  done
  a=$(ip -4 -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p') || true
  oeffentlich "$a" && { echo "$a"; return 0; }
  for q in https://api.ipify.org https://ipv4.icanhazip.com; do
    a=$(curl -4 -fsS -m 10 "$q" 2>/dev/null | tr -d '[:space:]') && oeffentlich "$a" && { echo "$a"; return 0; }
  done
  return 0
}

SCHRITT="Programme"
stand "⏳ 1/3 Der Server lädt die Programme. Das dauert ein paar Minuten."
auslagerung || echo "   Swap ließ sich nicht anlegen, weiter ohne" >&2
IP=$(ip_finden)
[ -n "$IP" ] || { echo "Keine öffentliche IPv4-Adresse gefunden" >&2; false; }
DOMAIN="${IP//./-}.sslip.io"
install -d -m 750 /etc/pearl-kit
cat > /etc/pearl-kit/box.env <<ENV
# Eigener Server (box/selbst.sh). Technik-Hinweise der Wache gehen über den eigenen Bot an die Person.
PFORTE_DOMAIN=$DOMAIN
WACHE_SELBST=a-$KURZ
ENV
chmod 600 /etc/pearl-kit/box.env
# Release-Schlüssel für die Updates (box/selbst-aktualisieren.sh). Dieser Stand ist vertraut: die Cloud-config hat ihn
# per SHA festgelegt. Die Kopie liegt außerhalb der Quelle, ein geholter Stand kann sie nicht ändern.
install -m 644 -o root -g root "$KIT_QUELLE/box/release-signers" /etc/pearl-kit/release-signers
bash "$KIT_QUELLE/box/einrichten.sh"   # mit Google-Dienst, die Google-App legt die Person selbst an
bash "$KIT_QUELLE/box/metadaten-sperre.sh"   # Cloud-config mit dem Bot-Token nur für root

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
# Updates und /whatsapp vor dem Selbsttest: Auch wenn danach etwas scheitert, holt sich der Server neue Fassungen.
install -m 644 /opt/pearl-kit/systemd/assistent-aktualisieren.service /opt/pearl-kit/systemd/assistent-aktualisieren.timer \
  /opt/pearl-kit/systemd/assistent-whatsapp.path /opt/pearl-kit/systemd/assistent-whatsapp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now assistent-aktualisieren.timer >/dev/null   # neue Fassungen kommen ab jetzt von selbst
systemctl enable --now assistent-whatsapp.path >/dev/null         # /whatsapp im Chat: box/whatsapp.sh
SELBST=1 NEU_TG_TOKEN="$TG_TOKEN" NEU_EINLADUNG="$EINLADUNG" NEU_KITS="$AUSSTATTUNG" NEU_SUPPORT="$SUPPORT" bash /opt/pearl-kit/box/person-anlegen.sh "$KURZ" </dev/null

SCHRITT="Abschluss"
rm -f "$ENVDATEI"
echo "Fertig: $(date -Is)"
# Die Beschreibung „Dein persönlicher Assistent …“ hat assistent.py beim Start gesetzt, das ist das Fertig-Signal.
# Falls Telegram das beim Start gedrosselt hat, hier noch einmal:
sleep 2
if ! curl -fsS -m 15 "$API/getMyDescription" | grep -q 'Dein persönlicher Assistent'; then
  stand "Dein persönlicher Assistent. Tipp auf Starten, dann richtest du ihn in drei Minuten ein."
fi
