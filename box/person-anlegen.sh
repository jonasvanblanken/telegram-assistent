#!/bin/bash
# Assistent für eine Person anlegen (als root).
#   bash /opt/pearl-kit/box/person-anlegen.sh <kurzname>      z. B. anna  ->  Linux-User a-anna
# Mit Einrichtungsseite (PFORTE_DOMAIN in /etc/pearl-kit/box.env) braucht es nur den Token eines neuen Bots
# (BotFather). Am Ende steht ein Einladungslink mit QR-Code: Die Person tippt ihn an, dann auf Starten, und richtet
# in Telegram Namen, KI und Google selbst ein. Ohne Einrichtungsseite fragt das Skript Telegram-ID, Namen und
# Google-Konto ab, die KI meldest du hier im Terminal an, die Person schickt dir dafür einen Code.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }
[ $# = 1 ] && [[ "$1" =~ ^[a-z][a-z0-9]{1,15}$ ]] || { echo "Aufruf: $0 <kurzname> (klein, nur a-z und 0-9)" >&2; exit 2; }
KIT=/opt/pearl-kit
KI_BIN=/opt/pearl-kit-ki/bin
U="a-$1"
H="/home/$U"
DATEN="$H/.local/share/assistent"
als() { runuser -u "$U" -- env HOME="$H" PATH="$H/.local/bin:$KI_BIN:/usr/local/bin:/usr/bin:/bin" "$@"; }
frage() { local x; read -r -p "$1: " x; echo "$x"; }
geheim() { local x; read -r -s -p "$1: " x; echo >&2; echo "$x"; }
frei() {  # freier Port im Bereich $1-$2, über alle Instanzen und laufenden Dienste
  local p=$1
  while grep -qs "^port = $p$" /home/a-*/assistent/config.toml || ss -ltn | grep -q ":$p "; do p=$((p + 1)); done
  [ "$p" -le "$2" ] || { echo "Keine freien Ports mehr in $1-$2" >&2; exit 1; }
  echo "$p"
}
DOMAIN=$(sed -n 's/^PFORTE_DOMAIN=//p' /etc/pearl-kit/box.env 2>/dev/null | tr -d ' "'"'"'')
BETREUER=$(sed -n 's/^BETREUER=//p' /etc/pearl-kit/box.env 2>/dev/null | tr -d '"'"'"'')
# Eigener Server der Person (box/selbst.sh): kein Betreuer, Token und Einladungscode kommen aus der Umgebung,
# nichts wird abgefragt.
SELBST=${SELBST:-}
if [ -n "$SELBST" ]; then
  [ -n "$DOMAIN" ] && [ -n "${NEU_TG_TOKEN:-}" ] || { echo "SELBST braucht PFORTE_DOMAIN und NEU_TG_TOKEN" >&2; exit 1; }
fi

echo "== Linux-User $U"
if ! id "$U" >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G assistenten "$U"
fi
chmod 700 "$H"
loginctl enable-linger "$U"

echo "== Claude Code für $U (ChatGPT und Gemini liegen fest in /opt/pearl-kit-ki)"
if [ ! -x "$H/.local/bin/claude" ]; then
  als bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null
fi
als "$H/.local/bin/claude" --version
[ -x "$KI_BIN/codex" ] && [ -x "$KI_BIN/gemini" ] || { echo "Codex oder Gemini fehlt, erst box/einrichten.sh laufen lassen" >&2; exit 1; }

if [ ! -e "$H/assistent/config.toml" ] && [ -n "$DOMAIN" ]; then
  echo "== Einladung (Namen, KI und Google richtet die Person selbst ein)"
  if [ -z "$BETREUER" ] && [ -z "$SELBST" ]; then
    BETREUER=$(frage "Dein Name als Betreuer (wird für alle weiteren Personen gemerkt)")
    printf 'BETREUER="%s"\n' "$BETREUER" >> /etc/pearl-kit/box.env
  fi
  WPORT=$(frei 8201 8299)
  ARGS=(--web-adresse "https://$DOMAIN/$1" --web-port "$WPORT")
  [ -n "$SELBST" ] || ARGS+=(--admin "$BETREUER")
  if [ -n "${NEU_KITS:-}" ]; then ARGS+=(--kits "$NEU_KITS"); fi   # vorab im Browser gewählte Kits (box/selbst.sh)
  if [ -r /etc/pearl-kit/google-client.json ]; then
    PORT=$(frei 8101 8199)
    ARGS+=(--port "$PORT")
    echo "   Google-Dienst bekommt Port $PORT. Im Google-Cloud-Projekt muss https://$DOMAIN/$1/oauth2callback als Redirect-URI stehen."
  else
    echo "   Ohne /etc/pearl-kit/google-client.json bietet die Seite keinen Kalender und keine Mail an."
  fi
  als python3 "$KIT/einrichten.py" "${ARGS[@]}"
elif [ ! -e "$H/assistent/config.toml" ]; then
  echo "== Angaben zur Person"
  PERSON=$(frage "Vorname der Person (so spricht der Assistent sie an)")
  TID=$(frage "Telegram-ID der Person (Zahl, z. B. über @userinfobot)")
  [[ "$TID" =~ ^[0-9]+$ ]] || { echo "Telegram-ID muss eine Zahl sein" >&2; exit 1; }
  NAME=$(frage "Name des Assistenten (wählt die Person)")
  ADMIN=$(frage "Dein Name als Betreuer")
  GOOGLE=$(frage "Google-Konto der Person (leer = ohne Google)")
  ARGS=(--person "$PERSON" --telegram-id "$TID" --assistent "$NAME" --admin "$ADMIN")
  KI=$(frage "KI der Person: claude, chatgpt oder gemini")
  [[ "$KI" =~ ^(claude|chatgpt|gemini)$ ]] || { echo "Bitte claude, chatgpt oder gemini" >&2; exit 1; }
  ARGS+=(--anbieter "$KI")
  if [ -n "$GOOGLE" ]; then
    PORT=$(frei 8101 8199)
    ARGS+=(--google "$GOOGLE" --port "$PORT")
    echo "   Google-Dienst bekommt Port $PORT. Im Google-Cloud-Projekt muss http://localhost:$PORT/oauth2callback als Redirect-URI stehen."
  fi
  als python3 "$KIT/einrichten.py" "${ARGS[@]}"
else
  echo "== Instanz gibt es schon, ziehe nur die Technik nach"
  als python3 "$KIT/einrichten.py" --nachziehen
fi
wert() {  # wert <abschnitt> <schlüssel>: aus config.toml der Instanz
  python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get(sys.argv[2], {}).get(sys.argv[3], ""))' \
    "$H/assistent/config.toml" "$1" "$2"
}
KI=$(wert ki anbieter)
WPORT=$(wert web port)

