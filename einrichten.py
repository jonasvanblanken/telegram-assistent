#!/usr/bin/env python3
"""Legt die Instanz eines Assistenten an oder zieht ihre Technik nach (läuft als der Linux-User der Person).

  python3 /opt/pearl-kit/einrichten.py --person Anna --telegram-id 123456 --assistent Lotte --admin Kim \\
      [--google anna@gmail.com --port 8101] [--web-adresse https://box.example.de/anna --web-port 8201]
      [--anbieter claude|chatgpt|gemini] [--modell … --klein … --effort … --rechte auto]
  python3 /opt/pearl-kit/einrichten.py --admin Kim --web-adresse https://box.example.de/anna --web-port 8201 \
      [--port 8101]                                       Einladung: alles Weitere wählt die Person selbst
  python3 /opt/pearl-kit/einrichten.py --nachziehen      Technik neu aus config.toml

Ohne --anbieter wählt die Person ihre KI selbst auf der Einrichtungsseite (braucht --web-adresse). Mit
--web-adresse dürfen auch --person, --telegram-id und --assistent fehlen: Die Person bekommt einen Einladungslink,
der Bot merkt sich ihr Telegram-Konto beim ersten Start, Namen und Google wählt sie auf der Seite. home/ entsteht
dann erst, wenn die Einrichtung abgeschlossen ist (assistent.py ruft home_anlegen auf). Leere Werte bei
modell, klein und effort heißen: Standard des Anbieters (anbieter.py).

Schreibt ~/assistent/config.toml (nur beim ersten Mal oder mit --neu-config), settings.json, mcp.json,
google.env und die Einstellungen von Codex und Gemini immer neu, und kopiert vorlage/home/ nach ~/assistent/home/, ohne vorhandene Dateien anzufassen:
Was der Assistent schon gelernt hat, bleibt. Platzhalter {{…}} werden ersetzt, Abschnitte zwischen
<!-- google --> und <!-- /google --> bleiben nur mit Google-Konto stehen.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys

import anbieter
import kit

KIT = os.path.dirname(os.path.abspath(__file__))
VORLAGE = os.path.join(KIT, "vorlage")
GOOGLE_CLIENT = os.environ.get("ASSISTENT_GOOGLE_CLIENT") or "/etc/pearl-kit/google-client.json"
def block(name):
    return re.compile(rf"[ \t]*<!-- {name} -->[ \t]*\n?(.*?)[ \t]*<!-- /{name} -->[ \t]*\n?", re.S)


GOOGLE_BLOCK = block("google")
BETREUT_BLOCK = block("betreut")   # Box mit Betreuer (ADMIN gesetzt)
SELBST_BLOCK = block("selbst")     # eigener Server der Person, ohne Betreuer
WHATSAPP_BLOCK = block("whatsapp")   # Erweiterung WhatsApp eingeschaltet
PLATZHALTER = re.compile(r"\{\{([A-Z_]+)\}\}")


def toml_wert(v):
    if isinstance(v, int):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_text(a):
    teile = {
        "person": {"name": a.person or "", "telegram_id": a.telegram_id or 0, "zeitzone": a.zeitzone, "sprache": a.sprache},
        "assistent": {"name": a.assistent or ""},
        "admin": {"name": a.admin or ""},
        "ki": {"anbieter": a.anbieter, "modell": a.modell, "klein": a.klein, "effort": a.effort, "rechte": a.rechte},
        "google": {"konto": a.google or "", "port": a.port or 0, "dienste": "kalender,mail"},
        "web": {"adresse": (a.web_adresse or "").rstrip("/"), "port": a.web_port or 0},
        "kits": {"wahl": a.kits or ""},
    }
    out = ["# Instanz des Assistenten (pearl-kit). Nach Änderungen: einrichten.py --nachziehen,",
           "# dann systemctl --user restart assistent google-mcp", ""]
    for abschnitt, felder in teile.items():
        out.append(f"[{abschnitt}]")
        out += [f"{k} = {toml_wert(v)}" for k, v in felder.items()]
        out.append("")
    return "\n".join(out)


def werte(cfg):
    return {"PERSON": cfg["person"]["name"], "ASSISTENT": cfg["assistent"]["name"], "ADMIN": cfg["admin"]["name"],
            "ZEITZONE": cfg["person"]["zeitzone"], "GOOGLE_KONTO": cfg["google"]["konto"],
            "GOOGLE_PORT": str(cfg["google"]["port"]), "HOME": kit.HOME_DIR, "INSTANZ": kit.INSTANZ, "KIT": KIT,
            "HEUTE": datetime.date.today().isoformat(),
            "WHATSAPP": "an" if cfg["whatsapp"]["status"] == "an" else ""}


def rendern(text, w, google):
    text = GOOGLE_BLOCK.sub(lambda m: m.group(1) if google else "", text)
    betreut = bool(w.get("ADMIN"))
    text = BETREUT_BLOCK.sub(lambda m: m.group(1) if betreut else "", text)
    text = SELBST_BLOCK.sub(lambda m: "" if betreut else m.group(1), text)
    text = WHATSAPP_BLOCK.sub(lambda m: m.group(1) if w.get("WHATSAPP") else "", text)

    def ersetze(m):
        if m.group(1) not in w:
            raise KeyError(f"Unbekannter Platzhalter {{{{{m.group(1)}}}}}")
        return w[m.group(1)]
    return PLATZHALTER.sub(ersetze, text)


def schreibe(pfad, text, modus=0o600):
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, modus)
    os.replace(tmp, pfad)


STAND = os.path.join(kit.INSTANZ, "vorlage-stand.json")   # je Datei: Hash der Vorlage und der geschriebenen Fassung
KIT_DATEIEN = ("CLAUDE.md",)   # gehören dem Kit, der Assistent ändert sie nicht


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stand_lesen():
    try:
        return json.load(open(STAND, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def stand_merken(eintraege):
    st = stand_lesen()
    st.update(eintraege)
    schreibe(STAND, json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def vorlagen():
    """(relativer Pfad, Text der Vorlage) für alles in vorlage/home/."""
    quelle = os.path.join(VORLAGE, "home")
    for wurzel, _, dateien in os.walk(quelle):
        for d in sorted(dateien):
            pfad = os.path.join(wurzel, d)
            yield os.path.normpath(os.path.relpath(pfad, quelle)), open(pfad, encoding="utf-8").read()


def home_nachziehen(w, google):
    """Neue Fassung des Kits: Dateien aus der Vorlage, die seit dem Anlegen niemand geändert hat, bekommen die neue
    Vorlage. Neue Vorlagen werden angelegt. Was die Person oder der Assistent geändert hat, bleibt. Ohne Eintrag im
    Stand (ältere Instanzen) wird nur CLAUDE.md erneuert, die übrigen Dateien gelten ab jetzt als Ausgangsstand.
    Rückgabe: geschriebene Pfade."""
    st, merken, out = stand_lesen(), {}, []
    for rel, vorlage in vorlagen():
        ziel = os.path.join(kit.HOME_DIR, rel)
        neu = rendern(vorlage, w, google)
        e = st.get(rel)
        if not os.path.isfile(ziel):
            schreiben = True
        else:
            jetzt = open(ziel, encoding="utf-8").read()
            if e:
                schreiben = e["vorlage"] != _hash(vorlage) and e["datei"] == _hash(jetzt)
            else:
                schreiben = rel in KIT_DATEIEN and jetzt != neu
            if not schreiben:
                if not e:
                    merken[rel] = {"vorlage": _hash(vorlage), "datei": _hash(jetzt)}
                continue
        schreibe(ziel, neu, 0o600)
        merken[rel] = {"vorlage": _hash(vorlage), "datei": _hash(neu)}
        out.append(rel)
    if merken:
        stand_merken(merken)
    for d in ("eingang", "ausgang", "leben/ablage", "gedaechtnis/_archiv"):
        os.makedirs(os.path.join(kit.HOME_DIR, d), exist_ok=True)
    return out


def home_anlegen(w, google):
    """vorlage/home/ nach home/ kopieren, vorhandene Dateien bleiben unberührt. Rückgabe: neu angelegte Pfade."""
    neu, merken = [], {}
    quelle = os.path.join(VORLAGE, "home")
    for wurzel, _, dateien in os.walk(quelle):
        rel = os.path.relpath(wurzel, quelle)
        ziel_ordner = os.path.normpath(os.path.join(kit.HOME_DIR, rel))
        os.makedirs(ziel_ordner, exist_ok=True)
        for d in dateien:
            ziel = os.path.join(ziel_ordner, d)
            if os.path.exists(ziel):
                continue
            text = open(os.path.join(wurzel, d), encoding="utf-8").read()
            fertig = rendern(text, w, google)
            schreibe(ziel, fertig, 0o600)
            rel_ziel = os.path.relpath(ziel, kit.HOME_DIR)
            neu.append(rel_ziel)
            merken[rel_ziel] = {"vorlage": _hash(text), "datei": _hash(fertig)}
    if merken:
        stand_merken(merken)
    for d in ("eingang", "ausgang", "leben/ablage", "gedaechtnis/_archiv"):
        os.makedirs(os.path.join(kit.HOME_DIR, d), exist_ok=True)
    return neu


def google_rechte(cfg):
    """--permissions für workspace-mcp aus [google] dienste."""
    dienste = [d for d in cfg["google"]["dienste"].split(",") if d] or list(kit.GOOGLE_DIENSTE)
    return " ".join(kit.GOOGLE_DIENSTE[d] for d in dienste)


def home_erneuern(w_alt, google_alt, w_neu, google_neu):
    """Nach neuem Namen oder Google: Dateien aus der Vorlage neu schreiben, die noch genau so sind, wie sie
    angelegt wurden. Was die Person oder der Assistent schon geändert hat, bleibt. Rückgabe: erneuerte Pfade."""
    out = []
    quelle = os.path.join(VORLAGE, "home")
    for wurzel, _, dateien in os.walk(quelle):
        for d in dateien:
            ziel = os.path.normpath(os.path.join(kit.HOME_DIR, os.path.relpath(wurzel, quelle), d))
            if not os.path.isfile(ziel):
                continue
            text = open(os.path.join(wurzel, d), encoding="utf-8").read()
            alt, neu = rendern(text, w_alt, google_alt), rendern(text, w_neu, google_neu)
            if alt != neu and open(ziel, encoding="utf-8").read() == alt:
                schreibe(ziel, neu, 0o600)
                rel = os.path.relpath(ziel, kit.HOME_DIR)
                out.append(rel)
                stand_merken({rel: {"vorlage": _hash(text), "datei": _hash(neu)}})
    return out


def home_umschalten(w_alt, w_neu, google):
    """Erweiterung an oder aus: unveränderte Vorlagen neu rendern, die Dateien des Kits (CLAUDE.md) immer."""
    out = home_erneuern(w_alt, google, w_neu, google)
    for rel in KIT_DATEIEN:
        ziel = os.path.join(kit.HOME_DIR, rel)
        if rel in out or not os.path.isfile(ziel):
            continue
        text = open(os.path.join(VORLAGE, "home", rel), encoding="utf-8").read()
        neu = rendern(text, w_neu, google)
        if open(ziel, encoding="utf-8").read() != neu:
            schreibe(ziel, neu, 0o600)
            stand_merken({rel: {"vorlage": _hash(text), "datei": _hash(neu)}})
            out.append(rel)
    return out


def technik(cfg):
    """settings.json, mcp.json, google.env, Codex- und Gemini-Einstellungen und .gitignore aus config.toml."""
    w = werte(cfg)
    google = bool(cfg["google"]["konto"])
    settings = json.loads(rendern(open(os.path.join(VORLAGE, "settings.vorlage.json"), encoding="utf-8").read(), w, google))
    if not google:
        settings["permissions"]["allow"] = [r for r in settings["permissions"]["allow"] if not r.startswith("mcp__google")]
        settings["permissions"]["deny"] = [r for r in settings["permissions"]["deny"] if not r.startswith("mcp__google")]
    schreibe(os.path.join(kit.INSTANZ, "settings.json"), json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    mcp = json.loads(rendern(open(os.path.join(VORLAGE, "mcp.vorlage.json"), encoding="utf-8").read(), w, google)) \
        if google else {"mcpServers": {}}
    schreibe(os.path.join(kit.INSTANZ, "mcp.json"), json.dumps(mcp, ensure_ascii=False, indent=2) + "\n")
    gport = cfg["google"]["port"] if google else 0
    schreibe(os.path.join(anbieter.CODEX_HOME, "config.toml"), anbieter.codex_config(kit.HOME_DIR, gport))
    schreibe(os.path.join(anbieter.GEMINI_HOME, ".gemini", "settings.json"),
             json.dumps(anbieter.gemini_settings(gport), ensure_ascii=False, indent=2) + "\n")
    if google:
        port = cfg["google"]["port"]
        daten = os.path.join(kit.DATEN, "google")
        web = cfg["web"]["adresse"]
        # Mit Webadresse leitet Google auf die Einrichtungsseite der Box um, die Seite lädt und meldet „verbunden“.
        # Ohne sie bleibt der alte Weg: localhost lädt nicht, die Person schickt die Adresse an den Bot.
        rueckruf = f"{web}/oauth2callback" if web else f"http://localhost:{port}/oauth2callback"
        schreibe(os.path.join(kit.INSTANZ, "google.env"), "\n".join([
            "# geschrieben von einrichten.py, nicht von Hand ändern",
            f"USER_GOOGLE_EMAIL={cfg['google']['konto']}",
            f"WORKSPACE_MCP_PORT={port}",
            "WORKSPACE_MCP_BASE_URI=http://localhost",
            f"GOOGLE_OAUTH_REDIRECT_URI={rueckruf}",
            f"WORKSPACE_MCP_CREDENTIALS_DIR={daten}/zugang",
            f"WORKSPACE_ATTACHMENT_DIR={kit.HOME_DIR}/eingang/google",
            f"GOOGLE_CLIENT_SECRET_PATH={GOOGLE_CLIENT}",
            f'GOOGLE_RECHTE="{google_rechte(cfg)}"',
            "OAUTHLIB_INSECURE_TRANSPORT=1", ""]))
    schreibe(os.path.join(kit.INSTANZ, ".gitignore"), "home/eingang/\nhome/ausgang/\nhome/whatsapp/.sweep/\ngoogle.env\n*.tmp\n",
             0o644)


def main():
    a = argparse.ArgumentParser(description="Instanz eines Assistenten anlegen oder nachziehen")
    a.add_argument("--person")
    a.add_argument("--telegram-id", type=int)
    a.add_argument("--assistent")
    a.add_argument("--admin")
    a.add_argument("--zeitzone", default="Europe/Berlin")
    a.add_argument("--sprache", default="de")
    a.add_argument("--google", default="")
    a.add_argument("--port", type=int, default=0)
    a.add_argument("--web-adresse", default="", help="https://<box-domain>/<kurzname>, für die Einrichtungsseite")
    a.add_argument("--web-port", type=int, default=0)
    a.add_argument("--anbieter", default="", choices=["", *anbieter.REIHE])
    a.add_argument("--modell", default="")
    a.add_argument("--klein", default="")
    a.add_argument("--effort", default="")
    a.add_argument("--rechte", default="auto")
    a.add_argument("--nachziehen", action="store_true", help="nur Technik aus der vorhandenen config.toml neu schreiben")
    a.add_argument("--whatsapp", choices=["an", "aus"], help="Erweiterung WhatsApp schalten (ruft werkzeuge/whatsapp.py)")
    a.add_argument("--kits", default="", help=f'vorab gewählte Kits, Komma-Liste aus {", ".join(kit.KITS)} oder "{kit.KITS_OHNE}"')
    a.add_argument("--neu-config", action="store_true", help="vorhandene config.toml überschreiben")
    x = a.parse_args()
    if kit.kits_gewaehlt(x.kits) is False:   # die Einrichtung soll daran nicht scheitern: dann alles anbieten
        print(f"--kits {x.kits!r} unbekannt, erlaubt sind {', '.join(kit.KITS)} oder {kit.KITS_OHNE}. Biete alle Kits an.")
        x.kits = ""

    os.makedirs(kit.INSTANZ, mode=0o700, exist_ok=True)
    if x.whatsapp:
        try:
            alt = kit.lies_config()
        except kit.ConfigFehler as e:
            sys.exit(str(e))
        kit.setze_config("whatsapp", "status", x.whatsapp)
        cfg = kit.lies_config()
        technik(cfg)
        google = bool(cfg["google"]["konto"])
        neu = home_umschalten(werte(alt), werte(cfg), google) \
            if os.path.isfile(os.path.join(kit.HOME_DIR, "CLAUDE.md")) else []
        print(f"WhatsApp {x.whatsapp}" + (f", erneuert: {', '.join(sorted(neu))}" if neu else ""))
        return
    if not x.nachziehen and (x.neu_config or not os.path.exists(kit.CONFIG)):
        fehlt = [n for n in (() if x.web_adresse else ("person", "telegram_id", "assistent", "admin"))
                 if not getattr(x, n)]
        if fehlt:
            sys.exit("Für eine neue Instanz fehlt: " + ", ".join("--" + f.replace("_", "-") for f in fehlt))
        if x.google and not x.port:
            sys.exit("Mit --google braucht es --port (8101 aufwärts, je Person einer)")
        schreibe(kit.CONFIG, config_text(x))
    try:
        cfg = kit.lies_config()
    except kit.ConfigFehler as e:
        sys.exit(str(e))
    technik(cfg)
    einladung = not cfg["person"]["name"]   # home/ entsteht erst nach der Einrichtung durch die Person
    if x.nachziehen:   # neue Fassung des Kits: unveränderte Vorlagen in home/ mitziehen
        neu = home_nachziehen(werte(cfg), bool(cfg["google"]["konto"])) \
            if os.path.isfile(os.path.join(kit.HOME_DIR, "CLAUDE.md")) else []
    else:
        neu = [] if einladung else home_anlegen(werte(cfg), bool(cfg["google"]["konto"]))
    for d in (kit.STATE_DIR, os.path.dirname(kit.SECRETS)):
        os.makedirs(d, mode=0o700, exist_ok=True)
    if not os.path.exists(kit.SECRETS):
        schreibe(kit.SECRETS, "# Schlüssel des Assistenten, Rechte 600. Siehe README im Kit.\n")
    ki = cfg["ki"]["anbieter"]
    print(f"Instanz {kit.INSTANZ}: " + (f"{cfg['assistent']['name']} für {cfg['person']['name']}" if not einladung
                                         else "Einladung, Namen wählt die Person")
          + (f", Google {cfg['google']['konto']} auf Port {cfg['google']['port']}" if cfg["google"]["konto"] else ", ohne Google")
          + (f", KI {anbieter.name(ki)}" if ki else ", Namen, KI und Google richtet die Person über den Einladungslink ein")
          + (f", Einrichtungsseite {cfg['web']['adresse']}" if cfg["web"]["adresse"] else ""))
    if neu:
        print(f"{'Aus der Vorlage erneuert' if x.nachziehen else 'Neu angelegt'}: {len(neu)} Dateien in home/")


if __name__ == "__main__":
    main()
