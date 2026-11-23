#!/bin/bash
# WhatsApp-Erweiterung auf dem eigenen Server der Person (box/selbst.sh), als root.
#
# Die Person schickt ihrem Assistenten /whatsapp. Der Dienst schreibt dann einen Auftrag nach
#   /home/a-ich/.local/state/assistent/whatsapp-auftrag     (Inhalt: an, aus)
# und assistent-whatsapp.path startet dieses Skript. Es liest den Auftrag mit den Rechten der Person, löscht ihn und
# meldet den Stand in /var/lib/pearl-kit/whatsapp/stand (eine Zeile, lesbar für alle auf dem Server):
#   baut           Bridge und Connector werden eingerichtet (beim ersten Mal 10 bis 15 Minuten)
#   bereit         läuft, wartet darauf, dass das Handy den QR-Code scannt
#   verknuepft     gescannt, die Kontakte kommen an
#   fertig         Bridge neu gestartet, Chats haben Namen, der Assistent kann loslegen
#   aus            entfernt
#   fehler: …      ist schiefgegangen, der Satz geht so an die Person
#
#   bash /opt/pearl-kit/box/whatsapp.sh auftrag      was der Dienst bestellt hat (systemd)
#   bash /opt/pearl-kit/box/whatsapp.sh an           von Hand einrichten
#   bash /opt/pearl-kit/box/whatsapp.sh aus          von Hand entfernen
#
# Bridge: adelaidasofia/whatsapp-mcp (MIT), Tag v0.4.1 mit den Patches aus whatsapp/patches/, als verknüpftes Gerät,
# nur lesend. Bridge (8080) und Connector (8081) hören nur auf 127.0.0.1, kein nginx, keine Subdomain. Die
# Nachrichtendatenbank ist mit SQLCipher verschlüsselt. Transkription bleibt aus.
# Nur für den eigenen Server einer Person: Auf einer Box mit mehreren Personen könnte jeder Linux-User die Bridge
# auf 127.0.0.1 lesen.
set -Eeuo pipefail
[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }

KIT=${KIT:-/opt/pearl-kit}
BRIDGE_VERSION=${BRIDGE_VERSION:-v0.4.1}
GO_VERSION=${GO_VERSION:-1.27.0}
REPO_URL=https://github.com/adelaidasofia/whatsapp-mcp.git
APP_USER=whatsapp
OPT_DIR=/opt/whatsapp-mcp
DATA_DIR=/var/lib/whatsapp-mcp
CONF_DIR=/etc/whatsapp-mcp
STAND_DIR=/var/lib/pearl-kit/whatsapp
STAND=$STAND_DIR/stand
API=http://127.0.0.1:8080/api

person() {   # der eine Assistent auf dem eigenen Server
  local u
  u=$(sed -n 's/^WACHE_SELBST=//p' /etc/pearl-kit/box.env 2>/dev/null | tr -d ' "')
  [[ "$u" =~ ^a-[a-z0-9-]+$ ]] && id -u "$u" >/dev/null 2>&1 || return 1
  echo "$u"
}

stand() {
  install -d -m 755 "$STAND_DIR"
  echo "$1" > "$STAND.tmp" && chmod 644 "$STAND.tmp" && mv "$STAND.tmp" "$STAND"
  echo "== $1"
}

SCHRITT="Start"
trap 'stand "fehler: Die Einrichtung ist bei „$SCHRITT“ stehen geblieben. Schick /whatsapp_neu, dann versuche ich es noch einmal."' ERR

auth_state() {
  curl -fsS -m 5 "$API/status" 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("auth_state",""))' 2>/dev/null || true
}

patch_stand() {
  echo "$BRIDGE_VERSION-$(cat "$KIT"/whatsapp/patches/* "$KIT"/whatsapp/server_http.py | sha256sum | cut -c1-12)"
}

