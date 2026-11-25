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
. "$(dirname "$0")/netz.sh"

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
  curl -fsSL --connect-timeout 20 -m 600 --retry 2 --retry-delay 10 "https://nodejs.org/dist/v$NODE/$DATEI" -o "$TMP/$DATEI"
  curl -fsSL --connect-timeout 20 -m 60 --retry 2 --retry-delay 10 "https://nodejs.org/dist/v$NODE/SHASUMS256.txt" -o "$TMP/SHASUMS256.txt"
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
  nochmal timeout -k 30 900 npm install -g --prefix "$ZIEL" --no-fund --no-audit --loglevel=error --fetch-timeout=60000 \
    "@openai/codex@$CODEX" "@google/gemini-cli@$GEMINI"
fi
chown -R root:root "$ZIEL"
chmod -R go-w "$ZIEL"

# rclone spiegelt home/ auf Wunsch nach Google Drive, Dropbox oder OneDrive (ablage.py). Ein Programm für alle drei.
if ! command -v rclone >/dev/null && command -v apt-get >/dev/null; then
  echo "   rclone für die Ablage"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=300 -o Acquire::http::Timeout=30 rclone >/dev/null \
    || echo "   rclone ließ sich nicht installieren, die Ablage in der Cloud fehlt dann" >&2
fi

# Ubuntu ab 24.04 sperrt User-Namespaces für normale Programme. Die Sandbox von Codex (bubblewrap) braucht sie,
# sonst scheitert jeder Befehl unter ChatGPT („bwrap: setting up uid map: Permission denied“) und der Assistent
# kann keine Datei lesen. Weg nach der Codex-Doku (developers.openai.com/codex/concepts/sandboxing): bubblewrap aus
# dem System (Codex nimmt das erste bwrap im PATH) und Ubuntus Profil bwrap-userns-restrict laden. Die Sperre für
# alle anderen Programme bleibt an. Dazu ein Profil nur für das Codex-Programm, falls es doch sein eigenes bwrap nimmt.
SPERRE=$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)
if [ "$SPERRE" = 1 ] && command -v apt-get >/dev/null; then
  if ! command -v bwrap >/dev/null \
     || { [ ! -f /usr/share/apparmor/extra-profiles/bwrap-userns-restrict ] && [ ! -f /etc/apparmor.d/bwrap-userns-restrict ]; }; then
    echo "   bubblewrap und AppArmor-Profile für die Sandbox von Codex"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=300 -o Acquire::http::Timeout=30 bubblewrap apparmor-profiles apparmor-utils >/dev/null \
      || echo "   bubblewrap ließ sich nicht installieren" >&2
  fi
  if [ ! -f /etc/apparmor.d/bwrap-userns-restrict ] && [ -f /usr/share/apparmor/extra-profiles/bwrap-userns-restrict ]; then
    install -m 0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/bwrap-userns-restrict
  fi
  if [ -f /etc/apparmor.d/bwrap-userns-restrict ] && command -v apparmor_parser >/dev/null; then
    apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict || echo "   Profil bwrap-userns-restrict ließ sich nicht laden" >&2
  fi
fi
CODEX_BIN=$(find "$ZIEL/lib/node_modules/@openai" -type f -path "*/vendor/*/bin/codex" 2>/dev/null | head -1)
if [ -n "$CODEX_BIN" ] && [ "$SPERRE" = 1 ] && command -v apparmor_parser >/dev/null; then
  PROFIL=/etc/apparmor.d/pearl-kit-codex
  INHALT="abi <abi/4.0>,
include <tunables/global>

profile pearl-kit-codex \"$CODEX_BIN\" flags=(unconfined) {
  userns,
  include if exists <local/pearl-kit-codex>
}"
  if [ "$(cat "$PROFIL" 2>/dev/null)" != "$INHALT" ] || ! grep -q "^pearl-kit-codex " /sys/kernel/security/apparmor/profiles 2>/dev/null; then
    printf '%s\n' "$INHALT" > "$PROFIL"
    if apparmor_parser -r "$PROFIL"; then
      echo "   AppArmor: Sandbox von Codex darf Namespaces anlegen"
    else
      echo "   AppArmor-Profil für Codex ließ sich nicht laden, ChatGPT kann dann keine Dateien lesen" >&2
    fi
  fi
fi
# Probe als normaler Nutzer: kommt die Sandbox hoch, kann ChatGPT Dateien lesen
if [ -x "$ZIEL/bin/codex" ] && id nobody >/dev/null 2>&1; then
  PROBE=$(mktemp -d); chown nobody "$PROBE"
  if (cd "$PROBE" && runuser -u nobody -- env HOME="$PROBE" CODEX_HOME="$PROBE" PATH="$ZIEL/bin:/usr/bin:/bin" \
        "$ZIEL/bin/codex" sandbox -- /bin/true) >/dev/null 2>&1; then
    echo "   Sandbox von Codex: läuft"
  else
    echo "   Sandbox von Codex kommt nicht hoch, ChatGPT kann so keine Dateien lesen" >&2
  fi
  rm -rf "$PROBE"
fi
echo "   codex $("$ZIEL/bin/codex" --version 2>/dev/null | tail -1), gemini $("$ZIEL/bin/gemini" --version 2>/dev/null | tail -1)"
