"""Die drei KI-Anbieter, die einem Assistenten als Gehirn dienen können. Jede Person nutzt ihr eigenes Abo.

  claude   Claude Code (`claude -p`), Anmeldung per `claude setup-token`, Langzeit-Token in secrets.env
  chatgpt  Codex (`codex exec`), Anmeldung per `codex login --device-auth`, Zugang in ~/.local/share/assistent/codex
  gemini   Gemini CLI (`gemini`), Anmeldung mit Google (NO_BROWSER), Zugang in ~/.local/share/assistent/gemini

Welcher Anbieter gilt, steht in config.toml unter [ki] anbieter. Leer heißt: die Person hat sich noch nicht
angemeldet. Die Einrichtungsseite (pforte.py) setzt den Wert nach der Anmeldung.
"""
import json
import os

import kit

ANBIETER = {
    "claude": {"name": "Claude", "abo": "Claude Pro oder Max",
               "modell": "sonnet", "klein": "sonnet", "effort": "high"},
    "chatgpt": {"name": "ChatGPT", "abo": "ChatGPT Plus, Pro oder Business",
                "modell": "standard", "klein": "standard", "effort": "medium"},
    "gemini": {"name": "Gemini", "abo": "privates Google-Konto, mehr mit Google AI Pro",
               "modell": "standard", "klein": "flash", "effort": ""},
}
REIHE = ("claude", "chatgpt", "gemini")
KI_BIN = os.environ.get("ASSISTENT_KI_BIN") or "/opt/pearl-kit-ki/bin"   # codex und gemini, fest installiert
CODEX_HOME = os.path.join(kit.DATEN, "codex")
GEMINI_HOME = os.path.join(kit.DATEN, "gemini")      # GEMINI_CLI_HOME, darin .gemini/
GOOGLE_GESPERRT = ("set_drive_file_permissions", "manage_drive_access", "manage_out_of_office",
                   "get_drive_shareable_link")         # wie die deny-Regeln in settings.vorlage.json


def name(anbieter):
    return ANBIETER.get(anbieter, {}).get("name", "KI")


def programm(anbieter):
    if anbieter == "claude":
        return os.environ.get("ASSISTENT_CLAUDE") or os.path.join(kit.HOME, ".local/bin/claude")
    datei = {"chatgpt": "codex", "gemini": "gemini"}[anbieter]
    return os.environ.get(f"ASSISTENT_{datei.upper()}") or os.path.join(KI_BIN, datei)


def standard(anbieter, feld):
    return ANBIETER.get(anbieter, ANBIETER["claude"])[feld]


def angemeldet(anbieter):
    """Liegt ein Zugang? Prüft nur, ob er da ist, nicht ob er noch gilt (das zeigt der erste Lauf)."""
    if anbieter == "claude":
        return bool(kit.secret("CLAUDE_CODE_OAUTH_TOKEN"))
    if anbieter == "chatgpt":
        return os.path.isfile(os.path.join(CODEX_HOME, "auth.json"))
    if anbieter == "gemini":
        return os.path.isfile(os.path.join(GEMINI_HOME, ".gemini", "oauth_creds.json"))
    return False


def toml_liste(werte):
    return "[" + ", ".join(json.dumps(w) for w in werte) + "]"


def codex_config(home_dir, google_port):
    """~/.local/share/assistent/codex/config.toml: Sandbox im Arbeitsordner, CLAUDE.md als Anweisung, Google."""
    out = ["# geschrieben von einrichten.py, nicht von Hand ändern",
           'approval_policy = "never"',
           'sandbox_mode = "workspace-write"',
           'project_doc_fallback_filenames = ["CLAUDE.md"]',
           "",
           "[sandbox_workspace_write]",
           "network_access = true",
           "",
           "[shell_environment_policy]",
           'inherit = "core"',
           "",
           f"[projects.{json.dumps(home_dir)}]",
           'trust_level = "trusted"']
    if google_port:
        out += ["", "[mcp_servers.google]",
                f'url = "http://127.0.0.1:{google_port}/mcp"',
                'bearer_token_env_var = "GOOGLE_MCP_TOKEN"',
                f"disabled_tools = {toml_liste(GOOGLE_GESPERRT)}"]
    return "\n".join(out) + "\n"


def gemini_settings(google_port):
    """~/.local/share/assistent/gemini/.gemini/settings.json: Anmeldung mit Google, CLAUDE.md als Anweisung."""
    s = {"security": {"auth": {"selectedType": "oauth-personal"}, "folderTrust": {"enabled": False}},
         "context": {"fileName": ["CLAUDE.md"]},
         "privacy": {"usageStatisticsEnabled": False},
         "mcpServers": {}}
    if google_port:
        s["mcpServers"]["google"] = {"httpUrl": f"http://127.0.0.1:{google_port}/mcp",
                                     "headers": {"Authorization": "Bearer $GOOGLE_MCP_TOKEN"},
                                     "excludeTools": list(GOOGLE_GESPERRT)}
    return s
