#!/bin/bash
# Nur root darf den Metadaten-Dienst des Anbieters (169.254.169.254) erreichen (Aufruf als root).
# Dort bleibt die Cloud-config mit dem Bot-Token lesbar, solange der Server lebt (Hetzner /hetzner/v1/userdata,
# ähnlich bei DigitalOcean und IONOS). Ohne Sperre könnte jeder Prozess der Person, auch die KI, den Token holen.
# Die Regel steht in /etc/ufw/before.rules, ufw lädt sie bei jedem Start und Reload. Mehrfach aufrufen schadet nicht.
# selbst.sh ruft das bei der Einrichtung auf, aktualisieren.sh auf eigenen Servern bei jedem Update.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
REGELN=/etc/ufw/before.rules
MARKE="# pearl-kit: Metadaten-Dienst nur für root (box/metadaten-sperre.sh)"
REGEL=(-d 169.254.169.254/32 -m owner ! --uid-owner 0 -j REJECT)

einfuegen() {  # einfuegen <datei> <marke> <regel>: Regel mit Marke vor das letzte COMMIT (schließt die Tabelle filter)
  awk -v m="$2" -v r="$3" '
    { z[NR] = $0; if ($0 == "COMMIT") c = NR }
    END { if (!c) exit 1; for (i = 1; i <= NR; i++) { if (i == c) { print m; print r } print z[i] } }' "$1"
}

[ -f "$REGELN" ] || { echo "$REGELN fehlt, ufw ist nicht installiert" >&2; exit 1; }
if ! grep -qF -- "$MARKE" "$REGELN"; then
  tmp=$(mktemp)
  einfuegen "$REGELN" "$MARKE" "-A ufw-before-output ${REGEL[*]}" > "$tmp" || { rm -f "$tmp"; echo "Kein COMMIT in $REGELN" >&2; exit 1; }
  cp -p "$REGELN" "$REGELN.vor-pearl-kit"
  cat "$tmp" > "$REGELN"   # Rechte und Besitzer der Datei bleiben
  rm -f "$tmp"
fi
ufw status | grep -q '^Status: active' || { echo "ufw ist aus, die Sperre greift erst mit ufw enable" >&2; exit 1; }
if ! ufw reload >/dev/null; then   # lehnt ufw die Datei ab, gilt wieder die alte, die Firewall bleibt an
  [ -f "$REGELN.vor-pearl-kit" ] && cat "$REGELN.vor-pearl-kit" > "$REGELN" && ufw reload >/dev/null
  echo "ufw hat die Sperre abgelehnt, $REGELN ist wie vorher" >&2
  exit 1
fi
iptables -C ufw-before-output "${REGEL[@]}" 2>/dev/null || { echo "Sperre für 169.254.169.254 ist nicht aktiv" >&2; exit 1; }
echo "   Metadaten-Dienst nur für root erreichbar"
