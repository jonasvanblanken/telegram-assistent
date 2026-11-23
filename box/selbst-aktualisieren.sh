#!/bin/bash
# Eigener Server (box/selbst.sh): neue Fassung des Kits von selbst holen (root, assistent-aktualisieren.timer stündlich).
# Holt main aus dem Repo, aus dem die Cloud-config den Server aufgesetzt hat (/opt/assistent-quelle). Ist dort
# nichts Neues, passiert nichts. Sonst spielt box/aktualisieren.sh die Fassung ein, die vorher die Tests laufen
# lässt und bei Fehlern nichts anfasst. Eine Fassung, die einmal gescheitert ist, wird nicht stündlich neu versucht.
#   bash /opt/assistent-quelle/box/selbst-aktualisieren.sh     sofort prüfen und einspielen
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
QUELLE=${QUELLE:-/opt/assistent-quelle}
ZWEIG=${ZWEIG:-main}
GESCHEITERT=/var/lib/pearl-kit/aktualisieren-gescheitert
[ -d "$QUELLE/.git" ] || { echo "$QUELLE ist kein Git-Stand, nichts zu tun"; exit 0; }
git -C "$QUELLE" fetch -q origin "$ZWEIG"
NEU=$(git -C "$QUELLE" rev-parse FETCH_HEAD)
ALT=$(git -C "$QUELLE" rev-parse HEAD)
[ "$NEU" = "$ALT" ] && exit 0
[ "$(cat "$GESCHEITERT" 2>/dev/null)" = "$NEU" ] && exit 0
echo "Neue Fassung ${NEU:0:7} (bisher ${ALT:0:7})"
git -C "$QUELLE" checkout -q "$NEU"
if bash "$QUELLE/box/aktualisieren.sh"; then
  rm -f "$GESCHEITERT"
  echo "Eingespielt: ${NEU:0:7}"
else
  install -d /var/lib/pearl-kit
  echo "$NEU" > "$GESCHEITERT"
  git -C "$QUELLE" checkout -q "$ALT"
  echo "Nicht eingespielt, bleibe bei ${ALT:0:7}" >&2
  exit 1
fi