SEC="$H/.config/assistent/secrets.env"
setze() {  # setze KEY WERT: Zeile in secrets.env ersetzen oder anhängen, ohne den Wert in die Prozessliste zu geben
  local tmp; tmp=$(mktemp -p "$(dirname "$SEC")")
  grep -v "^$1=" "$SEC" > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  chown "$U:$U" "$tmp"; chmod 600 "$tmp"; mv "$tmp" "$SEC"
}
hat() { grep -q "^$1=.\+" "$SEC"; }

echo "== Schlüssel"
if [ -n "${NEU_TG_TOKEN:-}" ]; then
  setze TG_TOKEN "$NEU_TG_TOKEN"
elif ! hat TG_TOKEN; then
  setze TG_TOKEN "$(geheim "Token des neuen Telegram-Bots (BotFather, wird nicht angezeigt)")"
fi
if [ -n "${NEU_EINLADUNG:-}" ] && ! grep -q '^telegram_id = [1-9]' "$H/assistent/config.toml"; then
  setze EINLADUNG "$NEU_EINLADUNG"   # Code aus dem Link, den die Anleitung im Browser schon gezeigt hat
fi
if ! hat GOOGLE_MCP_TOKEN; then
  setze GOOGLE_MCP_TOKEN "$(openssl rand -hex 32)"
fi
if ! hat OPENAI_API_KEY && [ -z "$SELBST" ]; then
  K=$(geheim "OpenAI-Key für Sprachnachrichten (leer = ohne)")
  [ -n "$K" ] && setze OPENAI_API_KEY "$K"
fi

if [ -z "$DOMAIN" ]; then   # ohne Einrichtungsseite: KI hier im Terminal anmelden
  case "$KI" in
    claude)
      if ! hat CLAUDE_CODE_OAUTH_TOKEN; then
        cat <<TXT

   Claude-Login: Gleich startet 'claude setup-token' als $U. Den Link, den es zeigt, schickst du der Person.
   Sie meldet sich mit IHREM Claude-Konto (Pro oder Max) an und schickt dir den Code, den du hier einfügst.
   Am Ende zeigt claude einen langen Token (sk-ant-oat…). Den kopierst du, er wird danach abgefragt.