bauen() {
  local arch tmp goenv
  arch=$(dpkg --print-architecture)
  [[ "$arch" =~ ^(amd64|arm64)$ ]] || { stand "fehler: Dieser Server hat eine Bauart, auf der die WhatsApp-Verbindung nicht läuft ($arch)."; exit 1; }

  SCHRITT="Speicher"
  if [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 6000000 ] && ! swapon --show | grep -q .; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi

  SCHRITT="Programme"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl git ca-certificates jq build-essential qrencode >/dev/null

  SCHRITT="Ordner"
  id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
  mkdir -p "$OPT_DIR/bin" "$OPT_DIR/app" "$DATA_DIR/store" "$DATA_DIR/media" "$DATA_DIR/uv-cache" "$CONF_DIR"
  chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
  chmod 700 "$DATA_DIR" "$CONF_DIR"

  if [ "$(cat "$OPT_DIR/stand" 2>/dev/null)" = "$(patch_stand)" ] \
      && [ -x "$OPT_DIR/bin/whatsapp-bridge" ] && [ -x "$OPT_DIR/app/whatsapp-mcp-server/.venv/bin/python" ]; then
    echo "   Bridge $BRIDGE_VERSION ist schon gebaut"
  else
    SCHRITT="Quellcode"
    if [ -d "$OPT_DIR/app/.git" ]; then
      git -C "$OPT_DIR/app" fetch --tags --quiet
      git -C "$OPT_DIR/app" checkout --quiet --force "$BRIDGE_VERSION"
      git -C "$OPT_DIR/app" clean -qfd whatsapp-bridge
    else
      git clone --quiet "$REPO_URL" "$OPT_DIR/app"
      git -C "$OPT_DIR/app" checkout --quiet "$BRIDGE_VERSION"
    fi

    SCHRITT="Bridge bauen"
    tmp=$(mktemp -d)
    if [ "$(/opt/go/bin/go version 2>/dev/null | awk '{print $3}')" != "go$GO_VERSION" ]; then
      rm -rf /opt/go
      curl -fsSL "https://go.dev/dl/go$GO_VERSION.linux-$arch.tar.gz" -o "$tmp/go.tar.gz"
      tar -C /opt -xzf "$tmp/go.tar.gz"
    fi
    bash "$KIT/whatsapp/patches/apply.sh" "$OPT_DIR/app"
    goenv=(GOROOT=/opt/go GOPATH="$tmp/gopath" GOCACHE="$tmp/gocache" GOFLAGS=-modcacherw)
    ( cd "$OPT_DIR/app/whatsapp-bridge" && env "${goenv[@]}" /opt/go/bin/go test ./... >/dev/null )
    ( cd "$OPT_DIR/app/whatsapp-bridge" && env "${goenv[@]}" /opt/go/bin/go build -o "$tmp/whatsapp-bridge" . )
    install -m 0755 -o root -g root "$tmp/whatsapp-bridge" "$OPT_DIR/bin/whatsapp-bridge"

    SCHRITT="Connector"
    install -m 0644 "$KIT/whatsapp/server_http.py" "$OPT_DIR/app/whatsapp-mcp-server/server_http.py"
    [ -x "$OPT_DIR/bin/uv" ] || curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$OPT_DIR/bin" sh >/dev/null
    ( cd "$OPT_DIR/app/whatsapp-mcp-server" && UV_CACHE_DIR="$DATA_DIR/uv-cache" "$OPT_DIR/bin/uv" sync --quiet )
    chown -R "$APP_USER:$APP_USER" "$OPT_DIR/app/whatsapp-mcp-server/.venv" "$DATA_DIR/uv-cache"
    rm -rf "$tmp"
    patch_stand > "$OPT_DIR/stand"
  fi

  SCHRITT="Schlüssel"
  # Einmal erzeugt, nie neu: ein neuer DB-Schlüssel macht die Nachrichten unlesbar, ein neues Token trennt den Assistenten.
  if [ ! -f "$CONF_DIR/bridge.env" ]; then
    cat > "$CONF_DIR/bridge.env" <<ENV
WHATSAPP_BRIDGE_HOST=127.0.0.1
WHATSAPP_BRIDGE_PORT=8080
WHATSAPP_DB_PATH=$DATA_DIR/store/messages.db
WHATSAPP_MEDIA_PATH=$DATA_DIR/media
WHATSAPP_BACKUP_PATH=$DATA_DIR/store/backups
WHATSAPP_ENCRYPT_DB=true
WHATSAPP_DB_KEY=$(openssl rand -hex 32)
WHATSAPP_VAULT_CRM_PATH=
WHATSAPP_WHISPER_BACKEND=off
WHATSAPP_WHISPER_API_KEY=
WHATSAPP_WHISPER_API_MODEL=whisper-1
WHATSAPP_WHISPER_LANGUAGE=auto
WHATSAPP_WHISPER_ONLY_CHATS=
WHATSAPP_WHISPER_BIN_PATH=
WHATSAPP_WHISPER_MODEL_PATH=
WHATSAPP_FFMPEG_BIN_PATH=
WHATSAPP_SCRUB_PROMPT_INJECTION=true
WHATSAPP_AUDIT_LOG=true
WHATSAPP_AUDIT_LOG_PATH=$DATA_DIR/audit.log
WHATSAPP_ALLOWED_ORIGINS=
WHATSAPP_AUTO_DOWNLOAD_MEDIA=false
ENV
  fi
  if [ ! -f "$CONF_DIR/mcp.env" ]; then
    cat > "$CONF_DIR/mcp.env" <<ENV
WHATSAPP_MCP_TOKEN=$(openssl rand -hex 32)
WHATSAPP_MCP_HTTP_HOST=127.0.0.1
WHATSAPP_MCP_HTTP_PORT=8081
WHATSAPP_MCP_HTTP_PATH=/mcp/
WHATSAPP_MCP_PUBLIC_URL=
WHATSAPP_MCP_ALLOWED_HOSTS=
WHATSAPP_BRIDGE_HOST=127.0.0.1
WHATSAPP_BRIDGE_PORT=8080
WHATSAPP_SCRUB_PROMPT_INJECTION=true
WHATSAPP_AUDIT_LOG=true
WHATSAPP_AUDIT_LOG_PATH=$DATA_DIR/audit-mcp.log
WHATSAPP_VAULT_CRM_PATH=
ENV
  fi
  chmod 600 "$CONF_DIR"/*.env; chown root:root "$CONF_DIR"/*.env
  # Das Token für den Assistenten liegt in einem Ordner, der root gehört; im Home der Person schreibt root nichts
  install -d -m 755 "$STAND_DIR"
  sed -n 's/^WHATSAPP_MCP_TOKEN=//p' "$CONF_DIR/mcp.env" > "$STAND_DIR/token.tmp"
  chown "$1:$1" "$STAND_DIR/token.tmp"; chmod 600 "$STAND_DIR/token.tmp"; mv "$STAND_DIR/token.tmp" "$STAND_DIR/token"

  SCHRITT="Dienste"
  install -m 0644 "$KIT/whatsapp/whatsapp-bridge.service" "$KIT/whatsapp/whatsapp-mcp.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable -q whatsapp-bridge.service whatsapp-mcp.service
  systemctl restart whatsapp-bridge.service whatsapp-mcp.service
}

warten() {   # bis das Handy gescannt hat, höchstens 30 Minuten, danach Bridge einmal neu starten (Kontaktnamen)
  local i
  SCHRITT="Warten auf den Dienst"
  for i in $(seq 1 60); do
    [ -n "$(auth_state)" ] && break
    sleep 5
  done
  [ -n "$(auth_state)" ] || { stand "fehler: Die WhatsApp-Verbindung auf dem Server startet nicht. Schick /whatsapp_neu."; exit 1; }
  if [ "$(auth_state)" = paired ]; then
    stand fertig
    return 0
  fi
  stand bereit
  for i in $(seq 1 360); do
    if [ "$(auth_state)" = paired ]; then
      stand verknuepft
      sleep 120
      systemctl restart whatsapp-bridge
      for i in $(seq 1 24); do [ "$(auth_state)" = paired ] && break; sleep 5; done
      stand fertig
      return 0
    fi
    sleep 5
  done
  echo "   30 Minuten ohne Scan, bleibe bereit"
}

entfernen() {
  SCHRITT="Entfernen"
  systemctl disable -q --now whatsapp-mcp.service whatsapp-bridge.service 2>/dev/null || true
  rm -f /etc/systemd/system/whatsapp-mcp.service /etc/systemd/system/whatsapp-bridge.service
  systemctl daemon-reload
  rm -rf "$DATA_DIR" "$CONF_DIR" "$OPT_DIR" "$STAND_DIR/token"
  if id -u "$APP_USER" >/dev/null 2>&1; then userdel "$APP_USER" 2>/dev/null || true; fi
  stand aus
}

U=$(person) || { echo "Kein eigener Server einer Person (WACHE_SELBST in /etc/pearl-kit/box.env fehlt)" >&2; exit 1; }
WAS=${1:-}
if [ "$WAS" = auftrag ]; then
  DATEI=$(getent passwd "$U" | cut -d: -f6)/.local/state/assistent/whatsapp-auftrag
  WAS=$(runuser -u "$U" -- cat "$DATEI" 2>/dev/null | head -c 20 | tr -dc 'a-z' || true)
  runuser -u "$U" -- rm -f "$DATEI"
fi
case "$WAS" in
  an) [ "$(auth_state)" = paired ] || stand baut; bauen "$U"; warten ;;
  aus) entfernen ;;
  *) echo "Aufruf: whatsapp.sh an|aus|auftrag (Auftrag war: „$WAS“)" >&2; exit 2 ;;
esac
