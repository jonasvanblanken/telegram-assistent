#!/bin/bash
# Sprachnachrichten auf dem Server selbst: faster-whisper mit dem Modell small nach /opt/pearl-kit-ki/sprache (als
# root). Nichts verlässt den Server, die Person braucht keinen Key. ki-programme.sh ruft es bei Einrichtung und Update
# auf, selbst-aktualisieren.sh mit --nachholen, solange es fehlt (dann höchstens alle drei Stunden ein Versuch).
# Installiert wird nur, wenn die Fassung fehlt oder sich geändert hat, erst in einen Nebenordner, dann getauscht.
# Pakete: box/sprache.txt (mit Prüfsumme) und box/sprache-pakete.txt. Modell: feste Revision, jede Datei mit Prüfsumme.
# Scheitert es, steht der Grund in /opt/pearl-kit-ki/sprache-fehler, der Assistent sagt es der Person.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
HIER=$(cd "$(dirname "$0")" && pwd)
. "$HIER/netz.sh"
ZIEL=${SPRACHE_ZIEL:-/opt/pearl-kit-ki/sprache}
NEU="$ZIEL.neu"
FEHLER="$ZIEL-fehler"
PAUSE=$((3 * 3600))
PLATZ_MB=1800   # gemessen: Umgebung 400 MB, Modell 480 MB, dazu Puffer für pip
REV=536b0662742c02347bc0e980a01041f333bce120   # Systran/faster-whisper-small auf Hugging Face
QUELLE="https://huggingface.co/Systran/faster-whisper-small/resolve/$REV"
DATEIEN="config.json b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828
model.bin 3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671
tokenizer.json fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab
vocabulary.txt 34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"
FASSUNG="$( (cat "$HIER/sprache.txt" "$HIER/sprache-pakete.txt" "$HIER/../sprache.py"; echo "$DATEIEN") | sha256sum | cut -c1-16)-${REV:0:12}"

if [ "$(cat "$ZIEL/FASSUNG" 2>/dev/null)" = "$FASSUNG" ] && [ -x "$ZIEL/bin/python3" ]; then
  rm -f "$FEHLER"
  exit 0
fi
if [ "${1:-}" = "--nachholen" ] && [ -e "$FEHLER" ] && [ $(($(date +%s) - $(stat -c %Y "$FEHLER"))) -lt "$PAUSE" ]; then
  exit 0
fi

gescheitert() {  # gescheitert <grund> <text>
  rm -rf "$NEU"
  echo "$1 $(date +%F)" > "$FEHLER"
  chmod 644 "$FEHLER"
  echo "   Sprachnachrichten: $2" >&2
  exit 1
}

rm -rf "$NEU"
install -d -m 755 "$(dirname "$ZIEL")"
FREI=$(df -Pm "$(dirname "$ZIEL")" | awk 'NR == 2 {print $4}')
[ "$FREI" -ge "$PLATZ_MB" ] || gescheitert platz "zu wenig Platz, $FREI MB frei, $PLATZ_MB MB nötig"
echo "   Sprachnachrichten: faster-whisper und Modell small (rund 900 MB)"
python3 -m venv "$NEU" || gescheitert python "python3 -m venv geht nicht"
# Erst die festen Pakete, dann faster-whisper ohne Abhängigkeiten. So herum meldet pip das fehlende onnxruntime nicht.
nochmal timeout -k 30 900 "$NEU/bin/pip" install -q --timeout 30 -r "$HIER/sprache-pakete.txt" \
  || gescheitert download "die Pakete für faster-whisper ließen sich nicht laden"
nochmal timeout -k 30 900 "$NEU/bin/pip" install -q --timeout 30 --require-hashes --no-deps -r "$HIER/sprache.txt" \
  || gescheitert download "faster-whisper ließ sich nicht laden"
install -d -m 755 "$NEU/modell"
while read -r datei summe; do
  nochmal curl -fsSL --connect-timeout 20 -m 900 --retry 2 --retry-delay 10 "$QUELLE/$datei" -o "$NEU/modell/$datei" \
    || gescheitert download "Modell ($datei) ließ sich nicht laden"
  echo "$summe  $NEU/modell/$datei" | sha256sum -c --quiet - >/dev/null 2>&1 \
    || gescheitert pruefsumme "Prüfsumme von $datei stimmt nicht"
done <<< "$DATEIEN"

# Probe: eine Sekunde Stille. Kommt sprache.py durch (0) oder hört nichts (3), läuft alles, was es braucht.
PROBE=$(mktemp --suffix .wav)
python3 -c 'import sys, wave
w = wave.open(sys.argv[1], "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(bytes(32000))' "$PROBE"
set +e
timeout 300 "$NEU/bin/python3" -I "$HIER/../sprache.py" "$PROBE" --modell "$NEU/modell" --kerne 1 >/dev/null 2>"$PROBE.log"
ERGEBNIS=$?
set -e
if [ "$ERGEBNIS" != 0 ] && [ "$ERGEBNIS" != 3 ]; then
  tail -3 "$PROBE.log" >&2
  rm -f "$PROBE" "$PROBE.log"
  gescheitert probe "Probe mit sprache.py gescheitert ($ERGEBNIS)"
fi
rm -f "$PROBE" "$PROBE.log"

echo "$FASSUNG" > "$NEU/FASSUNG"
chown -R root:root "$NEU"
chmod -R go-w,a+rX "$NEU"
rm -rf "$ZIEL.alt"
[ ! -e "$ZIEL" ] || mv "$ZIEL" "$ZIEL.alt"
mv "$NEU" "$ZIEL"
rm -rf "$ZIEL.alt"
rm -f "$FEHLER"
echo "   Sprachnachrichten: bereit"
