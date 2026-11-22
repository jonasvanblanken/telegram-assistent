#!/bin/bash
# Assistent einer Person beenden: Daten exportieren, Dienste stoppen, Linux-User löschen (als root).
#   bash /opt/pearl-kit/box/person-entfernen.sh <kurzname>
# Der Export (alles, was der Assistent wusste, plus Gesprächsverläufe) geht an die Person, danach löschen.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
[ $# = 1 ] || { echo "Aufruf: $0 <kurzname>" >&2; exit 2; }
U="a-$1"
id "$U" >/dev/null 2>&1 || { echo "Kein User $U" >&2; exit 1; }
H=$(getent passwd "$U" | cut -d: -f6)
ZIEL=/root/pearl-kit-export
install -d -m 700 "$ZIEL"
DATEI="$ZIEL/$U-$(date +%F).zip"
# zip statt tar.gz, damit die Person den Export am Handy öffnen kann. Ohne Git-Verlauf und ohne Zugangsdaten
# (google.env, Anmeldungen bei ChatGPT und Gemini). Gesprächsverläufe von Claude und ChatGPT kommen mit.
python3 - "$H" "$DATEI" <<'PY'
import os, sys, zipfile
h, ziel = sys.argv[1:]
with zipfile.ZipFile(ziel, "w", zipfile.ZIP_DEFLATED) as z:
    for teil in ("assistent", ".claude/projects", ".local/share/assistent/codex/sessions"):
        for wurzel, dirs, dateien in os.walk(os.path.join(h, teil)):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in dateien:
                p = os.path.join(wurzel, f)
                rel = os.path.relpath(p, h)
                if rel != "assistent/google.env" and os.path.isfile(p) and not os.path.islink(p):
                    z.write(p, rel)
PY
chmod 600 "$DATEI"
echo "Export: $DATEI ($(du -h "$DATEI" | cut -f1))"
read -r -p "User $U mit allen Daten löschen? Zum Bestätigen den Kurznamen tippen: " ok
[ "$ok" = "$1" ] || { echo "Abgebrochen, nichts gelöscht"; exit 1; }
systemctl --user -M "$U@" disable --now assistent.service google-mcp.service 2>/dev/null || true
if [ -e "/etc/caddy/pearl-kit/$1.caddy" ]; then
  rm -f "/etc/caddy/pearl-kit/$1.caddy"
  systemctl reload caddy || true
fi
loginctl disable-linger "$U" || true
pkill -u "$U" || true
sleep 1
userdel -r "$U" 2>/dev/null || userdel -rf "$U"
python3 - "$U" <<'PY'
import json, sys
p = "/var/lib/pearl-kit/wache.json"
try:
    st = json.load(open(p))
except (OSError, ValueError):
    sys.exit()
st["offen"] = {k: v for k, v in st.get("offen", {}).items() if not k.startswith(sys.argv[1] + "/")}
json.dump(st, open(p, "w"))
PY
echo "$U gelöscht. Den Export der Person geben (und danach hier löschen), den Telegram-Bot kann sie bei @BotFather mit /deletebot entfernen."
