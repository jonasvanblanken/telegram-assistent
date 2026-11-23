#!/bin/bash
# Google-Dienst einer Instanz: workspace-mcp im HTTP-Modus, nur auf 127.0.0.1, mit Bearer-Prüfung.
# Gestartet von google-mcp.service (User-Unit). Einstellungen aus ~/assistent/google.env (schreibt einrichten.py):
# USER_GOOGLE_EMAIL, WORKSPACE_MCP_PORT, GOOGLE_OAUTH_REDIRECT_URI, WORKSPACE_MCP_CREDENTIALS_DIR,
# WORKSPACE_ATTACHMENT_DIR, GOOGLE_CLIENT_SECRET_PATH, GOOGLE_RECHTE (aus den Schaltern Kalender und Mail).
# Senden bleibt aus: gmail:drafts erlaubt nur Entwürfe. tasks:manage legt an und hakt ab.
set -euo pipefail
KIT="$(cd "$(dirname "$0")/.." && pwd)"
INSTANZ="${ASSISTENT_DIR:-$HOME/assistent}"
set -a
. "$INSTANZ/google.env"
set +a
unset WORKSPACE_MCP_HOST
mkdir -p "$WORKSPACE_MCP_CREDENTIALS_DIR" "$WORKSPACE_ATTACHMENT_DIR"
# Abgelegte Originale darf der Dienst nach Drive hochladen (create_drive_file mit file://), sonst nichts aus home/
export ALLOWED_FILE_DIRS="$INSTANZ/home/leben/ablage"
mkdir -p "$ALLOWED_FILE_DIRS"
chmod 700 "$WORKSPACE_MCP_CREDENTIALS_DIR"
exec "$KIT/.venv-google/bin/python3" "$KIT/google/google-mcp-auth.py" \
  --transport streamable-http --single-user \
  --permissions ${GOOGLE_RECHTE:-gmail:drafts calendar:full drive:full tasks:manage}   # absichtlich ohne Anführungszeichen
