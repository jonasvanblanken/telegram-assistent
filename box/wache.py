#!/usr/bin/env python3
"""Wache über alle Assistenten der Box (root, pearl-kit-wache.timer alle drei Minuten).

Fragt je Linux-User der Gruppe `assistenten` den Puls ab (`assistent.py puls --json`), prüft, ob seine
Dienste laufen, und schaut auf Platte und Speicher. Neue Befunde gehen per Telegram an den Admin
(WACHE_TG_TOKEN, WACHE_CHAT_ID aus /etc/pearl-kit/box.env), offene Befunde alle 12 Stunden noch einmal,
behobene einmal als „wieder in Ordnung“. Inhalte aus den Gesprächen sieht die Wache nie.

  python3 /opt/pearl-kit/box/wache.py            ein Durchgang
  python3 /opt/pearl-kit/box/wache.py --zeigen   Befunde nur anzeigen, nichts senden
"""
import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX_ENV = os.environ.get("WACHE_BOX_ENV", "/etc/pearl-kit/box.env")
ZUSTAND = os.environ.get("WACHE_ZUSTAND", "/var/lib/pearl-kit/wache.json")
ERINNERN = 12 * 3600
LEISE = {"kontingent"}          # bekommt die Person selbst gesagt, der Admin muss nichts tun


def box_env():
    out = {}
    try:
        for z in open(BOX_ENV, encoding="utf-8"):
            if "=" in z and not z.strip().startswith("#"):
                k, v = z.strip().split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def nutzer():
    try:
        return sorted(grp.getgrnam("assistenten").gr_mem)
    except KeyError:
        return []


def puls_von(u):
    """Befunde eines Users: Liste von {schluessel, stufe, text}."""
    pw = pwd.getpwnam(u)
    home = pw.pw_dir
    if not os.path.exists(os.path.join(home, "assistent", "config.toml")):
        return []
    out = []
    dienste = ["assistent.service"]
    try:
        if 'konto = ""' not in open(os.path.join(home, "assistent", "config.toml"), encoding="utf-8").read():
            dienste.append("google-mcp.service")
    except OSError:
        pass
    for d in dienste:
        r = subprocess.run(["systemctl", "--user", "-M", f"{u}@", "is-active", d], capture_output=True, text=True, timeout=30)
        if r.stdout.strip() != "active":
            out.append({"schluessel": f"dienst-{d}", "stufe": 2, "text": f"{d} läuft nicht ({r.stdout.strip() or 'unbekannt'})"})
    try:
        r = subprocess.run(["runuser", "-u", u, "--", "env", f"HOME={home}", sys.executable,
                            os.path.join(KIT, "assistent.py"), "puls", "--json"],
                           capture_output=True, text=True, timeout=60)
        if r.stdout.strip().startswith("["):
            out += json.loads(r.stdout)
        elif r.returncode:
            out.append({"schluessel": "puls", "stufe": 2, "text": f"Puls nicht lesbar: {(r.stderr or r.stdout).strip()[-200:]}"})
    except (subprocess.TimeoutExpired, ValueError) as e:
        out.append({"schluessel": "puls", "stufe": 2, "text": f"Puls nicht lesbar ({type(e).__name__})"})
    return out


def box_befunde():
    out = []
    du = shutil.disk_usage("/")
    if du.used / du.total > 0.9:
        out.append({"schluessel": "platte", "stufe": 2, "text": f"Platte zu {du.used / du.total:.0%} voll"})
    try:
        mem = dict(z.split(":", 1) for z in open("/proc/meminfo"))
        frei = int(mem["MemAvailable"].split()[0]) / int(mem["MemTotal"].split()[0])
        if frei < 0.1:
            out.append({"schluessel": "speicher", "stufe": 1, "text": f"nur noch {frei:.0%} Arbeitsspeicher frei"})
    except (OSError, KeyError, ValueError):
        pass
    return out


