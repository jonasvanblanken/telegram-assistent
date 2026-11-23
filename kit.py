"""Gemeinsame Basis des Kits: wo die Instanz liegt, Config lesen, Schlüssel lesen.

Instanz: ASSISTENT_DIR oder ~/assistent (config.toml, settings.json, mcp.json, home/).
Zustand: ~/.local/state/assistent (oder ASSISTENT_STATE). Anmeldedaten von Google, ChatGPT und Gemini:
~/.local/share/assistent (oder ASSISTENT_DATEN). Schlüssel: ~/.config/assistent/secrets.env
(oder ASSISTENT_SECRETS), Zeilen KEY=wert, Rechte 600.
"""
import os
import re
import tomllib

HOME = os.path.expanduser("~")
INSTANZ = os.environ.get("ASSISTENT_DIR") or os.path.join(HOME, "assistent")
HOME_DIR = os.path.join(INSTANZ, "home")
CONFIG = os.path.join(INSTANZ, "config.toml")
STATE_DIR = os.environ.get("ASSISTENT_STATE") or os.path.join(HOME, ".local/state/assistent")
VERLAUF = os.path.join(STATE_DIR, "verlauf")
DATEN = os.environ.get("ASSISTENT_DATEN") or os.path.join(HOME, ".local/share/assistent")
SECRETS = os.environ.get("ASSISTENT_SECRETS") or os.path.join(HOME, ".config/assistent/secrets.env")

PFLICHT = set()   # mit Einrichtungsseite darf [admin] name leer sein: eigener Server der Person, ohne Betreuer
PFLICHT_OHNE_WEB = {("admin", "name"), ("person", "name"), ("person", "telegram_id"), ("assistent", "name")}   # mit Einrichtungsseite kommen
# Name, Telegram-ID und Name des Assistenten erst bei der Einrichtung durch die Person (Einladungslink)
STANDARD = {"person": {"zeitzone": "Europe/Berlin", "sprache": "de"},
            "assistent": {}, "admin": {},
            "ki": {"anbieter": "", "modell": "", "klein": "", "effort": "", "rechte": "auto"},
            "google": {"konto": "", "port": 0, "dienste": "kalender,mail"},
            "web": {"adresse": "", "port": 0},
            "whatsapp": {"status": "aus"}}   # an: Erweiterung eingerichtet (nur eigener Server, /whatsapp im Chat)
ANBIETER = ("claude", "chatgpt", "gemini")
RECHTE = {"auto", "acceptEdits", "default", "dontAsk", "bypassPermissions"}   # Claude: --permission-mode
RECHTE_ANDERE = {"auto", "voll"}
GOOGLE_DIENSTE = {   # Schalter auf der Einrichtungsseite -> --permissions von workspace-mcp (Senden bleibt immer aus)
    "kalender": "calendar:full tasks:manage",
    "mail": "gmail:drafts drive:full",
}   # ChatGPT und Gemini: auto = Sandbox im Arbeitsordner, voll = ohne


class ConfigFehler(Exception):
    pass


