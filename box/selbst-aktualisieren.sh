#!/bin/bash
# Eigener Server (box/selbst.sh): neue Fassung des Kits von selbst holen (root, assistent-aktualisieren.timer stündlich).
# Holt main aus dem Repo, aus dem die Cloud-config den Server aufgesetzt hat (/opt/assistent-quelle). Ist dort
# nichts Neues, passiert nichts, außer ein zurückgestellter Neustart ist offen (box/aktualisieren.sh). Sonst spielt
# box/aktualisieren.sh die Fassung ein, die vorher die Tests laufen lässt und bei Fehlern die bisherige behält.
# Eine Fassung bekommt drei Versuche (einen je Lauf). Scheitert sie dreimal, bleibt sie liegen, bis eine neuere kommt,
# und die Wache (box/wache.py) meldet das über den Bot der Person.
#
# Signatur: Eingespielt wird nur ein Commit, den der Release-Schlüssel signiert hat (SSH-Signatur, box/spiegel-signieren.md).
# Die erlaubten Schlüssel liest git aus der installierten Kopie /etc/pearl-kit/release-signers, nie aus dem geholten
# Stand. selbst.sh legt sie bei der Einrichtung aus dem Stand an, den die Anleitung per SHA festgelegt hat.
# Übergang für Server von vor der Signatur: Ihr altes Skript prüft nichts, die erste Fassung mit Signaturprüfung kommt
# also einmal ungeprüft herein (so wie jede Fassung vorher). Ab dem nächsten Lauf läuft dieses Skript. Fehlt dann
# /etc/pearl-kit/release-signers, kommt es aus dem installierten Kit /opt/pearl-kit (die eingespielte, vertraute
# Fassung), bevor irgendetwas Neues geprüft wird. Fehlt es auch dort, spielt das Skript nichts ein.
# Schlüsselwechsel: Ein Commit, den der bisherige Schlüssel signiert, bringt die neue box/release-signers. Nach dem
# Einspielen übernimmt dieses Skript sie nach /etc/pearl-kit, ab dann gilt der neue Schlüssel.
#   bash /opt/assistent-quelle/box/selbst-aktualisieren.sh     sofort prüfen und einspielen
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
QUELLE=${QUELLE:-/opt/assistent-quelle}
ZWEIG=${ZWEIG:-main}
STAND=/var/lib/pearl-kit
GESCHEITERT=$STAND/aktualisieren-gescheitert   # „<fassung> <versuche> [signatur]“, liest auch box/wache.py
VERSUCHE=3
KIT=/opt/pearl-kit
SIGNIERER=/etc/pearl-kit/release-signers

schluessel_bereit() {  # erlaubte Release-Schlüssel an ihrem Platz, notfalls aus dem installierten Kit (Übergang)
  [ -s "$SIGNIERER" ] && return 0
  if [ -s "$KIT/box/release-signers" ]; then
    install -D -m 644 -o root -g root "$KIT/box/release-signers" "$SIGNIERER"
    echo "Release-Schlüssel aus $KIT nach $SIGNIERER übernommen"
    return 0
  fi
  echo "Kein Release-Schlüssel in $SIGNIERER oder $KIT/box, ohne Prüfung spiele ich nichts ein" >&2
  return 1
}

signiert() {  # signiert <commit>: SSH-Signatur eines erlaubten Release-Schlüssels
  git -C "$QUELLE" -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$SIGNIERER" verify-commit "$1"
}

offene_neustarts() {  # von box/aktualisieren.sh zurückgestellte Neustarts nachholen, mit der laufenden Fassung
  if compgen -G "$STAND/neustart-offen/*" >/dev/null; then
    echo "Hole den zurückgestellten Neustart nach"
    bash "$QUELLE/box/aktualisieren.sh"
  fi
}

# Alles in Funktionen: bash hat sie ganz gelesen, bevor der Checkout unten diese Datei austauscht.
main() {
  [ -d "$QUELLE/.git" ] || { echo "$QUELLE ist kein Git-Stand, nichts zu tun"; exit 0; }
  if kill -0 "$(cat /run/pearl-kit-einrichtung 2>/dev/null || echo x)" 2>/dev/null; then
    echo "Die Einrichtung (box/selbst.sh) läuft noch, kein Update"
    exit 0
  fi
  timeout 120 git -C "$QUELLE" fetch -q origin "$ZWEIG"
  local neu alt fassung="" versuche=0
  neu=$(git -C "$QUELLE" rev-parse FETCH_HEAD)
  alt=$(git -C "$QUELLE" rev-parse HEAD)
  if [ "$neu" = "$alt" ]; then
    offene_neustarts
    exit 0
  fi
  read -r fassung versuche _ 2>/dev/null < "$GESCHEITERT" || true
  if [ "$fassung" = "$neu" ]; then
    [[ "$versuche" =~ ^[0-9]+$ ]] || versuche=1   # alte Zeile ohne Zähler: einmal gescheitert
    if [ "$versuche" -ge "$VERSUCHE" ]; then       # dreimal gescheitert, die Wache meldet es
      offene_neustarts
      exit 0
    fi
  else
    versuche=0
  fi
  if ! schluessel_bereit || ! signiert "$neu"; then   # kein zweiter Versuch: dieselbe Fassung wird nicht nachträglich echt
    install -d "$STAND"
    echo "$neu $VERSUCHE signatur" > "$GESCHEITERT"
    echo "WARNUNG: Fassung ${neu:0:7} trägt keine gültige Signatur eines Schlüssels aus $SIGNIERER." \
      "Nichts eingespielt, bleibe bei ${alt:0:7}, die Wache meldet es." >&2
    exit 1
  fi
  versuche=$((versuche + 1))
  echo "Neue Fassung ${neu:0:7} (bisher ${alt:0:7}), signiert, Versuch $versuche von $VERSUCHE"
  git -C "$QUELLE" checkout -q "$neu"
  if bash "$QUELLE/box/aktualisieren.sh"; then
    rm -f "$GESCHEITERT"
    echo "Eingespielt: ${neu:0:7}"
    # Schlüsselwechsel: die Datei kam in einem Commit, den ein bisher erlaubter Schlüssel signiert hat
    if [ -s "$QUELLE/box/release-signers" ] && ! cmp -s "$QUELLE/box/release-signers" "$SIGNIERER"; then
      install -m 644 -o root -g root "$QUELLE/box/release-signers" "$SIGNIERER"
      echo "Release-Schlüssel gewechselt, ab jetzt gilt $SIGNIERER aus ${neu:0:7}"
    fi
  else
    install -d "$STAND"
    echo "$neu $versuche" > "$GESCHEITERT"
    git -C "$QUELLE" checkout -q "$alt"
    if [ "$versuche" -ge "$VERSUCHE" ]; then
      echo "Fassung ${neu:0:7} ist $VERSUCHE-mal gescheitert und wird nicht mehr versucht, bis eine neuere kommt." \
        "Bleibe bei ${alt:0:7}, die Wache meldet es." >&2
    else
      echo "Nicht eingespielt, bleibe bei ${alt:0:7}, nächster Versuch in einer Stunde" >&2
    fi
    exit 1
  fi
}
main "$@"
exit