TXT
        read -r -p "Enter, wenn die Person bereit ist " _
        als "$H/.local/bin/claude" setup-token || true
        T=$(geheim "Token aus setup-token einfügen")
        [[ "$T" == sk-ant-* ]] || { echo "Das sieht nicht nach einem Token aus, bitte Skript noch einmal starten" >&2; exit 1; }
        setze CLAUDE_CODE_OAUTH_TOKEN "$T"
        setze CLAUDE_TOKEN_SEIT "$(date +%F)"
      fi ;;
    chatgpt)
      if [ ! -e "$DATEN/codex/auth.json" ]; then
        cat <<TXT

   ChatGPT-Login: Gleich zeigt Codex einen Link und einen Code. Beides schickst du der Person. Sie öffnet den
   Link, meldet sich mit IHREM ChatGPT-Konto (Plus, Pro oder Business) an und tippt den Code ein. Danach geht es
   hier von selbst weiter. Meldet OpenAI, die Geräte-Anmeldung sei aus: in ChatGPT unter Einstellungen,
   Sicherheit einschalten.

TXT
        read -r -p "Enter, wenn die Person bereit ist " _
        als env CODEX_HOME="$DATEN/codex" "$KI_BIN/codex" login --device-auth
      fi ;;
    gemini)
      if [ ! -e "$DATEN/gemini/.gemini/oauth_creds.json" ]; then
        cat <<TXT

   Gemini-Login: Gleich startet Gemini und zeigt einen Link. Den schickst du der Person. Sie meldet sich mit
   ihrem PRIVATEN Google-Konto an (Workspace-Konten brauchen ein eigenes Cloud-Projekt) und schickt dir den Code,
   den du hier einfügst. Wenn Gemini danach sein Eingabefeld zeigt, beendest du es mit /quit.

TXT
        read -r -p "Enter, wenn die Person bereit ist " _
        install -d -o "$U" -g "$U" -m 700 "$DATEN/gemini/anmeldung"
        (cd "$DATEN/gemini/anmeldung" && als env GEMINI_CLI_HOME="$DATEN/gemini" NO_BROWSER=true "$KI_BIN/gemini") || true
        [ -e "$DATEN/gemini/.gemini/oauth_creds.json" ] || { echo "Gemini ist nicht angemeldet, bitte Skript noch einmal starten" >&2; exit 1; }
      fi ;;
  esac
fi

if [ -n "$DOMAIN" ] && [ -n "$WPORT" ] && [ "$WPORT" != 0 ]; then
  echo "== Einrichtungsseite https://$DOMAIN/$1"
  cat > "/etc/caddy/pearl-kit/$1.caddy" <<CADDY
handle_path /$1/* {
	reverse_proxy 127.0.0.1:$WPORT
}
CADDY
  systemctl reload caddy
fi

echo "== Dienste"
install -d -o "$U" -g "$U" -m 700 "$H/.config/systemd/user"
install -o "$U" -g "$U" -m 644 "$KIT/systemd/assistent.service" "$KIT/systemd/google-mcp.service" "$H/.config/systemd/user/"
systemctl start "user@$(id -u "$U").service"
systemctl --user -M "$U@" daemon-reload
if grep -q '^konto = ".\+"' "$H/assistent/config.toml"; then
  [ -r /etc/pearl-kit/google-client.json ] || echo "   WARNUNG: /etc/pearl-kit/google-client.json fehlt, Google startet erst danach"
  systemctl --user -M "$U@" enable --now google-mcp.service
  sleep 4
fi
systemctl --user -M "$U@" enable --now assistent.service

echo "== Selbsttest"
sleep 3
if als python3 "$KIT/assistent.py" pruefen; then
  echo
  LINK=$(als python3 "$KIT/assistent.py" einladung 2>/dev/null | tail -1)
  if [ -n "$DOMAIN" ] && [[ "$LINK" == https://t.me/* ]]; then
    echo "Fertig. Schick der Person diesen Link (oder lass sie den Code scannen). Sie tippt auf Starten,"
    echo "dann auf Einrichten, und ist in drei Minuten durch:"
    echo
    echo "   $LINK"
    echo
    command -v qrencode >/dev/null && qrencode -t ANSIUTF8 "$LINK"
    echo "Der Link gilt, bis sich das erste Telegram-Konto damit meldet. Neu ausgeben:"
    echo "   runuser -u $U -- env HOME=$H python3 $KIT/assistent.py einladung"
  else
    echo "Fertig. Die Person schreibt ihrem Bot jetzt /start."
    if grep -q '^konto = ".\+"' "$H/assistent/config.toml"; then
      echo "Danach im Chat /google schicken, um Kalender und Mail zu verbinden."
    fi
  fi
  if [ -n "$DOMAIN" ]; then
    echo "Von außen prüfen: https://$DOMAIN/$1/einrichten muss „Abgelaufen“ zeigen."
  fi
else
  echo
  echo "Selbsttest mit Fehlern (siehe oben). Klappt der KI-Lauf mit rechte = \"auto\" nicht, in"
  echo "$H/assistent/config.toml unter [ki] rechte = \"dontAsk\" (Claude) oder \"voll\" (ChatGPT, Gemini) setzen"
  echo "und dieses Skript noch einmal starten."
  exit 1
fi
