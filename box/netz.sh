#!/bin/bash
# Gemeinsame Helfer für box/*.sh, eingebunden mit `. "$(dirname "$0")/netz.sh"`.

# nochmal <befehl …>: ein Netz-Schritt bekommt nach einem Aussetzer einen zweiten Versuch. Scheitert auch der, zählt
# sein Ergebnis, das Skript bricht dann sichtbar ab, statt still weiterzulaufen.
nochmal() {
  "$@" && return 0
  local was="$*"
  echo "   Fehlgeschlagen, zweiter Versuch in ${NOCHMAL_PAUSE:-15} s: ${was:0:120}" >&2
  sleep "${NOCHMAL_PAUSE:-15}"
  "$@"
}

# workspace_mcp <venv> <anforderungen>: Google-Dienst ins venv. Das oberste Paket nur mit passender Prüfsumme
# (box/workspace-mcp.txt), danach seine Abhängigkeiten in den Grenzen, die es selbst setzt.
workspace_mcp() {
  timeout -k 30 900 "$1/bin/pip" install -q --timeout 30 --require-hashes --no-deps -r "$2" \
    && timeout -k 30 900 "$1/bin/pip" install -q --timeout 30 "$(grep -o '^workspace-mcp==[^ ]*' "$2")"
}
