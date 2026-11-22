#!/bin/bash
# Node, Codex (ChatGPT) und Gemini CLI fest nach /opt/pearl-kit-ki (als root). einrichten.sh und aktualisieren.sh
# rufen das auf. Es installiert nur, wenn eine Fassung fehlt oder hier eine neue steht. Claude Code installiert
# person-anlegen.sh je Person, weil es sich selbst aktualisiert.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
NODE=22.12.0
CODEX=0.162.1
GEMINI=0.70.0
ZIEL=/opt/pearl-kit-ki

case "$(uname -m)" in
  x86_64) NA=x64 ;;
  aarch64) NA=arm64 ;;
  *) echo "Architektur $(uname -m) wird nicht unterstützt" >&2; exit 1 ;;
esac
install -d -m 755 "$ZIEL" "$ZIEL/bin"

if [ "$("$ZIEL/node/bin/node" --version 2>/dev/null || true)" != "v$NODE" ]; then
  echo "   Node $NODE"
  TMP=$(mktemp -d)
  DATEI="node-v$NODE-linux-$NA.tar.xz"
  curl -fsSL "https://nodejs.org/dist/v$NODE/$DATEI" -o "$TMP/$DATEI"
  curl -fsSL "https://nodejs.org/dist/v$NODE/SHASUMS256.txt" -o "$TMP/SHASUMS256.txt"
  (cd "$TMP" && grep " $DATEI\$" SHASUMS256.txt | sha256sum -c --quiet -) || { echo "Prüfsumme von Node stimmt nicht" >&2; exit 1; }
  rm -rf "$ZIEL/node"
  install -d -m 755 "$ZIEL/node"
  tar -xJf "$TMP/$DATEI" -C "$ZIEL/node" --strip-components=1 --no-same-owner
  rm -rf "$TMP"
fi
ln -sf "$ZIEL/node/bin/node" "$ZIEL/bin/node"   # die Programme starten mit `env node`, PATH der Läufe enthält $ZIEL/bin
export PATH="$ZIEL/node/bin:$PATH"

version() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' \
    "$ZIEL/lib/node_modules/$1/package.json" 2>/dev/null || true
}
if [ "$(version @openai/codex)" != "$CODEX" ] || [ "$(version @google/gemini-cli)" != "$GEMINI" ]; then
  echo "   Codex $CODEX, Gemini CLI $GEMINI"
  npm install -g --prefix "$ZIEL" --no-fund --no-audit --loglevel=error "@openai/codex@$CODEX" "@google/gemini-cli@$GEMINI"
fi
chown -R root:root "$ZIEL"
chmod -R go-w "$ZIEL"
echo "   codex $("$ZIEL/bin/codex" --version 2>/dev/null | tail -1), gemini $("$ZIEL/bin/gemini" --version 2>/dev/null | tail -1)"
