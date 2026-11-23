#!/bin/bash
# Neue Fassung des Kits einspielen und alle Assistenten neu starten (als root).
#   rsync -a --exclude .git ~/Projects/pearl-kit/ root@<box>:/root/pearl-kit/
#   ssh root@<box> bash /root/pearl-kit/box/aktualisieren.sh
# Was die Assistenten gelernt haben (home/), bleibt unberührt. settings.json, mcp.json und die Einstellungen für
# Codex und Gemini kommen neu aus config.toml. Neue Fassungen von Codex und Gemini stehen in box/ki-programme.sh.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
QUELLE="$(cd "$(dirname "$0")/.." && pwd)"
KIT=/opt/pearl-kit
[ "$QUELLE" = "$KIT" ] && { echo "Bitte aus der neuen Kopie starten (z. B. /root/pearl-kit), nicht aus $KIT" >&2; exit 1; }
python3 -m unittest discover -s "$QUELLE/tests" -q || { echo "Tests schlagen fehl, nichts eingespielt" >&2; exit 1; }
rsync -a --delete --exclude .git --exclude .venv-google --exclude __pycache__ --exclude tests/ergebnisse "$QUELLE"/ "$KIT"/
chown -R root:root "$KIT"; chmod -R go-w "$KIT"
bash "$KIT/box/ki-programme.sh"
install -m 644 "$KIT/systemd/pearl-kit-wache.service" "$KIT/systemd/pearl-kit-wache.timer" /etc/systemd/system/
if [ -d /opt/assistent-quelle/.git ]; then   # eigener Server aus der Cloud-config: holt neue Fassungen selbst
  install -m 644 "$KIT/systemd/assistent-aktualisieren.service" "$KIT/systemd/assistent-aktualisieren.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable -q assistent-aktualisieren.timer
  systemctl start assistent-aktualisieren.timer
fi
systemctl daemon-reload
for u in $(getent group assistenten | cut -d: -f4 | tr , ' '); do
  h=$(getent passwd "$u" | cut -d: -f6)
  [ -e "$h/assistent/config.toml" ] || continue
  runuser -u "$u" -- env HOME="$h" python3 "$KIT/einrichten.py" --nachziehen
  install -o "$u" -g "$u" -m 644 "$KIT/systemd/assistent.service" "$KIT/systemd/google-mcp.service" "$h/.config/systemd/user/"
  systemctl --user -M "$u@" daemon-reload
  systemctl --user -M "$u@" restart assistent.service
  grep -q '^konto = ".\+"' "$h/assistent/config.toml" && systemctl --user -M "$u@" restart google-mcp.service
  echo "$u: neu gestartet"
done