def lies_config(pfad=CONFIG):
    """config.toml lesen, Standardwerte ergänzen und prüfen. Wirft ConfigFehler mit klarer Meldung."""
    try:
        with open(pfad, "rb") as f:
            roh = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigFehler(f"{pfad} fehlt. Instanz mit `python3 einrichten.py` anlegen.")
    except tomllib.TOMLDecodeError as e:
        raise ConfigFehler(f"{pfad} ist kein gültiges TOML: {e}")
    cfg = {k: {**v, **(roh.get(k) or {})} for k, v in STANDARD.items()}
    pflicht = PFLICHT if cfg["web"]["adresse"] else PFLICHT | PFLICHT_OHNE_WEB
    fehlt = [f"[{a}] {b}" for a, b in sorted(pflicht) if not str(cfg[a].get(b) or "").strip()]
    if fehlt:
        raise ConfigFehler(f"In {pfad} fehlt: {', '.join(fehlt)}")
    try:
        cfg["person"]["telegram_id"] = int(cfg["person"]["telegram_id"] or 0)
        cfg["google"]["port"] = int(cfg["google"].get("port") or 0)
        cfg["web"]["port"] = int(cfg["web"].get("port") or 0)
    except (TypeError, ValueError):
        raise ConfigFehler("telegram_id, google.port und web.port müssen Zahlen sein")
    if cfg["google"]["konto"] and not 1024 < cfg["google"]["port"] < 65536:
        raise ConfigFehler("Google-Konto gesetzt, aber kein gültiger [google] port")
    if set(filter(None, cfg["google"]["dienste"].split(","))) - set(GOOGLE_DIENSTE):
        raise ConfigFehler(f"[google] dienste: erlaubt sind {', '.join(GOOGLE_DIENSTE)}, mit Komma getrennt")
    ki = cfg["ki"]
    if ki["anbieter"] and ki["anbieter"] not in ANBIETER:
        raise ConfigFehler(f"[ki] anbieter muss leer oder eins von {', '.join(ANBIETER)} sein")
    erlaubt = RECHTE if ki["anbieter"] in ("", "claude") else RECHTE_ANDERE
    if ki["rechte"] not in erlaubt | {"auto"}:
        raise ConfigFehler(f"[ki] rechte muss eins von {', '.join(sorted(erlaubt))} sein")
    if cfg["whatsapp"]["status"] not in ("an", "aus"):
        raise ConfigFehler('[whatsapp] status muss "an" oder "aus" sein')
    if cfg["web"]["adresse"]:
        if not re.fullmatch(r"https://[^/\s]+(/[a-z0-9-]+)?", cfg["web"]["adresse"]):
            raise ConfigFehler("[web] adresse muss so aussehen: https://box.example.de/kurzname")
        if not 1024 < cfg["web"]["port"] < 65536:
            raise ConfigFehler("[web] adresse gesetzt, aber kein gültiger [web] port")
    return cfg


def setze_config(abschnitt, schluessel, wert, pfad=CONFIG):
    """Einen Wert in config.toml ändern, ohne den Rest anzufassen (z. B. [ki] anbieter nach der Anmeldung)."""
    text = open(pfad, encoding="utf-8").read()
    zeile = f'{schluessel} = "{wert}"' if isinstance(wert, str) else f"{schluessel} = {wert}"
    m = re.search(rf"^\[{re.escape(abschnitt)}\]\s*$", text, re.M)
    if not m:
        text = text.rstrip("\n") + f"\n\n[{abschnitt}]\n{zeile}\n"
    else:
        ende = re.search(r"^\[", text[m.end():], re.M)
        block_ende = m.end() + (ende.start() if ende else len(text) - m.end())
        block = text[m.end():block_ende]
        if re.search(rf"^{re.escape(schluessel)}\s*=.*$", block, re.M):
            block = re.sub(rf"^{re.escape(schluessel)}\s*=.*$", lambda _: zeile, block, count=1, flags=re.M)
        else:
            block = "\n" + zeile + block
        text = text[:m.end()] + block + text[block_ende:]
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, pfad)
    global _cfg
    _cfg = None


_cfg = None


def config():
    global _cfg
    if _cfg is None:
        _cfg = lies_config()
    return _cfg


def lies_secrets(pfad=SECRETS):
    out = {}
    try:
        with open(pfad, encoding="utf-8") as f:
            for zeile in f:
                zeile = zeile.strip()
                if not zeile or zeile.startswith("#") or "=" not in zeile:
                    continue
                k, v = zeile.split("=", 1)
                k = k.strip().removeprefix("export ").strip()
                out[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def setze_secret(name, wert, pfad=SECRETS):
    """Zeile in secrets.env ersetzen oder anhängen, Rechte 600."""
    zeilen = []
    try:
        zeilen = [z for z in open(pfad, encoding="utf-8").read().splitlines() if not z.startswith(f"{name}=")]
    except OSError:
        os.makedirs(os.path.dirname(pfad), mode=0o700, exist_ok=True)
    zeilen.append(f"{name}={wert}")
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(zeilen) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, pfad)


def secret(name):
    """Schlüssel aus secrets.env, bei jedem Aufruf frisch gelesen (Token-Tausch ohne Neustart)."""
    return lies_secrets().get(name, "")