def entscheiden(offen, befunde, jetzt):
    """offen: {key: {"seit", "gemeldet", "text"}}; befunde: {key: text}. Rückgabe: (nachrichten, neu_offen)."""
    nachrichten, neu = [], {}
    for k, text in befunde.items():
        alt = offen.get(k)
        if not alt:
            nachrichten.append(f"⚠️ {k.split('/')[0]}: {text}")
            neu[k] = {"seit": jetzt, "gemeldet": jetzt, "text": text}
        elif jetzt - alt["gemeldet"] >= ERINNERN:
            stunden = int((jetzt - alt["seit"]) // 3600)
            nachrichten.append(f"⚠️ {k.split('/')[0]}: {text} (seit {stunden} h)")
            neu[k] = {**alt, "gemeldet": jetzt, "text": text}
        else:
            neu[k] = {**alt, "text": text}
    for k, alt in offen.items():
        if k not in befunde:
            nachrichten.append(f"✅ {k.split('/')[0]}: wieder in Ordnung ({alt['text']})")
    return nachrichten, neu


def eigener_bot(u):
    """Eigener Server der Person (WACHE_SELBST=<user>): Meldungen gehen über ihren Bot an sie selbst."""
    try:
        import tomllib
        home = pwd.getpwnam(u).pw_dir
        cfg = tomllib.load(open(os.path.join(home, "assistent", "config.toml"), "rb"))
        token = ""
        for z in open(os.path.join(home, ".config", "assistent", "secrets.env"), encoding="utf-8"):
            if z.startswith("TG_TOKEN="):
                token = z.split("=", 1)[1].strip()
        return token, str(cfg.get("person", {}).get("telegram_id") or "")
    except (KeyError, OSError, ValueError):
        return "", ""


def senden(env, text):
    token, chat = env.get("WACHE_TG_TOKEN"), env.get("WACHE_CHAT_ID")
    if (not token or not chat) and env.get("WACHE_SELBST"):
        token, chat = eigener_bot(env["WACHE_SELBST"])
        if chat in ("", "0"):
            return True   # noch niemand verbunden, niemand zum Benachrichtigen
        text = "Technik-Hinweis von deinem Server. Hilft nichts von selbst, starte ihn in der Hetzner-Konsole neu.\n\n" + text
    if not token or not chat:
        print("Wache: kein WACHE_TG_TOKEN/WACHE_CHAT_ID in box.env, nur Ausgabe:\n" + text)
        return True
    daten = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    api = os.environ.get("WACHE_TELEGRAM_API", "https://api.telegram.org")
    try:
        with urllib.request.urlopen(f"{api}/bot{token}/sendMessage", data=daten, timeout=20) as r:
            return r.status == 200
    except OSError as e:
        print(f"Wache: Telegram nicht erreicht ({type(e).__name__})", file=sys.stderr)
        return False


def main():
    jetzt = time.time()
    befunde = {}
    for u in nutzer():
        try:
            for b in puls_von(u):
                if b["schluessel"] not in LEISE:
                    befunde[f"{u}/{b['schluessel']}"] = b["text"]
        except Exception as e:
            befunde[f"{u}/wache"] = f"Prüfung gescheitert ({type(e).__name__}: {e})"
    for b in box_befunde():
        befunde[f"box/{b['schluessel']}"] = b["text"]
    if "--zeigen" in sys.argv:
        print("\n".join(f"{k}: {t}" for k, t in befunde.items()) or "alles in Ordnung")
        return
    try:
        st = json.load(open(ZUSTAND, encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    nachrichten, offen = entscheiden(st.get("offen", {}), befunde, jetzt)
    if nachrichten and not senden(box_env(), "\n".join(nachrichten)):
        return   # nicht gesendet: Zustand nicht fortschreiben, nächster Durchgang versucht es erneut
    os.makedirs(os.path.dirname(ZUSTAND), exist_ok=True)
    tmp = ZUSTAND + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"offen": offen, "zuletzt": jetzt}, f, ensure_ascii=False)
    os.replace(tmp, ZUSTAND)


if __name__ == "__main__":
    main()
