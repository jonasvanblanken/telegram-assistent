#!/bin/bash
# Neue Fassung des Kits einspielen und alle Assistenten neu starten (als root).
#   rsync -a --exclude .git ~/Projects/pearl-kit/ root@<box>:/root/pearl-kit/
#   ssh root@<box> bash /root/pearl-kit/box/aktualisieren.sh
# Was die Assistenten gelernt haben (home/), bleibt unberührt. settings.json, mcp.json und die Einstellungen für
# Codex und Gemini kommen neu aus config.toml. Neue Fassungen von Codex und Gemini stehen in box/ki-programme.sh.
# Die neue Fassung entsteht erst in /opt/pearl-kit.neu und tauscht dann den Platz mit /opt/pearl-kit. Scheitert danach
# etwas, kommt die bisherige Fassung zurück (sie liegt bis zum nächsten Update in /opt/pearl-kit.alt).
# Richtet eine Person gerade ein (Telegram verbunden, home/ noch nicht da), startet ihr Assistent bis zu zwei Stunden
# lang nicht neu, damit die Anmeldung im Terminal der Einrichtungsseite nicht abreißt. Den Neustart holt der nächste
# Lauf nach (eigener Server: box/selbst-aktualisieren.sh stündlich, sonst dieses Skript noch einmal starten).
set -Eeuo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
QUELLE="$(cd "$(dirname "$0")/.." && pwd)"
KIT=/opt/pearl-kit
NEU=$KIT.neu
ALT=$KIT.alt
OFFEN=/var/lib/pearl-kit/neustart-offen     # je User eine Datei: seit wann sein Neustart zurückgestellt ist
ZURUECKSTELLEN=$((2 * 3600))
[ "$QUELLE" = "$KIT" ] && { echo "Bitte aus der neuen Kopie starten (z. B. /root/pearl-kit), nicht aus $KIT" >&2; exit 1; }
. "$QUELLE/box/netz.sh"

pruefen() {  # Tests der neuen Fassung ohne root: als nobody auf einer Kopie, die Quelle bleibt unberührt
  local t ok=0
  t=$(mktemp -d)
  rsync -a --exclude .git --exclude .venv-google --exclude __pycache__ --exclude tests/ergebnisse "$QUELLE"/ "$t/kit"/
  chown -R nobody: "$t"
  if runuser -u nobody -- env -i -C "$t/kit" HOME="$t" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
      timeout -k 30 1200 python3 -m unittest discover -s tests -q; then
    ok=1
  fi
  rm -rf "$t"
  [ "$ok" = 1 ]
}
pruefen || { echo "Tests schlagen fehl, nichts eingespielt" >&2; exit 1; }

zurueckstellen() {  # zurueckstellen <user> <home>: wahr, solange die Person gerade einrichtet
  local u=$1 h=$2 seit
  if [ -e "$h/assistent/home/CLAUDE.md" ] || ! grep -q '^telegram_id = [1-9]' "$h/assistent/config.toml"; then
    rm -f "$OFFEN/$u"
    return 1
  fi
  seit=$(cat "$OFFEN/$u" 2>/dev/null || true)
  [[ "$seit" =~ ^[0-9]+$ ]] || seit=$(date +%s)
  if [ $(($(date +%s) - seit)) -ge "$ZURUECKSTELLEN" ]; then
    echo "$u: richtet seit über $((ZURUECKSTELLEN / 3600)) Stunden ein, starte trotzdem neu"
    rm -f "$OFFEN/$u"
    return 1
  fi
  install -d -m 700 "$OFFEN"
  echo "$seit" > "$OFFEN/$u"
  echo "$u: richtet gerade ein, Neustart beim nächsten Lauf"
}

einspielen() {  # alles, was die Fassung in $KIT außerhalb von $KIT braucht: Dienste, je Person nachziehen, Neustart
  install -m 644 "$KIT/systemd/pearl-kit-wache.service" "$KIT/systemd/pearl-kit-wache.timer" /etc/systemd/system/
  if [ -d /opt/assistent-quelle/.git ]; then   # eigener Server aus der Cloud-config: holt neue Fassungen selbst
    if [ ! -x "$KIT/.venv-google/bin/workspace-mcp" ]; then   # Server von vor dem Kit Kalender und Mail
      echo "== Google-Dienst (workspace-mcp) nachrüsten"
      { python3 -m venv "$KIT/.venv-google" && workspace_mcp "$KIT/.venv-google" "$KIT/box/workspace-mcp.txt"; } \
        || echo "   Google-Dienst nicht installiert, nächster Versuch beim nächsten Update" >&2
    fi
    bash "$KIT/box/metadaten-sperre.sh" \
      || echo "   WARNUNG: Metadaten-Dienst nicht gesperrt, die Cloud-config mit dem Bot-Token ist für alle lesbar" >&2
    install -m 644 "$KIT/systemd/assistent-aktualisieren.service" "$KIT/systemd/assistent-aktualisieren.timer" /etc/systemd/system/
    install -m 644 "$KIT/systemd/assistent-whatsapp.path" "$KIT/systemd/assistent-whatsapp.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable -q assistent-aktualisieren.timer assistent-whatsapp.path
    systemctl start assistent-aktualisieren.timer assistent-whatsapp.path
  fi
  systemctl daemon-reload
  local u h
  for u in $(getent group assistenten | cut -d: -f4 | tr , ' '); do
    h=$(getent passwd "$u" | cut -d: -f6)
    [ -e "$h/assistent/config.toml" ] || continue
    if zurueckstellen "$u" "$h"; then continue; fi
    runuser -u "$u" -- env HOME="$h" python3 "$KIT/einrichten.py" --nachziehen
    install -o "$u" -g "$u" -m 644 "$KIT/systemd/assistent.service" "$KIT/systemd/google-mcp.service" "$h/.config/systemd/user/"
    systemctl --user -M "$u@" daemon-reload
    systemctl --user -M "$u@" restart assistent.service
    if grep -q '^konto = ".\+"' "$h/assistent/config.toml"; then systemctl --user -M "$u@" restart google-mcp.service; fi
    echo "$u: neu gestartet"
  done
}

zurueck() {  # ERR nach dem Tausch: bisherige Fassung zurück an ihren Platz und noch einmal damit einspielen
  trap - ERR
  set +e
  echo "Einspielen gescheitert, stelle die bisherige Fassung wieder her" >&2
  rm -rf "$NEU"
  mv "$KIT" "$NEU" && mv "$ALT" "$KIT"
  einspielen
  echo "Bisherige Fassung läuft wieder, die neue liegt zum Nachsehen in $NEU" >&2
  exit 1
}

echo "== Neue Fassung nach $NEU"
rm -rf "$NEU"
# Harte Links kosten kaum Platz und bringen .venv-google mit. rsync ersetzt geänderte Dateien durch neue, statt sie zu
# beschreiben, $KIT bleibt also unberührt.
cp -al "$KIT" "$NEU"
rsync -a --delete --exclude .git --exclude .venv-google --exclude __pycache__ --exclude tests/ergebnisse "$QUELLE"/ "$NEU"/
chown -R root:root "$NEU"; chmod -R go-w "$NEU"
bash "$NEU/box/ki-programme.sh"   # Downloads vor dem Tausch: scheitern sie, bleibt $KIT, wie es war

echo "== Tausch"
rm -rf "$ALT"
mv "$KIT" "$ALT"
mv "$NEU" "$KIT" || { mv "$ALT" "$KIT"; exit 1; }
trap zurueck ERR
einspielen
trap - ERR
echo "Eingespielt, die bisherige Fassung liegt in $ALT"
