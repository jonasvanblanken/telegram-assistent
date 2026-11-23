#!/usr/bin/env python3
"""Persönlicher Assistent in Telegram (pearl-kit). Das Gehirn ist das KI-Abo der Person: Claude (Claude Code),
ChatGPT (Codex) oder Gemini (Gemini CLI), siehe anbieter.py und [ki] in config.toml.

Nimmt nur Nachrichten vom Telegram-Konto der Person an (config.toml, [person] telegram_id), gibt sie an die KI
im Ordner home/ der Instanz und schickt die Antwort zurück. Ein Gespräch läuft, bis der Tag
wechselt (03:50/04:00) oder es größer als GESPRAECH_MAX Tokens wird. Vor jedem Wechsel sichert der Assistent,
was bleiben soll (Auftrag in jobs/tagesabschluss.md: Gedächtnis, Akten, profil/laufend.md, Logbuch).
Scheitert das, bleibt das Gespräch vorgemerkt, der Dienst versucht es weiter und meldet sich. Alles, was im
Chat gesagt wird, steht ungekürzt im Logbuch home/leben/logbuch/<tag>.md.

Zeitgesteuerte Läufe liegen als Dateien in home/jobs/ (Kopf: zeit, tage, monatstag, datum, alle, fenster,
modell, effort, gespraech, melden, hoechstens, ausweichen, verpasst, text, vorher, pruefen) und laufen im
selben Dienst wie der Chat, damit nie zwei Läufe gleichzeitig dasselbe Gespräch fortsetzen. Arbeit, die
noch nicht erledigt ist, liegt in ~/.local/state/assistent/schlange/ und übersteht einen Neustart. Ist das
Abo-Kontingent eines Modells aufgebraucht, weicht der Dienst aus oder holt Läufe danach nach.

  assistent.py                        Dienst (assistent.service), Long Polling bei Telegram
  assistent.py frag "<text>"          eine Nachricht wie aus Telegram, Antwort hier, eigenes Gespräch (Test)
  assistent.py job <name> [--senden]  einen Lauf sofort ausführen, Ausgabe hier, mit --senden auch in Telegram
  assistent.py neu                    die nächste Nachricht beginnt ein neues Gespräch (das alte wird gesichert)
  assistent.py status                 Lagebild
  assistent.py puls [--json]          Lebenszeichen für die Wache, Exit 1 bei Problemen
  assistent.py pruefen                Selbsttest: Config, Schlüssel, Telegram, KI, Google, Einrichtungsseite
  assistent.py einladung              Einladungslink für die Person (t.me/<bot>?start=<code>), solange sie fehlt

Instanz: ~/assistent (oder ASSISTENT_DIR) mit config.toml, settings.json, mcp.json und home/, angelegt von
einrichten.py. Schlüssel in ~/.config/assistent/secrets.env: TG_TOKEN, CLAUDE_CODE_OAUTH_TOKEN (Claude, aus
`claude setup-token`), GOOGLE_MCP_TOKEN, optional OPENAI_API_KEY (Sprachnachrichten). Mit [web] adresse läuft
im Dienst die Einrichtungsseite (pforte.py), die der Bot als Web App in Telegram öffnet: Namen, KI-Anmeldung,
Kalender und Mail. Ohne [person] telegram_id wartet der Dienst auf den Einladungslink (/start <code>), home/ entsteht
erst, wenn die Person die Einrichtung abschließt.
"""
import base64
import datetime
import glob
import hmac
import json
import os
import queue
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import requests

import anbieter
import einrichten
import kit
import pforte

HERE = os.path.dirname(os.path.abspath(__file__))   # Code des Kits, für alle Instanzen gleich
CFG = kit.config()
INSTANZ = kit.INSTANZ                               # ~/assistent: config.toml, settings.json, mcp.json, home/
HOME_DIR = kit.HOME_DIR                             # Arbeitsordner der KI
JOBS_DIR = os.path.join(HOME_DIR, "jobs")
EINGANG = os.path.join(HOME_DIR, "eingang")         # Fotos, Dateien, Sprachnachrichten aus Telegram
LOGBUCH = os.path.join(HOME_DIR, "leben", "logbuch")  # jeder Tag ungekürzt und dauerhaft, in der Sicherung
WERKZEUGE = os.path.join(HERE, "werkzeuge")         # vorher:/pruefen: der Läufe, nur Skripte von hier
SETTINGS = os.path.join(INSTANZ, "settings.json")
MCP = os.path.join(INSTANZ, "mcp.json")
HOME = os.path.expanduser("~")
TELEGRAM_API = os.environ.get("ASSISTENT_TELEGRAM_API", "https://api.telegram.org")
STATE_DIR = kit.STATE_DIR
STATE = os.path.join(STATE_DIR, "state.json")
VERLAUF = kit.VERLAUF                               # Mitschrift je Gesprächstag (gekürzt), liest der Herzschlag
SCHLANGE = os.path.join(STATE_DIR, "schlange")      # unerledigte Arbeit, übersteht Neustarts

PERSON = CFG["person"]["name"]                      # so heißt die Person im Chat und im Logbuch
NAME = CFG["assistent"]["name"]                     # so heißt der Assistent
ADMIN = CFG["admin"]["name"]                        # wer die Box betreut
PERSON_ID = int(CFG["person"]["telegram_id"] or 0)  # nur dieses Telegram-Konto wird angenommen, 0: Einladung offen
TZ = ZoneInfo(CFG["person"].get("zeitzone") or "Europe/Berlin")
SPRACHE = CFG["person"].get("sprache") or "de"
GOOGLE_KONTO = CFG["google"].get("konto") or ""
GOOGLE_PORT = int(CFG["google"].get("port") or 0)
WEB = (CFG["web"].get("adresse") or "").rstrip("/")   # Einrichtungsseite, z. B. https://box.example.de/anna
WEB_PORT = int(CFG["web"].get("port") or 0)
ANMELDE_GUELTIG = 3600           # so lange gilt ein Link zur Einrichtungsseite
TAGESWECHSEL = 4                 # neues Gespräch ab 04:00
NACHHOLEN = 3 * 3600             # verpasste Läufe höchstens drei Stunden später nachholen
NACHHOLEN_LIMIT = 12 * 3600      # am Abo-Limit ausgefallene Läufe höchstens zwölf Stunden später nachholen
IM_GESPRAECH = 20 * 60           # wiederkehrende Läufe pausieren, solange die Person gerade schreibt
VERLAUF_TAGE = 14                # Mitschriften so lange aufheben (das Logbuch bleibt)
BUENDELN = float(os.environ.get("ASSISTENT_BUENDELN", "2.0"))   # Nachrichten kurz hintereinander gehen zusammen an die KI
LAUF_MAX = 20 * 60               # ein KI-Lauf darf höchstens 20 Minuten dauern
ZWISCHENSTAND = 90               # nach so vielen Sekunden ohne Antwort kommt ein Satz, woran der Assistent ist
GESPRAECH_MAX = 150_000          # ab so vielen Tokens im Kontext sichern und ein neues Gespräch beginnen
RECHTE = CFG["ki"].get("rechte") or "auto"   # Claude: --permission-mode, ChatGPT: auto = Sandbox, voll = ohne
KI_ANBIETER = KI_NAME = CHAT_MODELL = KLEIN_MODELL = CHAT_EFFORT = ""   # setzt ki_setzen()
MODELLE = ()                     # Ausweichen am Abo-Limit nur zwischen Chat- und Klein-Modell
KIND_ENV = ("GOOGLE_MCP_TOKEN",)                         # nur diese Schlüssel sieht die KI (für den Google-Dienst)
GEHEIM = re.compile(r"(\.config/|\.ssh/|\.credentials|\.env\b|\.bashrc|\.profile|/etc/pearl-kit|config\.toml)")
LIMIT = re.compile(r"hit your (?:\w+ )?limit|exhausted your (?:\w+ )?(?:quota|capacity)|quota exceeded|"
                   r"RESOURCE_EXHAUSTED|usage limit reached|rate limit exceeded", re.I)
GESPERRT = "GESPERRT: KI-Kontingent aufgebraucht"
WOCHENTAG = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONATE = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
LAUF_NAMEN = {"morgen": "Morgen-Check-in", "abend": "Abend-Check-in", "wochenrueckblick": "Wochenrückblick",
              "herzschlag": "Herzschlag", "tagesabschluss": "Tagesabschluss",
              "vorstellen": "Vorstellen", "whatsapp-nacht": "WhatsApp-Nachtlauf",
              "whatsapp-kreis": "Kreis festlegen", "whatsapp-morgen": "WhatsApp-Vorschläge"}
HILFE = ("Schreib mir einfach, auch Sprachnachrichten, Fotos und Dateien.\n"
         "/status  was gerade läuft\n"
         + ("/google  Kalender und Mail verbinden\n" if GOOGLE_KONTO or (WEB and GOOGLE_PORT) else "")
         + ("/ki  zwischen deinen KIs wechseln oder eine weitere verbinden\n" if WEB else "")
         + "/neu  neues Gespräch. Brauchst du nicht, ich wechsle selbst und halte vorher fest, was bleiben soll")
GOOGLE_RUECKRUF = re.compile(r"https?://(?:localhost|127\.0\.0\.1):(\d+)/oauth2callback\?(\S+)")

_lock = threading.RLock()
_arbeit = queue.Queue()
_eingereiht = set()              # Laeufe, die in der Schlange stehen oder gerade laufen
_kaputt = set()                  # Dateien in jobs/ mit unlesbarem Kopf
_puls_zuletzt = 0.0


def ki_setzen(name):
    """Anbieter und Modelle aus [ki] übernehmen. Leere Felder: Standard des Anbieters."""
    global KI_ANBIETER, KI_NAME, CHAT_MODELL, KLEIN_MODELL, CHAT_EFFORT, MODELLE
    c, basis = CFG["ki"], name or "claude"
    KI_ANBIETER, KI_NAME = name, anbieter.name(name) if name else "KI"
    CHAT_MODELL = c.get("modell") or anbieter.standard(basis, "modell")   # Chat und Läufe mit `modell: standard`
    KLEIN_MODELL = c.get("klein") or anbieter.standard(basis, "klein")    # Läufe mit `modell: klein`
    CHAT_EFFORT = c.get("effort") or anbieter.standard(basis, "effort")
    MODELLE = tuple(dict.fromkeys([CHAT_MODELL, KLEIN_MODELL]))


ki_setzen(CFG["ki"].get("anbieter") or "")


# --- Einrichtung und Einrichtungsseite ------------------------------------------------
# Mit [web] adresse richtet die Person alles selbst ein. Der Einladungslink t.me/<bot>?start=<code> öffnet ihren Bot,
# der merkt sich beim ersten Start ihr Telegram-Konto und schickt einen Knopf zur Einrichtungsseite (pforte.py):
# Namen, KI, Kalender und Mail. Danach entsteht home/ aus der Vorlage und der Assistent stellt sich vor.

NAME_MUSTER = re.compile(r"[^\W\d_][\w .'-]{0,29}")
MAIL_MUSTER = re.compile(r"[^@\s\"'\\]+@[^@\s\"'\\]+\.[A-Za-z]{2,}")
GOOGLE_NEUSTART = os.environ.get("ASSISTENT_GOOGLE_NEUSTART") or \
    "systemctl --user enable google-mcp.service && systemctl --user restart google-mcp.service"
EINRICHTEN_SPAETESTENS = 30 * 60   # KI angemeldet, Seite liegen gelassen: danach ohne Google abschließen
BOT = ""                           # Benutzername des Bots


def eingerichtet():
    """home/ ist angelegt. Vorher wartet alle Arbeit, die Person richtet gerade ein."""
    return os.path.isfile(os.path.join(HOME_DIR, "CLAUDE.md"))


def bot_name():
    global BOT
    if not BOT:
        BOT = telegram("getMe")["result"].get("username") or ""
    return BOT


def bot_sicher():
    try:
        return bot_name()
    except Exception:
        return ""


def einladungslink():
    """t.me-Link für die Person. Der Code gilt, bis sich das erste Telegram-Konto damit meldet."""
    if PERSON_ID:
        return ""
    code = secret("EINLADUNG")
    if not code:
        code = secrets.token_hex(8)
        kit.setze_secret("EINLADUNG", code)
    return f"https://t.me/{bot_name()}?start={code}"


def anmeldelink(neu=False):
    """Link zur Einrichtungsseite. Ein noch mindestens zehn Minuten gültiger Link wird wiederverwendet."""
    with _lock:
        st = load()
        a = st.get("anmeldelink") or {}
        if neu or not a.get("t") or a.get("bis", 0) < time.time() + 600:
            a = {"t": secrets.token_urlsafe(24), "bis": time.time() + ANMELDE_GUELTIG}
            st["anmeldelink"] = a
            save(st)
    return f"{WEB}/einrichten?t={a['t']}"


def link_gueltig(t):
    a = load().get("anmeldelink") or {}
    return bool(t and a.get("t")) and hmac.compare_digest(str(t), a["t"]) and a.get("bis", 0) > time.time()


def sende_knopf(text, knopf, url):
    """Nachricht mit einem Knopf, der die Seite in Telegram öffnet (Web App). Geht das nicht, ein normaler Link."""
    for art in ("web_app", "url"):
        ziel = {"url": url} if art == "web_app" else url
        try:
            return telegram("sendMessage", {"chat_id": PERSON_ID, "text": html(text), "parse_mode": "HTML",
                                            "reply_markup": {"inline_keyboard": [[{"text": knopf, art: ziel}]]}})
        except TelegramFehler as e:
            if e.code != 400:
                raise
            log(f"Knopf als {art} abgelehnt ({e})")
    sende_text(f"{text}\n\n{knopf}: {url}")


def einrichten_hinweis(immer=False, wo=""):
    """Knopf zur Einrichtungsseite. Ungefragt höchstens alle zehn Minuten. wo: ki oder google, springt dorthin."""
    if not immer and time.time() - load().get("anmelde_hinweis", 0) < 600:
        return
    update(anmelde_hinweis=time.time())
    if not WEB:
        return sende_text(f"Ich bin noch mit keiner KI verbunden. {ADMIN} richtet das ein, danach antworte ich dir.")
    url = anmeldelink(neu=True) + (f"&wo={wo}" if wo else "")   # kein #: Telegram belegt den Hash der Web App
    if not eingerichtet():
        text = ("Tipp auf Einrichten, das dauert drei Minuten. Du brauchst nur dein Abo bei Claude oder ChatGPT."
                + (" Deine Nachrichten beantworte ich, sobald ich eingerichtet bin." if not immer else ""))
        return sende_knopf(text, "Einrichten", url)
    if wo == "profil":
        return sende_knopf("Damit ich dich schneller kennenlerne: sieben kurze Fragen, das dauert drei Minuten.",
                           "Fragen beantworten", url)
    if wo == "google":
        return sende_knopf("Hier verbindest du deinen Kalender und deine Mails oder schaltest einen davon ab.",
                           "Kalender und Mail", url)
    sende_knopf((f"Hier meldest du mich neu an oder wechselst von {KI_NAME} zu einer anderen KI." if KI_ANBIETER else
                 "Hier meldest du mich bei deiner KI an, das dauert zwei Minuten."), "Anmelden", url)


anmelden_hinweis = einrichten_hinweis   # alter Name


OHNE_CODE_GEANTWORTET = set()


def binden(frm, text):
    """Erste Nachricht über den Einladungslink: Dieses Telegram-Konto gehört ab jetzt zum Assistenten."""
    global PERSON_ID, PERSON
    code, teile = secret("EINLADUNG"), text.split()
    if not (code and len(teile) == 2 and teile[0].lower().split("@")[0] == "/start"
            and hmac.compare_digest(teile[1], code)):
        log(f"Fremdes Konto {frm.get('id')} ignoriert (noch niemand verbunden)")
        if teile and teile[0].lower().split("@")[0] == "/start" and frm.get("id") not in OHNE_CODE_GEANTWORTET:
            OHNE_CODE_GEANTWORTET.add(frm.get("id"))   # einmal je Konto, mehr verrät der Bot Fremden nicht
            sende_text("Öffne mich bitte über den Link oder den QR-Code aus der Anleitung, dann tipp auf Starten. "
                       "Darin steckt dein persönlicher Code.", chat=frm["id"])
        return
    PERSON_ID = int(frm["id"])
    kit.setze_config("person", "telegram_id", PERSON_ID)
    kit.setze_secret("EINLADUNG", "")
    vorname = (frm.get("first_name") or "").strip()
    if not PERSON and NAME_MUSTER.fullmatch(vorname):
        kit.setze_config("person", "name", vorname)
        PERSON = vorname
    log("Telegram-Konto über den Einladungslink verbunden")
    sende_text(f"Hallo{' ' + PERSON if PERSON else ''}, schön, dass du da bist. Ich bin dein neuer Assistent.")
    einrichten_hinweis(immer=True)


def werte_alt():
    """Platzhalter und Google wie gerade in home/ angelegt, für einrichten.home_erneuern."""
    cfg = kit.lies_config()
    return einrichten.werte(cfg), bool(cfg["google"]["konto"])


def name_setzen(person, assistent):
    """Namen von der Einrichtungsseite. Rückgabe: Fehlertext oder ''."""
    global PERSON, NAME
    person, assistent = " ".join((person or "").split()), " ".join((assistent or "").split())
    if not NAME_MUSTER.fullmatch(person) or not NAME_MUSTER.fullmatch(assistent):
        return "Bitte zwei Namen mit Buchstaben, höchstens 30 Zeichen."
    alt = werte_alt() if eingerichtet() else None
    kit.setze_config("person", "name", person)
    kit.setze_config("assistent", "name", assistent)
    CFG["person"]["name"], CFG["assistent"]["name"] = person, assistent
    PERSON, NAME = person, assistent
    if alt:
        neu = einrichten.home_erneuern(*alt, *werte_alt())
        log(f"Namen geändert, {len(neu)} Dateien aus der Vorlage erneuert")
    try:
        telegram("setMyName", {"name": assistent})
    except Exception as e:
        log(f"Name des Bots nicht gesetzt ({e})")
    return ""


def google_starten(konto, dienste):
    """Schalter Kalender und Mail: Konto und Dienste übernehmen, Google-Dienst neu starten, Anmeldelink holen.
    Rückgabe: (link, fehler)."""
    global GOOGLE_KONTO
    konto = (konto or "").strip().lower()
    dienste = [d for d in kit.GOOGLE_DIENSTE if d in (dienste or [])]
    if not GOOGLE_PORT:
        return None, "Google ist auf dieser Box nicht eingerichtet."
    if not MAIL_MUSTER.fullmatch(konto):
        return None, "Das sieht nicht nach einer Google-Adresse aus."
    if not dienste:
        return None, "Schalte Kalender, Mail oder beides ein."
    alt = werte_alt() if eingerichtet() else None
    kit.setze_config("google", "konto", konto)
    kit.setze_config("google", "dienste", ",".join(dienste))
    CFG["google"]["konto"], CFG["google"]["dienste"] = konto, ",".join(dienste)
    GOOGLE_KONTO = konto
    einrichten.technik(kit.lies_config())
    if alt:
        einrichten.home_erneuern(*alt, *werte_alt())
    r = subprocess.run(GOOGLE_NEUSTART, shell=True, capture_output=True, text=True, timeout=60)
    if r.returncode:
        log(f"Google-Dienst nicht gestartet: {(r.stderr or r.stdout).strip()[-300:]}")
        return None, "Der Google-Dienst startet gerade nicht. Versuch es in ein paar Minuten noch einmal."
    update(google_verbunden=None)
    ende = time.time() + 40   # workspace-mcp braucht nach dem Start ein paar Sekunden
    while True:
        try:
            link = google_link()
            if link:
                return link, ""
        except (requests.RequestException, ValueError) as e:
            if time.time() > ende:
                log(f"Google-Link nicht geholt ({type(e).__name__})")
                break
        if time.time() > ende:
            break
        time.sleep(1)
    return None, "Google antwortet gerade nicht. Versuch es in ein paar Minuten noch einmal."


def google_ueberspringen():
    update(google_uebersprungen=time.time())


def anmeldung_fertig(name):
    """Die Einrichtungsseite meldet eine gelungene Anmeldung: Anbieter übernehmen, Link entwerten, Bescheid geben.
    Während der Einrichtung schweigt der Chat, die Seite geht weiter zu Kalender und Mail."""
    global RECHTE
    alt = KI_ANBIETER
    kit.setze_config("ki", "anbieter", name)
    CFG["ki"]["anbieter"] = name
    if alt != name:   # Modelle und Rechte des alten Anbieters passen nicht mehr
        for feld in ("modell", "klein", "effort"):
            kit.setze_config("ki", feld, "")
            CFG["ki"][feld] = ""
        if RECHTE not in (kit.RECHTE if name == "claude" else kit.RECHTE_ANDERE):
            kit.setze_config("ki", "rechte", "auto")
            CFG["ki"]["rechte"] = RECHTE = "auto"
    ki_setzen(name)
    fertig = eingerichtet()
    with _lock:
        st = load()
        if fertig:
            st.pop("anmeldelink", None)
        st.pop("anmelde_hinweis", None)
        st["gesperrt"], st["limit_gemeldet"], st["ki_seit"] = {}, {}, time.time()
        if alt and alt != name:   # Gespräche des alten Anbieters lassen sich mit dem neuen nicht fortsetzen
            st["sitzung"], st["tag"], st["kontext"], st["zu_sichern"] = None, None, 0, []
        save(st)
    puls(anmeldung=None)
    log(f"Angemeldet bei {KI_NAME}" + (f" (vorher {anbieter.name(alt)})" if alt and alt != name else ""))
    if fertig:
        begruessen = not load().get("begruesst")
        if begruessen:
            update(begruesst=time.time())
        notiere("System", f"Angemeldet bei {KI_NAME}")
        sende_text(f"Ich bin jetzt mit {KI_NAME} verbunden. "
                   + (f"Hallo {PERSON}, ich bin {NAME}. " + BEGRUESSUNG if begruessen else "Schreib mir einfach weiter."))


def abschliessen():
    """Letzter Schritt der Seite (oder von selbst, wenn sie liegen bleibt): home/ anlegen, vorstellen.
    Rückgabe: Fehlertext oder ''."""
    if not KI_ANBIETER:
        return "Erst die KI anmelden."
    if not PERSON or not NAME:
        return "Erst eure Namen eintragen."
    if not eingerichtet():
        cfg = kit.lies_config()
        neu = einrichten.home_anlegen(einrichten.werte(cfg), bool(cfg["google"]["konto"]))
        log(f"Eingerichtet: {len(neu)} Dateien in home/")
        notiere("System", f"Eingerichtet mit {KI_NAME}" + (", Google " + GOOGLE_KONTO if GOOGLE_KONTO else ""))
    with _lock:
        st = load()
        st.pop("anmeldelink", None)
        begruessen = not st.get("begruesst") and not st.get("fragebogen")   # sonst meldet sich die KI selbst
        st["begruesst"] = st.get("begruesst") or time.time()
        save(st)
    if begruessen:
        sende_text(f"Fertig, {PERSON}. Ich bin {NAME}. " + BEGRUESSUNG)
    return ""


def liegen_geblieben():
    """Zeitplan: Ist die KI angemeldet und die Seite seit EINRICHTEN_SPAETESTENS liegen geblieben, ohne Google
    abschließen. Rückgabe: eingerichtet?"""
    if eingerichtet():
        return True
    if WEB and KI_ANBIETER and PERSON and NAME and time.time() - load().get("ki_seit", time.time()) > EINRICHTEN_SPAETESTENS:
        log("Einrichtungsseite liegen geblieben, schließe ohne Google ab")
        return not abschliessen()
    return False


FRAGEBOGEN = (("wohnort", "Wo wohnst du?"), ("arbeit", "Was machst du beruflich oder tagsüber?"),
              ("menschen", "Wer gehört zu deinem Alltag?"), ("woche", "Was steht jede Woche fest an?"),
              ("hilfe", "Wobei soll ich vor allem helfen?"), ("ton", "Wie soll ich mit dir reden?"),
              ("sonst", "Was sollte ich sonst noch wissen?"))
HILFE_WAHL = ("Termine", "Fristen und Papierkram", "Haushalt und Einkauf", "Ideen festhalten", "Arbeit",
              "Gesundheit und Sport")
TON_WAHL = ("Locker", "Herzlich", "Kurz und sachlich")


def fragebogen_speichern(antworten):
    """Antworten von der Einrichtungsseite: merken und der KI den Auftrag geben, daraus Wissen zu machen und sich
    mit einer ersten Nachricht zu melden. Rückgabe: Fehlertext oder ''."""
    a = {}
    for k, _ in FRAGEBOGEN:
        v = antworten.get(k) if isinstance(antworten, dict) else None
        if k == "hilfe":
            v = [x for x in HILFE_WAHL if isinstance(v, list) and x in v]
        elif k == "ton":
            v = v if v in TON_WAHL else ""
        else:
            v = " ".join(str(v or "").split())[:600]
        if v:
            a[k] = v
    if not a:
        return "Beantworte mindestens eine Frage oder tipp auf Überspringen."
    with _lock:
        st = load()
        erneut = bool(st.get("fragebogen"))
        st["fragebogen"] = {"antworten": a, "zeit": time.time()}
        st.pop("fragebogen_uebersprungen", None)
        save(st)
    zeilen = "\n".join(f"- {frage} {', '.join(a[k]) if isinstance(a[k], list) else a[k]}"
                       for k, frage in FRAGEBOGEN if k in a)
    bis = (jetzt() + datetime.timedelta(days=7)).date().isoformat()
    prompt = (f"[Einrichtung, {stempel()}: {PERSON} hat den Kennenlern-Fragebogen {'geändert' if erneut else 'ausgefüllt'}. "
              f"Die Antworten, wörtlich:]\n{zeilen}\n\n"
              "Mach daraus Wissen, ohne etwas hinzuzudichten:\n"
              f"1. Trag die Fakten in `profil/person.md` in die passenden Abschnitte ein. Menschen mit Namen bekommen "
              "zusätzlich eine Zeile in `leben/menschen.md` (wer, Beziehung, was {PERSON} über sie gesagt hat).\n"
              "2. Den gewünschten Ton und wobei du helfen sollst, trägst du in `profil/assistent.md` ein.\n"
              "3. In `profil/fragen.md` streichst du, was jetzt beantwortet ist, und ergänzt drei bis fünf Nachfragen zu "
              "dem, was fehlt oder neugierig macht. "
              + ("" if erneut else f"Oben in die Datei kommt die Zeile `Kennenlernwoche bis: {bis}`. ")
              + "Feste Wochentermine legst du nicht ohne Nachfrage als Zeitpläne an.\n\n"
              + ("Die Bestätigung hat der Bot schon geschickt, antworte danach nur `STILL`."
                 if erneut else
                 f"Dann schreibst du {PERSON} deine erste Nachricht: zwei bis vier Sätze, die zeigen, dass du zugehört "
                 "hast (ein, zwei Dinge aus den Antworten aufgreifen, nicht alles aufzählen). Sag beiläufig, dass "
                 f"{PERSON} dir Verträge und Briefe einfach als Foto oder PDF schicken kann, und stell genau eine "
                 "Nachfrage. Kein Fragebogen-Ton, keine Zusammenfassung, keine Liste."))
    einreihen("job", ["kennenlernen", {"prompt": prompt, "gespraech": "chat", "modell": "standard",
                                         "effort": "medium", "melden": "antwort"}])
    try:   # sofort ein Zeichen in Telegram, die Nachricht der KI braucht ein, zwei Minuten
        sende_text("Danke, deine Änderungen habe ich übernommen." if erneut else
                   "Danke dir, ich habe deine Antworten bekommen und sortiere sie gerade ein. "
                   "In ein, zwei Minuten melde ich mich.")
    except Exception as e:
        log(f"Bestätigung Fragebogen nicht gesendet: {type(e).__name__}: {e}")
    notiere("System", f"Fragebogen {'geändert' if erneut else 'ausgefüllt'}")
    log(f"Fragebogen {'geändert' if erneut else 'ausgefüllt'}: {', '.join(a)}")
    return ""


def fragebogen_ueberspringen():
    update(fragebogen_uebersprungen=time.time())


def fragebogen_anbieten():
    """Ältere Einrichtungen ohne Fragebogen bekommen ihn einmal angeboten."""
    st = load()
    if not WEB or not eingerichtet() or st.get("fragebogen") or st.get("fragebogen_uebersprungen") \
            or st.get("fragebogen_angeboten"):
        return
    update(fragebogen_angeboten=time.time())
    einrichten_hinweis(immer=True, wo="profil")


def verbundene():
    """Anbieter mit hinterlegtem Zugang, in der Reihenfolge der Einrichtungsseite."""
    return [k for k in anbieter.REIHE if anbieter.angemeldet(k)]


def ki_wechseln(name):
    """Zu einer schon verbundenen KI wechseln, ohne neue Anmeldung. Ein offenes Gespräch sichert erst noch die
    bisherige KI (im Arbeiter-Faden), danach gilt die neue. Rückgabe: Fehlertext oder ''."""
    if name not in anbieter.ANBIETER or not anbieter.angemeldet(name):
        return "Diese KI ist noch nicht verbunden."
    if name == KI_ANBIETER:
        return ""
    if load().get("sitzung"):
        einreihen("ki", name)
    else:
        anmeldung_fertig(name)
    return ""


def ki_uebersicht():
    """/ki: aktive KI, Wechsel per Befehl zum Antippen, Knopf für eine weitere."""
    andere = [k for k in verbundene() if k != KI_ANBIETER]
    text = f"Du sprichst gerade mit {KI_NAME}." if KI_ANBIETER else "Ich bin noch mit keiner KI verbunden."
    if andere:
        text += "\n" + "\n".join(f"Zu {anbieter.name(k)} wechseln: /ki_{k}" for k in andere)
    fehlt = [anbieter.name(k) for k in anbieter.ANGEBOTEN if k not in verbundene()]
    if not WEB:
        return sende_text(text)
    if fehlt:
        text += f"\n\nDu kannst zusätzlich {' oder '.join(fehlt)} verbinden und dann jederzeit wechseln."
    url = anmeldelink(neu=True) + "&wo=ki"
    sende_knopf(text, "Weitere KI verbinden" if fehlt else "KI neu anmelden", url)


def einrichtung_stand():
    st = load()
    return {"person": PERSON, "assistent": NAME, "anbieter": KI_ANBIETER, "eingerichtet": eingerichtet(),
            "verbunden": verbundene(),
            "fragebogen": {"erledigt": bool(st.get("fragebogen") or st.get("fragebogen_uebersprungen")),
                           "antworten": (st.get("fragebogen") or {}).get("antworten", {}),
                           "hilfe": list(HILFE_WAHL), "ton": list(TON_WAHL)},
            "google": {"moeglich": bool(GOOGLE_PORT), "konto": GOOGLE_KONTO,
                       "dienste": [d for d in CFG["google"]["dienste"].split(",") if d],
                       "verbunden": bool(st.get("google_verbunden")), "uebersprungen": bool(st.get("google_uebersprungen"))},
            "whatsapp": {"moeglich": wa_moeglich(), "wahl": st.get("wa_onboarding") or "", "an": wa_status() == "an"}}


def google_web(query):
    """Rückweg von Google über die Seite: an den Google-Dienst der Instanz weitergeben. Rückgabe: ok?"""
    if not GOOGLE_PORT or not query:
        return False
    try:
        r = requests.get(f"http://127.0.0.1:{GOOGLE_PORT}/oauth2callback?{query}", timeout=60,
                         headers={"Authorization": f"Bearer {secret('GOOGLE_MCP_TOKEN')}"})
        ok = r.ok
        log(f"Google-Anmeldung über die Seite: HTTP {r.status_code}")
    except requests.RequestException as e:
        ok = False
        log(f"Google-Anmeldung: Dienst nicht erreicht ({type(e).__name__})")
    if ok:
        update(google_verbunden=time.time())
        if eingerichtet():
            notiere("System", "Google-Anmeldung erfolgreich")
            try:
                sende_text("Google ist verbunden, ab jetzt sehe ich, was du freigegeben hast.")
            except Exception:
                pass
    return ok


def pforte_starten():
    if not WEB_PORT:
        return
    try:
        srv = pforte.starten(WEB_PORT, {
            "name": lambda: NAME or "Dein Assistent", "bot": bot_sicher, "link_gueltig": link_gueltig,
            "fertig": anmeldung_fertig, "google": google_web, "stand": einrichtung_stand, "namen": name_setzen,
            "google_start": google_starten, "google_aus": google_ueberspringen, "abschliessen": abschliessen,
            "wechseln": ki_wechseln, "fragebogen": fragebogen_speichern,
            "fragebogen_aus": fragebogen_ueberspringen, **({"wa_link_gueltig": wa_link_gueltig,
            "wa_stand": wa_seitenstand, "wa_start": wa_seite_start, "wa_onboarding": wa_onboarding}
                                                           if wa_moeglich() else {})})
        log(f"Einrichtungsseite auf 127.0.0.1:{WEB_PORT}" + (f", erreichbar unter {WEB}" if WEB else ""))
        return srv
    except OSError as e:
        log(f"Einrichtungsseite nicht gestartet ({e})")


def log(text):
    print(text, flush=True)


# --- Zustand und Schluessel ---------------------------------------------------

def load():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE + f".{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def update(**kw):
    with _lock:
        st = load()
        st.update(kw)
        save(st)
        return st


def puls(**kw):
    """Lebenszeichen fuer die Wache: telegram (letzter Abruf), claude_ok (letzter Lauf ohne Fehler), arbeit."""
    with _lock:
        st = load()
        st.setdefault("puls", {}).update(kw)
        save(st)


def secret(name):
    return kit.secret(name)


def jetzt():
    return datetime.datetime.now(TZ)


def stempel(t=None):
    t = t or jetzt()
    return f"{WOCHENTAG[t.weekday()]} {t:%d.%m.%Y %H:%M}"


def zeitpunkt(ts):
    """Zeitpunkt fuer die Person: „14:30“, „morgen 10:00“ oder „Sa 21.11. 10:00“."""
    t, heute = datetime.datetime.fromtimestamp(ts, TZ), jetzt().date()
    if t.date() == heute:
        return f"{t:%H:%M}"
    if t.date() == heute + datetime.timedelta(days=1):
        return f"morgen {t:%H:%M}"
    return f"{WOCHENTAG[t.weekday()]} {t:%d.%m. %H:%M}"


def gespraechstag(t=None):
    return ((t or jetzt()) - datetime.timedelta(hours=TAGESWECHSEL)).date().isoformat()


def verlauf_pfad():
    return os.path.join(VERLAUF, gespraechstag() + ".md")


def logbuch_pfad(tag=None):
    return os.path.join(LOGBUCH, (tag or gespraechstag()) + ".md")


def notiere(wer, text):
    """Mitschrift des Tages (gekuerzt, 14 Tage, fuer Herzschlag und eigene Laeufe) und Logbuch (ungekuerzt,
    dauerhaft, damit der Assistent nichts vergisst, was gesagt wurde)."""
    t = jetzt()
    os.makedirs(VERLAUF, exist_ok=True)
    kurz = text.strip().replace("\n", " ")
    with open(verlauf_pfad(), "a", encoding="utf-8") as f:
        f.write(f"[{t:%H:%M}] {wer}: {kurz[:800]}{'…' if len(kurz) > 800 else ''}\n")
    os.makedirs(LOGBUCH, exist_ok=True)
    pfad = logbuch_pfad()
    neu = not os.path.exists(pfad)
    with open(pfad, "a", encoding="utf-8") as f:
        if neu:
            tag = datetime.date.fromisoformat(gespraechstag())
            f.write(f"# Logbuch {WOCHENTAG[tag.weekday()]} {tag:%d.%m.%Y}\n\n")
        f.write(f"**{t:%H:%M} {wer}:** {text.strip()}\n\n")


def aufraeumen():
    grenze = time.time() - VERLAUF_TAGE * 86400
    for pfad in glob.glob(os.path.join(VERLAUF, "*.md")) + glob.glob(os.path.join(EINGANG, "*")):
        if os.path.getmtime(pfad) < grenze:
            os.remove(pfad)
    with _lock:   # Merker einmaliger Laeufe, deren Datei es nicht mehr gibt
        st = load()
        da = set(jobs())
        alt = [n for n, d in st.get("jobs", {}).items() if n not in da and d < (jetzt().date() - datetime.timedelta(days=7)).isoformat()]
        for n in alt:
            del st["jobs"][n]
        if alt:
            save(st)


# --- Telegram -----------------------------------------------------------------

def telegram(method, params=None, files=None, timeout=20):
    url = f"{TELEGRAM_API}/bot{secret('TG_TOKEN')}/{method}"
    data = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
            for k, v in (params or {}).items()}
    # Verbindungsaufbau kurz halten: haengt IPv6 zu Telegram, versucht urllib3 nach 5 s IPv4 statt nach der vollen Zeit (24.10.2026)
    r = requests.post(url, data=data, files=files, timeout=(5, timeout))
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not r.ok:
        raise TelegramFehler(r.status_code, body.get("description", ""))
    return body


class TelegramFehler(Exception):
    def __init__(self, code, text):
        super().__init__(f"HTTP {code}: {text}")
        self.code = code


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def html(md):
    """Das Markdown, das die KI schreibt, als Telegram-HTML: fett, kursiv, Code, Links, Aufzaehlungen."""
    teile = re.split(r"```[\w+-]*\n?(.*?)```", md, flags=re.S)
    out = []
    for i, t in enumerate(teile):
        if i % 2:
            out.append("<pre>" + esc(t.strip("\n")) + "</pre>")
            continue
        t = esc(t)
        t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
        t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
        t = re.sub(r"^(\s*)[-*] ", r"\1• ", t, flags=re.M)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
        t = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", t)
        out.append(t)
    return "".join(out)


def stuecke(text, laenge=3500):
    """Lange Antworten an Absatzgrenzen teilen, Telegram nimmt hoechstens 4096 Zeichen."""
    out, rest = [], text.strip()
    while len(rest) > laenge:
        cut = max(rest.rfind("\n\n", 0, laenge), rest.rfind("\n", 0, laenge))
        cut = cut if cut > laenge // 3 else laenge
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return out + ([rest] if rest else [])


def sende_text(text, chat=None, knoepfe=None):
    """knoepfe: Antworten zum Antippen (Telegram-Tastatur unter dem Chat), hängen an der letzten Nachricht.
    Ein Tipp schickt den Text des Knopfs als normale Nachricht der Person."""
    chat = chat or PERSON_ID
    teile = stuecke(text)
    for i, teil in enumerate(teile):
        extra = {}
        if knoepfe and i == len(teile) - 1:
            extra["reply_markup"] = {"keyboard": [[{"text": k}] for k in knoepfe],
                                     "resize_keyboard": True, "one_time_keyboard": True}
        try:
            telegram("sendMessage", {"chat_id": chat, "text": html(teil), "parse_mode": "HTML",
                                     "disable_web_page_preview": "true", **extra})
        except TelegramFehler as e:
            if e.code != 400:
                raise
            telegram("sendMessage", {"chat_id": chat, "text": teil, "disable_web_page_preview": "true", **extra})


def sende_datei(pfad, chat=None):
    chat = chat or PERSON_ID
    bild = pfad.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    with open(pfad, "rb") as f:
        telegram("sendPhoto" if bild else "sendDocument", {"chat_id": chat},
                 files={"photo" if bild else "document": (os.path.basename(pfad), f)}, timeout=120)


def dateien_aus(text):
    """Zeilen `[datei: <pfad>]` aus der Antwort ziehen. Rueckgabe: Text ohne diese Zeilen, Pfade."""
    pfade, zeilen = [], []
    for z in text.splitlines():
        m = re.fullmatch(r"\s*\[datei:\s*(.+?)\]\s*", z)
        if not m:
            zeilen.append(z)
            continue
        p = os.path.expanduser(m.group(1).strip().strip("`"))
        p = p if os.path.isabs(p) else os.path.join(HOME_DIR, p)
        p = os.path.realpath(p)
        if GEHEIM.search(p) or not os.path.isfile(p):
            zeilen.append(f"(Datei {os.path.basename(p)} kann ich nicht schicken)")
        else:
            pfade.append(p)
    return "\n".join(zeilen).strip(), pfade


def knoepfe_aus(text):
    """Zeilen `[knopf: <antwort>]` aus der Antwort ziehen, höchstens vier. Rückgabe: Text ohne diese Zeilen, Knöpfe."""
    knoepfe, zeilen = [], []
    for z in text.splitlines():
        m = re.fullmatch(r"\s*\[knopf:\s*(.+?)\]\s*", z)
        if m and len(knoepfe) < 4:
            knoepfe.append(m.group(1).strip().strip("`")[:60])
        elif not m:
            zeilen.append(z)
    return "\n".join(zeilen).strip(), knoepfe


def antworte(text):
    text, pfade = dateien_aus(text)
    text, knoepfe = knoepfe_aus(text)
    if text:
        sende_text(text, knoepfe=knoepfe)
    for p in pfade:
        try:
            sende_datei(p)
        except Exception as e:
            log(f"Datei nicht gesendet ({type(e).__name__})")
            sende_text(f"Die Datei {os.path.basename(p)} ging nicht durch.")


class Tippen:
    """Zeigt in Telegram „schreibt …“, solange die KI arbeitet."""

    def __init__(self, aktiv=True):
        self.aktiv, self.stop = aktiv, threading.Event()

    def __enter__(self):
        if self.aktiv:
            threading.Thread(target=self._lauf, daemon=True).start()
        return self

    def _lauf(self):
        while not self.stop.is_set():
            try:
                telegram("sendChatAction", {"chat_id": PERSON_ID, "action": "typing"}, timeout=10)
            except Exception:
                pass
            self.stop.wait(4.5)

    def __exit__(self, *_):
        self.stop.set()


# --- Eingang: Nachrichten in Text fuer die KI ---------------------------------

def lade_datei(file_id, name):
    info = telegram("getFile", {"file_id": file_id})["result"]
    os.makedirs(EINGANG, exist_ok=True)
    basis = os.path.join(EINGANG, f"{jetzt():%Y-%m-%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9._-]+', '-', name)}")
    ziel, n = basis, 1
    while os.path.exists(ziel):   # Alben kommen in derselben Sekunde, sonst ueberschreibt Foto 2 Foto 1
        n += 1
        stamm, endung = os.path.splitext(basis)
        ziel = f"{stamm}-{n}{endung}"
    url = f"{TELEGRAM_API}/file/bot{secret('TG_TOKEN')}/{info['file_path']}"
    with requests.get(url, timeout=120, stream=True) as r:
        r.raise_for_status()
        with open(ziel, "wb") as f:
            for block in r.iter_content(1 << 16):
                f.write(block)
    return ziel


def transkribiere(pfad):
    key = secret("OPENAI_API_KEY")
    if not key:
        return None
    with open(pfad, "rb") as f:
        r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {key}"},
                          files={"file": ("sprachnachricht.ogg", f)},
                          data={"model": "whisper-1", "language": SPRACHE}, timeout=180)
    r.raise_for_status()
    return r.json().get("text", "").strip()


def rel(p):
    return os.path.relpath(p, HOME_DIR)


def als_text(m):
    """Eine Telegram-Nachricht als Zeile fuer die KI. Medien landen in eingang/, Sprache wird Text."""
    teile = []
    fo = m.get("forward_origin") or {}
    if fo:
        von = (fo.get("sender_user") or {}).get("first_name") or fo.get("sender_user_name") \
            or (fo.get("chat") or {}).get("title") or "unbekannt"
        teile.append(f"[weitergeleitet von {von}]")
    r = m.get("reply_to_message")
    if r:
        zitat = (r.get("text") or r.get("caption") or "").strip().replace("\n", " ")
        teile.append(f"[Antwort auf: „{zitat[:300]}{'…' if len(zitat) > 300 else ''}“]")
    if m.get("voice") or m.get("audio"):
        a = m.get("voice") or m.get("audio")
        p = lade_datei(a["file_id"], a.get("file_name") or "sprachnachricht.ogg")
        dauer = f"{a.get('duration', 0) // 60}:{a.get('duration', 0) % 60:02d}"
        try:
            t = transkribiere(p)
        except Exception as e:
            log(f"Transkription fehlgeschlagen ({type(e).__name__})")
            t = None
        teile.append(f"[Sprachnachricht {dauer}] {t}" if t else
                     f"[Sprachnachricht {dauer}, nicht transkribiert, liegt in {rel(p)}]")
    if m.get("photo"):
        p = lade_datei(m["photo"][-1]["file_id"], "foto.jpg")
        teile.append(f"[Foto: {rel(p)}]")
    if m.get("document"):
        d = m["document"]
        if d.get("file_size", 0) > 20 * 1024 * 1024:
            teile.append(f"[Datei {d.get('file_name', '')}, zu groß für den Bot (über 20 MB)]")
        else:
            p = lade_datei(d["file_id"], d.get("file_name") or "datei")
            teile.append(f"[Datei: {rel(p)}]")
    if m.get("video") or m.get("video_note"):
        teile.append("[Video, kann ich nicht ansehen]")
    if m.get("sticker"):
        teile.append(f"[Sticker {m['sticker'].get('emoji', '')}]")
    if m.get("location"):
        loc = m["location"]
        teile.append(f"[Standort: {loc['latitude']:.5f}, {loc['longitude']:.5f}]")
    if m.get("contact"):
        c = m["contact"]
        teile.append(f"[Kontakt: {c.get('first_name', '')} {c.get('last_name', '')}, {c.get('phone_number', '')}]")
    text = (m.get("text") or m.get("caption") or "").strip()
    if text:
        teile.append(text)
    return " ".join(teile).strip()


# --- KI-Läufe -----------------------------------------------------------------

TAETIGKEITEN = [(r"^(Agent|Task)$", "eine Teilaufgabe abzuarbeiten"), (r"gmail", "deine Mails durchzusehen"),
                (r"calendar|events|freebusy", "in die Kalender zu schauen"), (r"drive", "in Drive zu suchen"),
                (r"task", "deine Aufgaben zu pflegen"),
                (r"^(WebSearch|WebFetch)$", "im Netz nachzusehen"), (r"^(Read|Glob|Grep)$", "in meinen Akten zu lesen"),
                (r"^(Edit|Write|NotebookEdit)$", "meine Akten zu schreiben"), (r"^Bash$", "ein Werkzeug laufen zu lassen"),
                (r"^Skill$", "eine Anleitung zu lesen")]


def taetigkeit(werkzeuge):
    for w in reversed(werkzeuge):
        for muster, text in TAETIGKEITEN:
            if re.search(muster, w, re.I):
                return text
    return "etwas nachzuschlagen" if werkzeuge else "nachzudenken"


def beenden(p):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
            p.wait(timeout=10)
            return
        except (subprocess.TimeoutExpired, ProcessLookupError):
            continue


WERKZEUG_NAMEN = {"command_execution": "Bash", "web_search": "WebSearch", "file_change": "Edit",
                  "read_file": "Read", "read_many_files": "Read", "glob": "Glob", "search_file_content": "Grep",
                  "grep_search": "Grep", "write_file": "Write", "replace": "Edit", "run_shell_command": "Bash",
                  "google_web_search": "WebSearch", "web_fetch": "WebFetch"}   # Codex und Gemini wie bei Claude


def gedaechtnis_vorweg():
    """Codex und Gemini laden das Gedächtnis nicht von selbst: der Index kommt an den Anfang jedes Gesprächs."""
    try:
        text = open(os.path.join(HOME_DIR, "gedaechtnis", "MEMORY.md"), encoding="utf-8").read().strip()
    except OSError:
        return ""
    return f"[Dein Gedächtnis, Index aus `gedaechtnis/MEMORY.md`:]\n{text}\n[Ende des Index]\n\n" if text else ""


def _claude_aufruf(sitzung, modell, effort, extra):
    cmd = [anbieter.programm("claude"), "-p", "--output-format", "stream-json", "--verbose", "--model", modell,
           "--effort", effort, "--setting-sources", "project,local", "--settings", SETTINGS,
           "--mcp-config", MCP, "--strict-mcp-config", "--permission-mode", RECHTE, *extra]
    if sitzung:
        cmd += ["--resume", sitzung]
    env = {}
    if secret("CLAUDE_CODE_OAUTH_TOKEN"):   # Langzeit-Token aus `claude setup-token`, sonst der normale Login
        env["CLAUDE_CODE_OAUTH_TOKEN"] = secret("CLAUDE_CODE_OAUTH_TOKEN")
    return cmd, env


def _claude_zeile(d, st):
    if d.get("type") == "assistant":
        msg = d.get("message") or {}
        u = msg.get("usage") or {}
        k = sum(u.get(x) or 0 for x in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if k:
            st["kontext"] = k + (u.get("output_tokens") or 0)
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                st["werkzeuge"].append(b.get("name") or "")
    elif d.get("type") == "result":
        res = d
        if res.get("is_error") or res.get("subtype") not in (None, "success"):
            st["res"] = {"fehler": res.get("result") or res.get("subtype") or "Fehler", "text": res.get("result") or ""}
        else:
            st["res"] = {"text": (res.get("result") or "").strip()}
    if d.get("session_id"):
        st["sid"] = d["session_id"]


def _codex_aufruf(sitzung, modell, effort, extra):
    cmd = [anbieter.programm("chatgpt"), "exec", "--json", "--skip-git-repo-check", "-C", HOME_DIR]
    if modell and modell != "standard":
        cmd += ["-m", modell]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{ {"xhigh": "high", "max": "high"}.get(effort, effort)}"']
    if RECHTE == "voll":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd += [*extra, "resume", sitzung, "-"] if sitzung else [*extra, "-"]
    return cmd, {"CODEX_HOME": anbieter.CODEX_HOME}


def _codex_zeile(d, st):
    t, it = d.get("type"), d.get("item") or {}
    if t == "thread.started":
        st["sid"] = d.get("thread_id")
    elif t in ("item.started", "item.completed"):
        art = it.get("type")
        if art == "agent_message":
            if t == "item.completed" and (it.get("text") or "").strip():
                st["texte"].append(it["text"].strip())
        elif it.get("id") not in st["ids"]:
            st["ids"].add(it.get("id"))
            w = f"mcp__{it.get('server') or ''}__{it.get('tool') or ''}" if art == "mcp_tool_call" else WERKZEUG_NAMEN.get(art)
            if w:
                st["werkzeuge"].append(w)
    elif t == "turn.completed":
        u = d.get("usage") or {}
        st["kontext"] = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
        st["res"] = {"text": st["texte"][-1] if st["texte"] else ""}
    elif t == "turn.failed":
        st["res"] = {"fehler": (d.get("error") or {}).get("message") or "Fehler"}
    elif t == "error" and d.get("message"):
        st["meldungen"].append(d["message"])


def _gemini_aufruf(sitzung, modell, effort, extra):
    cmd = [anbieter.programm("gemini"), "-o", "stream-json", "--approval-mode", "yolo", "--skip-trust"]
    if modell and modell != "standard":
        cmd += ["-m", modell]
    cmd += [*extra, "--resume", sitzung] if sitzung else [*extra, "--session-id", str(uuid.uuid4())]
    return cmd, {"GEMINI_CLI_HOME": anbieter.GEMINI_HOME}


def _gemini_zeile(d, st):
    t = d.get("type")

    def abschliessen():
        if st["teil"].strip():
            st["texte"].append(st["teil"].strip())
        st["teil"] = ""
    if t == "init":
        st["sid"] = d.get("session_id")
    elif t == "message" and d.get("role") == "assistant":
        st["teil"] += d.get("content") or ""
    elif t == "tool_use":
        abschliessen()
        w = d.get("tool_name") or ""
        st["werkzeuge"].append(WERKZEUG_NAMEN.get(w, w))
    elif t == "error" and d.get("severity") == "error" and d.get("message"):
        st["meldungen"].append(d["message"])
    elif t == "result":
        abschliessen()
        stats = d.get("stats") or {}
        # Gemini nennt nur die Summe aller Modellaufrufe dieses Laufs; geteilt durch die Aufrufe ≈ Kontext am Ende
        st["kontext"] = (stats.get("input_tokens") or 0) // max(1, (stats.get("tool_calls") or 0) + 1)
        if d.get("status") == "success":
            st["res"] = {"text": st["texte"][-1] if st["texte"] else ""}
        else:
            st["res"] = {"fehler": (d.get("error") or {}).get("message") or "Fehler"}


LAEUFE = {"claude": (_claude_aufruf, _claude_zeile), "chatgpt": (_codex_aufruf, _codex_zeile),
          "gemini": (_gemini_aufruf, _gemini_zeile)}


def ki_lauf(prompt, sitzung=None, modell=None, effort=None, extra=(), zwischen=None):
    """Ein Lauf der KI in home/. Rueckgabe: (antwort, sitzung, fehler, kontext), kontext = Tokens im Kontext
    nach dem letzten Modellaufruf. zwischen(taetigkeit) ruft der Lauf einmal auf, wenn nach ZWISCHENSTAND Sekunden
    noch keine Antwort da ist."""
    if not KI_ANBIETER:
        return "", sitzung, "Noch keine KI angemeldet", 0
    modell, effort = modell or CHAT_MODELL, effort if effort is not None else CHAT_EFFORT
    aufruf, zeile = LAEUFE[KI_ANBIETER]
    if KI_ANBIETER != "claude" and not sitzung:
        prompt = gedaechtnis_vorweg() + prompt
    cmd, eigene = aufruf(sitzung, modell, effort, extra)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY",
                        "GEMINI_API_KEY", "GOOGLE_API_KEY")}
    env.update({k: secret(k) for k in KIND_ENV})
    env.update(eigene)
    env["PATH"] = f"{HOME}/.local/bin:{anbieter.KI_BIN}:/usr/local/bin:/usr/bin:/bin"
    try:
        p = subprocess.Popen(cmd, cwd=HOME_DIR, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        return "", sitzung, f"{KI_NAME} nicht startbar: {e}", 0
    st = {"werkzeuge": [], "kontext": 0, "res": None, "sid": None, "err": "", "texte": [], "teil": "",
          "meldungen": [], "ids": set()}

    def lesen():
        for z in p.stdout:
            try:
                d = json.loads(z)
            except ValueError:
                continue
            if isinstance(d, dict):
                zeile(d, st)

    def fehlerausgabe():
        st["err"] = p.stderr.read()

    leser = threading.Thread(target=lesen, daemon=True)
    leser.start()
    threading.Thread(target=fehlerausgabe, daemon=True).start()
    try:
        p.stdin.write(prompt)
        p.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    t0, gemeldet = time.time(), False
    while leser.is_alive():
        leser.join(5)
        dauer = time.time() - t0
        if zwischen and not gemeldet and dauer > ZWISCHENSTAND and st["res"] is None and leser.is_alive():
            gemeldet = True
            try:
                zwischen(taetigkeit(st["werkzeuge"]))
            except Exception as e:
                log(f"Zwischenstand nicht gesendet ({type(e).__name__})")
        if dauer > LAUF_MAX:
            beenden(p)
            leser.join(10)
            return "", st["sid"] or sitzung, \
                f"nach {LAUF_MAX // 60} Minuten abgebrochen (zuletzt: {taetigkeit(st['werkzeuge'])})", st["kontext"]
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        beenden(p)
    res = st["res"]
    if not isinstance(res, dict):
        time.sleep(0.2)
        grund = (st["meldungen"][-1] if st["meldungen"] else "") or (st["err"] or "").strip() or f"Exit {p.returncode}"
        return "", st["sid"] or sitzung, grund[-500:], st["kontext"]
    if res.get("fehler"):
        return res.get("text") or "", st["sid"] or sitzung, res["fehler"][-500:], st["kontext"]
    return res.get("text") or "", st["sid"] or sitzung, None, st["kontext"]


def sitzung_weg(fehler):
    """Lässt sich das Gespräch nicht fortsetzen, weil es nicht mehr da ist?"""
    return bool(fehler) and bool(re.search(r"no conversation found|invalid session identifier|no (?:rollout|session) "
                                           r"found|session .{0,40}not found|thread .{0,40}not found", fehler, re.I))


def anmeldung_kaputt(fehler):
    return any(w in fehler.lower() for w in ("authenticat", "oauth", "401", "login", "invalid api key"))


def fehlertext(fehler):
    f = fehler.lower()
    if anmeldung_kaputt(fehler):
        if WEB:
            return (f"Meine Anmeldung bei {KI_NAME} ist abgelaufen oder ungültig. Melde mich hier neu an, das dauert "
                    f"zwei Minuten: [Neu anmelden]({anmeldelink()})")
        return (f"Meine Anmeldung bei {KI_NAME} ist abgelaufen oder ungültig. {ADMIN} bekommt Bescheid und "
                "erneuert sie, danach bin ich wieder da.")
    if "abgebrochen" in f:
        m = re.search(r"\(zuletzt: (.+?)\)", fehler)
        return ("Das hat zu lange gedauert, ich habe nach 20 Minuten abgebrochen."
                + (f" Zuletzt war ich dabei, {m.group(1)}." if m else "")
                + " Sag mir, ob ich weitermachen soll, dann schaue ich zuerst, was schon fertig ist.")
    return "Ich hänge gerade: " + fehler.strip().splitlines()[-1][:300]


# --- Abo-Kontingent -----------------------------------------------------------

def limit_bis(fehler):
    """Reset aus „You've hit your weekly limit · resets 10am (Europe/Berlin)“, „resets 7:50pm“, „resets Nov 21,
    10am“ (Claude), „try again in 2 hours 13 minutes“, „try again at 3:04 PM“ (ChatGPT) oder „reset after
    13h21m5s“ (Gemini). Ohne Angabe: in einer Stunde."""
    t = jetzt()
    m = re.search(r"(?:try again in|reset(?:s)? (?:after|in))\s+(?:(\d+)\s*(?:days?|d)(?![a-z])\s*,?\s*(?:and\s+)?)?"
                  r"(?:(\d+)\s*(?:hours?|hrs?|h)(?![a-z])\s*,?\s*(?:and\s+)?)?"
                  r"(?:(\d+)\s*(?:minutes?|mins?|m)(?![a-z])\s*,?\s*(?:and\s+)?)?"
                  r"(?:(\d+)\s*(?:seconds?|secs?|s)(?![a-z]))?", fehler, re.I)
    if m and any(m.groups()):
        d, h, mi, sek = (int(x or 0) for x in m.groups())
        return time.time() + d * 86400 + h * 3600 + mi * 60 + sek + 60
    m = re.search(r"try again at\s+(\d{1,2}):(\d\d)\s*([ap]m)", fehler, re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        ziel = t.replace(hour=h, minute=int(m.group(2)), second=0, microsecond=0)
        if ziel <= t:
            ziel += datetime.timedelta(days=1)
        return ziel.timestamp() + 60
    m = re.search(r"resets\s+(?:(?:on\s+)?([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d\d))?\s*([ap]m)",
                  fehler, re.I)
    if not m:
        return time.time() + 3600
    h = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
    ziel = t.replace(hour=h, minute=int(m.group(4) or 0), second=0, microsecond=0)
    if m.group(1) and m.group(1).lower()[:3] in MONATE:
        try:
            ziel = ziel.replace(month=MONATE[m.group(1).lower()[:3]], day=int(m.group(2)))
        except ValueError:
            pass
        if ziel < t - datetime.timedelta(days=1):
            ziel = ziel.replace(year=ziel.year + 1)
    elif ziel <= t:
        ziel += datetime.timedelta(days=1)
    return ziel.timestamp() + 60


def frei(modell):
    return load().get("gesperrt", {}).get(modell, 0) < time.time()


def kontingent_frei(modell, ausweichen=True):
    return frei(modell) or (ausweichen and any(frei(m) for m in MODELLE))


def gesperrt(fehler):
    return bool(fehler) and fehler.startswith("GESPERRT")


def sperren(modell, fehler):
    """Modell bis zum Reset meiden und der Person einmal je Sperre Bescheid geben."""
    bis = limit_bis(fehler)
    with _lock:
        st = load()
        st.setdefault("gesperrt", {})[modell] = bis
        gemeldet = st.setdefault("limit_gemeldet", {})
        schon = gemeldet.get(modell, 0) > time.time()
        gemeldet[modell] = bis
        save(st)
    log(f"Kontingent {modell} aufgebraucht bis {zeitpunkt(bis)}: {fehler[:120]}")
    if schon:
        return
    andere = [m for m in MODELLE if m != modell and frei(m)]
    welches = f" für {modell.capitalize()}" if len(MODELLE) > 1 and modell != "standard" else ""
    text = f"Mein {KI_NAME}-Kontingent{welches} ist bis {zeitpunkt(bis)} aufgebraucht. "
    text += (f"Was nicht warten kann, mache ich mit {andere[0].capitalize()}, den Rest hole ich danach nach."
             if andere else "Bis dahin bleiben deine Nachrichten liegen, ich beantworte sie danach. Erinnerungen bekommst du trotzdem.")
    try:
        sende_text(text)
    except Exception as e:
        log(f"Hinweis zum Kontingent nicht gesendet ({type(e).__name__})")


def lauf(prompt, sitzung=None, modell=None, effort=None, ausweichen=True, zwischen=None):
    """ki_lauf() mit Blick aufs Abo: gesperrtes Modell meiden, am Limit sperren und aufs andere ausweichen.
    Rueckgabe wie ki_lauf(). Ist kein Modell frei, beginnt fehler mit GESPERRT."""
    modell = modell or CHAT_MODELL
    reihe = [modell] + ([m for m in MODELLE if m != modell] if ausweichen else [])
    for m in reihe:
        if not frei(m):
            continue
        antwort, sid, fehler, kontext = ki_lauf(prompt, sitzung, m, effort, zwischen=zwischen)
        if fehler and LIMIT.search(fehler):
            sperren(m, fehler)
            continue
        if not fehler:
            puls(claude_ok=time.time(), anmeldung=None)
        elif anmeldung_kaputt(fehler):
            puls(anmeldung=time.time())   # die Wache meldet das dem Admin
        return antwort, sid, fehler, kontext
    return "", sitzung, GESPERRT, 0


def warte_auf_kontingent():
    while not any(frei(m) for m in MODELLE):
        time.sleep(60)


# --- Gespraeche ---------------------------------------------------------------

def einstieg():
    """Erste Zeile eines neuen Gespraechs, wenn heute schon geredet wurde."""
    if not os.path.exists(logbuch_pfad()):
        return ""
    return (f"[Neues Gespräch. Was heute schon gesagt wurde, steht im Logbuch `{rel(logbuch_pfad())}`. "
            f"Bezieht {PERSON} sich auf etwas von vorhin, schau dort nach.]\n")


def im_gespraech(prompt, modell=None, effort=None, tippen=True, ausweichen=True, zwischen=None):
    """Prompt im laufenden Gespraech. Hat der Tag gewechselt, wird das alte erst zum Sichern vorgemerkt.
    Rueckgabe (antwort, fehler, kontext)."""
    st = load()
    if st.get("sitzung") and st.get("tag") != gespraechstag():
        wechsel("Tageswechsel")
        st = load()
    sitzung = st.get("sitzung")
    with Tippen(tippen):
        antwort, neu, fehler, kontext = lauf(prompt if sitzung else einstieg() + prompt, sitzung, modell, effort,
                                             ausweichen, zwischen)
        if sitzung and sitzung_weg(fehler):
            log(f"Gespräch {sitzung[:8]} nicht mehr da, beginne neu")
            update(sitzung=None, tag=None)
            sitzung = None
            antwort, neu, fehler, kontext = lauf(einstieg() + prompt, None, modell, effort, ausweichen, zwischen)
    if neu and neu != sitzung:
        update(sitzung=neu, tag=gespraechstag(), seit=time.time())
    if neu:
        update(letzte=time.time(), kontext=kontext or load().get("kontext", 0))
    return antwort, fehler, kontext


def wechsel(grund):
    """Aktuelles Gespraech schliessen: zum Sichern vormerken und freigeben. Nur im Arbeiter-Faden."""
    with _lock:
        st = load()
        s = st.get("sitzung")
        if s:
            st.setdefault("zu_sichern", []).append({"sitzung": s, "tag": st.get("tag") or gespraechstag(),
                                                     "grund": grund, "seit": time.time(), "versuche": 0})
        st["sitzung"], st["tag"], st["kontext"] = None, None, 0
        save(st)
    if s:
        log(f"Gespräch {s[:8]} geschlossen ({grund}), wird gesichert")


def sichere_offene():
    """Geschlossene Gespraeche sichern: der Assistent setzt jedes mit dem Auftrag aus jobs/tagesabschluss.md fort
    (Gedaechtnis, Akten, profil/laufend.md, Logbuch). Rueckgabe: ist nichts mehr offen?"""
    while True:
        offen = load().get("zu_sichern", [])
        if not offen:
            return True
        e = offen[0]
        j = jobs().get("tagesabschluss", {})
        modell, effort = modell_von(j), j.get("effort") or "medium"
        if not kontingent_frei(modell):
            return False
        prompt = (f"[Gesprächsende ({e['grund']}), {stempel()}. Logbuch dieses Gesprächs: "
                  f"`leben/logbuch/{e['tag']}.md`]\n" + (j.get("prompt") or "Halte in deinem Gedächtnis fest, was bleiben soll."))
        puls(arbeit={"was": "Gespräch sichern", "seit": time.time()})
        t0 = time.time()
        antwort, _, fehler, _ = lauf(prompt, e["sitzung"], modell, effort, ausweichen=True)
        if sitzung_weg(fehler):
            log(f"Gespräch {e['sitzung'][:8]} hat kein Transkript mehr, steht nur im Logbuch")
            fehler = None
        with _lock:
            st = load()
            liste = st.get("zu_sichern", [])
            eintrag = next((x for x in liste if x["sitzung"] == e["sitzung"]), None)
            if eintrag is not None:
                if not fehler:
                    liste.remove(eintrag)
                    st["abschluss"] = time.time()
                elif not gesperrt(fehler):
                    eintrag["versuche"] = eintrag.get("versuche", 0) + 1
                    eintrag["fehler"] = fehler[:200]
                save(st)
        if not fehler:
            log(f"Gespräch {e['sitzung'][:8]} gesichert in {time.time() - t0:.0f} s: {antwort.strip()[:200]}")
            continue
        log(f"Sichern von {e['sitzung'][:8]} fehlgeschlagen: {fehler[:200]}")
        if gesperrt(fehler) or eintrag is None:
            return False
        tag = datetime.date.fromisoformat(e["tag"]).strftime("%d.%m.")
        if eintrag["versuche"] == 2:
            sende_text(f"Ich konnte unser Gespräch vom {tag} zweimal nicht in mein Gedächtnis übernehmen. Ich versuche es "
                       "jede halbe Stunde weiter. Im Logbuch steht alles, verloren ist nichts.")
        if eintrag["versuche"] >= 6:
            with _lock:
                st = load()
                st["zu_sichern"] = [x for x in st.get("zu_sichern", []) if x["sitzung"] != e["sitzung"]]
                save(st)
            sende_text(f"Unser Gespräch vom {tag} bekomme ich nicht ins Gedächtnis ({fehler[:120]}). Es steht vollständig "
                       "im Logbuch, mein Wochenrückblick holt es von dort.")
        return False


# --- Zeitplan -----------------------------------------------------------------

def jobs():
    """home/jobs/<name>.md mit Kopf zwischen --- Zeilen. Wann: `zeit: HH:MM` (dazu `tage: mo,di` oder leer
    = taeglich, `monatstag: 1`, `datum: JJJJ-MM-TT` = einmal, danach wird die Datei geloescht) oder `alle: <minuten>`
    mit `fenster: HH:MM-HH:MM`. Wie: modell (standard, klein oder ein Name), effort, gespraech (chat = im Tagesgespraech, sonst eigenes),
    melden (antwort, fehler, nie), hoechstens (Nachrichten je Tag, nur fuer wiederkehrende), ausweichen (ja: am
    Abo-Limit das andere Modell nehmen, Standard nur fuer Erinnerungen), verpasst (still: am Limit ausgefallen
    ohne Meldung, der Lauf holt selbst auf), text (Erinnerung ohne Modell: genau dieser Text geht an die Person)."""
    out = {}
    for pfad in sorted(glob.glob(os.path.join(JOBS_DIR, "*.md"))):
        name = os.path.splitext(os.path.basename(pfad))[0]
        try:
            text = open(pfad, encoding="utf-8").read()
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, flags=re.S)
        if not m:
            _kaputt.add(name)
            continue
        kopf = dict(re.findall(r"^(\w+):\s*\"?(.*?)\"?\s*$", m.group(1), flags=re.M))
        if kopf.get("aktiv", "ja") != "ja":
            continue
        z = re.fullmatch(r"(\d{1,2}):(\d\d)", kopf.get("zeit", ""))
        if z:
            kopf["zeit"] = f"{int(z.group(1)):02d}:{z.group(2)}"
        if not (z or kopf.get("alle", "").isdigit()) or (kopf.get("datum") and not re.fullmatch(r"\d{4}-\d\d-\d\d", kopf["datum"])):
            _kaputt.add(name)
            continue
        _kaputt.discard(name)
        kopf["prompt"], kopf["pfad"] = m.group(2).strip(), pfad
        out[name] = kopf
    return out


def modell_von(j):
    """`modell:` eines Laufs: standard (oder leer) = Chat-Modell, klein = KLEIN_MODELL, sonst der Name selbst."""
    m = (j.get("modell") or "standard").strip().lower()
    return {"standard": CHAT_MODELL, "klein": KLEIN_MODELL}.get(m, m)


def darf_ausweichen(j):
    return (j.get("ausweichen") or ("ja" if j.get("datum") else "nein")) == "ja"


def faellig():
    t, st = jetzt(), load()
    erledigt = st.get("jobs", {})
    alle_jobs = jobs()
    for name, j in alle_jobs.items():
        if name in _eingereiht:
            continue
        bereit = bool(j.get("text")) or kontingent_frei(modell_von(j), darf_ausweichen(j))
        tage = [x.strip()[:2].lower() for x in j.get("tage", "").split(",") if x.strip()]
        if tage and WOCHENTAG[t.weekday()].lower() not in tage:
            continue
        if j.get("monatstag") and int(j["monatstag"]) != t.day:
            continue
        if j.get("datum"):   # Erinnerung: ab ihrer Zeit, eine verpasste vom Vortag ab 08:00; die Datei geht erst nach dem Senden weg
            if j["datum"] > t.date().isoformat() or (j["datum"] == t.date().isoformat() and f"{t:%H:%M}" < j["zeit"]):
                continue
            if j["datum"] < t.date().isoformat() and t.hour < 8:
                continue
            yield name, j
            continue
        if j.get("alle"):
            if not bereit:
                continue
            von, _, bis = (j.get("fenster") or "00:00-24:00").partition("-")
            letzt = st.get("intervall", {}).get(name)
            if not von <= f"{t:%H:%M}" < bis or time.time() - st.get("letzte", 0) < IM_GESPRAECH:
                continue
            if letzt is not None and time.time() - letzt < int(j["alle"]) * 60:
                continue
            with _lock:
                st = load()
                st.setdefault("intervall", {})[name] = time.time()
                save(st)
            if letzt is not None:   # beim allerersten Mal nur den Takt setzen
                yield name, j
            continue
        h, mi = map(int, j["zeit"].split(":"))
        soll = t.replace(hour=h, minute=mi, second=0, microsecond=0)
        if soll <= t < soll + datetime.timedelta(seconds=NACHHOLEN) and erledigt.get(name) != soll.date().isoformat():
            with _lock:
                st = load()
                st.setdefault("jobs", {})[name] = soll.date().isoformat()
                if not bereit:
                    st.setdefault("nachholen", {})[name] = soll.timestamp()
                save(st)
            if bereit:
                yield name, j
            else:
                log(f"Lauf {name}: Kontingent aufgebraucht, vorgemerkt zum Nachholen")
    for name, soll_ts in list(load().get("nachholen", {}).items()):   # am Limit ausgefallene Laeufe
        j = alle_jobs.get(name)
        if name in _eingereiht:
            continue
        zu_spaet = time.time() - soll_ts > NACHHOLEN_LIMIT
        if j and not zu_spaet and not kontingent_frei(modell_von(j), darf_ausweichen(j)):
            continue
        with _lock:
            st = load()
            st.get("nachholen", {}).pop(name, None)
            save(st)
        if not j:
            continue
        if zu_spaet:
            log(f"Lauf {name}: zu spät zum Nachholen, ausgefallen")
            if j.get("verpasst") != "still":
                sende_text(f"Der {LAUF_NAMEN.get(name, name)} von {zeitpunkt(soll_ts)} ist wegen des KI-Kontingents "
                           "ausgefallen und lässt sich nicht mehr sinnvoll nachholen.")
            continue
        yield name, dict(j, nachgeholt=soll_ts)


def werkzeug(befehl):
    """`vorher:`/`pruefen:` eines Laufs: nur Python-Skripte aus werkzeuge/ des Kits, fest im Code, ohne KI.
    Rueckgabe (exitcode, ausgabe)."""
    teile = shlex.split(befehl)
    if not teile or not re.fullmatch(r"werkzeuge/[\w.-]+\.py", teile[0]) or not os.path.isfile(os.path.join(HERE, teile[0])):
        return 2, f"Befehl nicht erlaubt: {befehl[:100]}"
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, teile[0]), *teile[1:]], cwd=HOME_DIR,
                           capture_output=True, text=True, timeout=LAUF_MAX)
    except subprocess.TimeoutExpired:
        return 2, f"{teile[0]} nach {LAUF_MAX // 60} Minuten abgebrochen"
    aus = (p.stdout.strip() or p.stderr.strip() or f"Exit {p.returncode}")[-1500:]
    return p.returncode, aus


def nachreichen(name, text):
    """Was ein eigener Lauf der Person geschickt hat, kommt mit ihrer nächsten Nachricht ins Gespräch."""
    with _lock:
        st = load()
        st.setdefault("nachgereicht", []).append(f"{jetzt():%H:%M} ({name}): {text.strip()}")
        save(st)


def erinnerung_weg(j):
    try:
        os.remove(j["pfad"])
    except OSError:
        pass


def erinnerung_roh(j):
    """Erinnerung, wenn das Modell nicht laeuft: der erste Absatz des Auftrags, damit sie nie verloren geht."""
    absatz = j["prompt"].strip().split("\n\n")[0].strip()
    return "Eine Erinnerung, die ich für jetzt geplant hatte (schön formulieren ging gerade nicht):\n\n" + absatz


def fuehre_job_aus(name, j, senden=True):
    if name == "tagesabschluss":   # Tagesende: Gespraech schliessen und sichern, Auftrag steht in der Datei
        if load().get("sitzung"):
            wechsel("Tagesende")
        ok = sichere_offene()
        return "" if senden else ("alles gesichert" if ok else "noch nicht alles gesichert, siehe zu_sichern in assistent.py status")
    if j.get("text"):   # Erinnerung ohne Modell
        if senden:
            sende_text(j["text"])
            notiere(f"{NAME}, von sich aus", j["text"])
            nachreichen(name, j["text"])
            if j.get("datum"):
                erinnerung_weg(j)
        return j["text"]
    prompt = f"[Zeitplan: {name}, {stempel()}]\n"
    if j.get("nachgeholt"):
        prompt += (f"[Nachgeholt: dieser Lauf war für {zeitpunkt(j['nachgeholt'])} geplant und ist am KI-Kontingent "
                   "ausgefallen. Richte dich nach der Uhrzeit von jetzt.]\n")
    if j.get("vorher"):
        code, aus = werkzeug(j["vorher"])
        log(f"Lauf {name}: vorher Exit {code}")
        prompt += f"[Vorbereitung `{j['vorher']}`, Exit {code}]\n{aus}\n"
    if j.get("gespraech") != "chat" and os.path.exists(verlauf_pfad()):
        prompt += f"(Mitschrift eures heutigen Gesprächs: {verlauf_pfad()}, ungekürzt im Logbuch {logbuch_pfad()})\n"
    prompt += j["prompt"]
    modell, effort = modell_von(j), j.get("effort") or CHAT_EFFORT
    log(f"Lauf {name} startet ({modell}, {effort})")
    t0 = time.time()
    kontext = 0
    if j.get("gespraech") == "chat":
        antwort, fehler, kontext = im_gespraech(prompt, modell, effort, tippen=False, ausweichen=darf_ausweichen(j))
    else:
        antwort, _, fehler, _ = lauf(prompt, None, modell, effort, darf_ausweichen(j))
    log(f"Lauf {name} fertig in {time.time() - t0:.0f} s" + (f", Fehler: {fehler[:200]}" if fehler else ""))
    if senden and gesperrt(fehler):
        if j.get("datum"):
            fehler = "Erinnerung ohne Modell"   # unten: Rohtext statt Fehlermeldung
        elif not j.get("alle"):
            with _lock:
                st = load()
                st.setdefault("nachholen", {})[name] = j.get("nachgeholt") or time.time()
                save(st)
            return ""
        else:
            return ""
    melden = j.get("melden", "antwort")
    pruef_text = ""
    if j.get("pruefen"):   # feste Pruefung nach dem Lauf; Exit != 0 erreicht die Person immer, auch bei melden: fehler
        code, aus = werkzeug(j["pruefen"])
        log(f"Lauf {name}: Prüfung Exit {code}: {aus[:200]}")
        if code != 0:
            pruef_text = aus
            if senden:
                sende_text(aus)
    if not senden:
        ergebnis = antwort if not fehler else f"FEHLER: {fehler}"
        return ergebnis + (f"\n\nPRÜFUNG: {pruef_text}" if pruef_text else "")
    if fehler and j.get("datum"):   # eine Erinnerung geht nie verloren
        roh = erinnerung_roh(j)
        sende_text(roh)
        notiere(f"{NAME}, von sich aus", roh)
        nachreichen(name, roh)
        erinnerung_weg(j)
        return roh
    if fehler and melden != "nie":
        sende_text(f"Mein Lauf „{LAUF_NAMEN.get(name, name)}“ ist schiefgegangen. " + fehlertext(fehler))
    elif melden == "antwort" and antwort and antwort.strip().upper() != "STILL":
        if j.get("hoechstens", "").isdigit():
            with _lock:
                st = load()
                z = st.setdefault("gesendet", {}).get(name, {})
                n = z.get("n", 0) if z.get("tag") == gespraechstag() else 0
                if n >= int(j["hoechstens"]):
                    log(f"Lauf {name}: Tageshöchstzahl erreicht, nicht gesendet")
                    return antwort
                st["gesendet"][name] = {"tag": gespraechstag(), "n": n + 1}
                save(st)
        antworte(antwort)
        notiere(NAME if j.get("gespraech") == "chat" else f"{NAME}, von sich aus", antwort)
        if j.get("gespraech") != "chat":   # ins Tagesgespraech nachreichen, damit der Assistent die Antwort einordnen kann
            nachreichen(name, antwort)
    if j.get("datum") and not fehler:
        erinnerung_weg(j)
    if kontext > GESPRAECH_MAX:
        wechsel(f"Gespräch über {kontext // 1000}k Tokens")
        sichere_offene()
    return antwort


# --- Schlange: Arbeit, die einen Neustart uebersteht ---------------------------

def einreihen(art, inhalt):
    os.makedirs(SCHLANGE, exist_ok=True)
    pfad = os.path.join(SCHLANGE, f"{time.time():.6f}-{art}.json")
    with open(pfad + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"art": art, "inhalt": inhalt}, f, ensure_ascii=False)
    os.replace(pfad + ".tmp", pfad)
    _arbeit.put((art, inhalt, pfad))


def erledigt(pfad):
    try:
        os.remove(pfad)
    except (OSError, TypeError):
        pass


def schlange_laden():
    """Beim Start: was vor einem Neustart liegen blieb, kommt wieder dran. Rueckgabe: Zahl der Nachrichten."""
    n = 0
    for pfad in sorted(glob.glob(os.path.join(SCHLANGE, "*.json"))):
        try:
            d = json.load(open(pfad, encoding="utf-8"))
        except (OSError, ValueError):
            erledigt(pfad)
            continue
        art, inhalt = d.get("art"), d.get("inhalt")
        if art == "msg":
            inhalt["neustart"] = True
            n += 1
        elif art == "job":
            _eingereiht.add(inhalt[0])
        elif art == "sichern":
            _eingereiht.add("__sichern__")
        _arbeit.put((art, inhalt, pfad))
    return n


# --- Dienst -------------------------------------------------------------------

def server_ip():
    """Öffentliche IPv4 des Servers aus den Hetzner-Metadaten, damit die Person sie ohne Hetzner-Konsole findet."""
    try:
        r = requests.get("http://169.254.169.254/hetzner/v1/metadata/public-ipv4", timeout=1)
        ip = r.text.strip() if r.ok else ""
        return ip if re.fullmatch(r"[0-9.]{7,15}", ip) else ""
    except requests.RequestException:
        return ""


# --- WhatsApp (Erweiterung, nur auf dem eigenen Server der Person) --------------
# Der Bot läuft als die Person und darf nichts als root. Er schreibt einen Auftrag nach STATE_DIR/whatsapp-auftrag,
# assistent-whatsapp.path startet box/whatsapp.sh als root, das meldet seinen Stand in WA_DIR/stand. Einwilligung und
# QR-Code laufen über die Seite /whatsapp der Einrichtungsseite (pforte.py), die den Code von der Bridge auf 127.0.0.1
# holt. Einmal bietet der Assistent WhatsApp von selbst an (wa_anbieten), danach nur noch auf /whatsapp.

WA_DIR = os.environ.get("ASSISTENT_WA_DIR") or "/var/lib/pearl-kit/whatsapp"
WA_API = os.environ.get("ASSISTENT_WA_API") or "http://127.0.0.1:8080/api"
WA_UNIT = os.environ.get("ASSISTENT_WA_UNIT") or "/etc/systemd/system/assistent-whatsapp.path"
WA_AUFTRAG = os.path.join(STATE_DIR, "whatsapp-auftrag")
WA_QR_ZEIT = 30 * 60             # so lange wartet der Server auf den Scan
WA_MAX = 100 * 60                # länger begleitet der Bot eine Einrichtung nicht
WA_TAKT = float(os.environ.get("ASSISTENT_WA_TAKT", "3"))   # so oft schaut er auf den Stand des Servers
_wa_faden = None

WA_LINK_GUELTIG = 7 * 86400     # so lange gilt der Link zur WhatsApp-Seite
WA_JA, WA_SPAETER, WA_NEIN = "WhatsApp verbinden", "Später", "Nein danke"
WA_ANGEBOT = (
    "Mir ist aufgefallen, dass dein WhatsApp noch nicht mit mir verbunden ist. Magst du das nachholen? Dann lese ich "
    "nachts deine Einzelchats mit und sage dir morgens, wem du noch antworten wolltest und was bei den Menschen los "
    "ist, die dir wichtig sind. Fragen kannst du mich auch, etwa „Was hat Lena letzte Woche geschrieben?“. Wir "
    "schreiben weiter hier in Telegram, in WhatsApp schreibe ich nie.\n\n"
    f"Tipp auf „{WA_JA}“, dann führt dich eine Seite durch. Am Ende scannst du dort einen QR-Code mit dem Handy.")
_wa_qr_cache = {}


def wa_moeglich():
    """Nur auf dem eigenen Server (ohne Betreuer, mit Einrichtungsseite) und erst, wenn der Server den Auftrag
    abholen kann."""
    return not ADMIN and bool(WEB) and os.path.exists(WA_UNIT)


def wa_status():
    try:
        return kit.lies_config()["whatsapp"]["status"]
    except (kit.ConfigFehler, KeyError):
        return "aus"


def wa_stand():
    """(stand, geändert um) aus WA_DIR/stand, ohne Datei ("", 0)."""
    pfad = os.path.join(WA_DIR, "stand")
    try:
        return open(pfad, encoding="utf-8").read().strip(), os.path.getmtime(pfad)
    except OSError:
        return "", 0


def wa_auftrag(was):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    with open(WA_AUFTRAG + ".tmp", "w", encoding="utf-8") as f:
        f.write(was + "\n")
    os.replace(WA_AUFTRAG + ".tmp", WA_AUFTRAG)   # erst jetzt sieht der Path-Unit die Datei


def wa_qr():
    """Aktueller QR-Code der Bridge als Text, sonst ""."""
    try:
        r = requests.get(f"{WA_API}/auth/qr", timeout=5)
        return (r.json().get("qr_code") or "") if r.ok else ""
    except Exception:
        return ""


def wa_qr_bild(code):
    """QR-Code als data:-Adresse (SVG) für die Seite. Pro Code nur einmal erzeugt."""
    if code not in _wa_qr_cache:
        svg = subprocess.run(["qrencode", "-t", "SVG", "-m", "2", "-o", "-", code], check=True, timeout=20,
                             capture_output=True).stdout
        _wa_qr_cache.clear()
        _wa_qr_cache[code] = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
    return _wa_qr_cache[code]


def wa_url():
    """Link zur WhatsApp-Seite. Ein noch mindestens einen Tag gültiger Link wird wiederverwendet."""
    with _lock:
        st = load()
        a = st.get("wa_link") or {}
        if not a.get("t") or a.get("bis", 0) < time.time() + 86400:
            a = {"t": secrets.token_urlsafe(24), "bis": time.time() + WA_LINK_GUELTIG}
            st["wa_link"] = a
            save(st)
    return f"{WEB}/whatsapp?t={a['t']}"


def wa_link_gueltig(t):
    a = load().get("wa_link") or {}
    return bool(t and a.get("t")) and hmac.compare_digest(str(t), a["t"]) and a.get("bis", 0) > time.time()


def wa_laeuft():
    return bool(_wa_faden and _wa_faden.is_alive())


def wa_seitenstand():
    """Für die Seite: {schritt: frage|baut|qr|verknuepft|fertig|fehler, qr, fehler}."""
    st = load()
    w = st.get("whatsapp") or {}
    if wa_laeuft() and w.get("was") == "an":
        stand, um = wa_stand()
        if um < w.get("seit", 0) - 1 or stand in ("", "baut", "aus"):
            return {"schritt": "baut"}
        if stand == "bereit":
            code = wa_qr()
            try:
                return {"schritt": "qr", "qr": wa_qr_bild(code)} if code else {"schritt": "baut"}
            except Exception as e:
                log(f"WhatsApp: QR-Bild nicht erzeugt ({type(e).__name__})")
                return {"schritt": "baut"}
        if stand.startswith("fehler"):
            return {"schritt": "fehler", "fehler": stand.partition(":")[2].strip()}
        return {"schritt": "verknuepft"}
    if wa_status() == "an":
        return {"schritt": "fertig"}
    if st.get("wa_fehler"):
        return {"schritt": "fehler", "fehler": st["wa_fehler"]}
    return {"schritt": "frage"}


def sende_ohne_tastatur(text):
    """Wie sende_text, nimmt aber die Knöpfe des Angebots unter dem Chat weg."""
    try:
        telegram("sendMessage", {"chat_id": PERSON_ID, "text": html(text), "parse_mode": "HTML",
                                 "reply_markup": {"remove_keyboard": True}})
    except TelegramFehler as e:
        if e.code != 400:
            raise
        sende_text(text)


def wa_seite_start(im_chat=True):
    """Einwilligung auf der Seite: der Server richtet die Bridge ein. Rückgabe: Fehlertext oder ''."""
    if not wa_moeglich():
        return "WhatsApp gibt es nur auf einem eigenen Server."
    if wa_laeuft():
        return ""
    a = load().get("wa_angebot") or {}
    update(whatsapp_ja=load().get("whatsapp_ja") or time.time(), whatsapp=None, wa_fehler=None,
           wa_angebot={**a, "offen": False, "ja": time.time()})
    wa_starten("an")
    if im_chat:
        sende_ohne_tastatur("Ich richte WhatsApp jetzt auf deinem Server ein. Beim ersten Mal dauert das zehn bis "
                            "fünfzehn Minuten, dann melde ich mich mit dem QR-Code.")
    return ""


def wa_onboarding(an):
    """Schritt WhatsApp der Einrichtungsseite. Ja: der Server baut sofort, der QR-Code kommt nach der Einrichtung
    im Chat. Ohne: später fragt der Assistent einmal von selbst (wa_anbieten). Rückgabe: Fehlertext oder ''."""
    if not an:
        update(wa_onboarding="ohne")
        return ""
    f = wa_seite_start(im_chat=False)
    if not f:
        update(wa_onboarding="ja")
    return f


def wa_anbieten(immer=False):
    """Einmal von selbst (tagsüber, frühestens drei Tage nach der Begrüßung), ohne Antwort nach einer Woche ein
    zweites Mal. Mit immer=True sofort, etwa auf /whatsapp."""
    st = load()
    a = st.get("wa_angebot") or {}
    if not immer:
        if not (wa_moeglich() and eingerichtet() and KI_ANBIETER) or wa_status() == "an" or wa_laeuft() \
                or st.get("whatsapp") or a.get("nein") or a.get("ja") or a.get("anzahl", 0) >= 2 \
                or time.time() < a.get("wieder", 0) or time.time() - st.get("begruesst", 0) < 3 * 86400 \
                or not 10 <= jetzt().hour < 19:
            return False
    update(wa_angebot={**a, "offen": True, "anzahl": a.get("anzahl", 0) + (0 if immer else 1),
                       "wieder": time.time() + 7 * 86400})
    url = wa_url()
    tastatur = {"keyboard": [[{"text": WA_JA, "web_app": {"url": url}}], [{"text": WA_SPAETER}, {"text": WA_NEIN}]],
                "resize_keyboard": True, "one_time_keyboard": True}
    try:
        telegram("sendMessage", {"chat_id": PERSON_ID, "text": html(WA_ANGEBOT), "parse_mode": "HTML",
                                 "reply_markup": tastatur})
    except TelegramFehler as e:
        if e.code != 400:
            raise
        log(f"Angebot mit Web-App-Knopf abgelehnt ({e})")
        sende_knopf(WA_ANGEBOT, WA_JA, url)
    return True


def wa_antwort(text):
    """„Später“ oder „Nein danke“ auf das Angebot. Rückgabe: war es eine solche Antwort?"""
    a = load().get("wa_angebot") or {}
    if not a.get("offen") or text not in (WA_SPAETER, WA_NEIN):
        return False
    if text == WA_SPAETER:
        update(wa_angebot={**a, "offen": False, "wieder": time.time() + 7 * 86400})
        sende_ohne_tastatur("Gut, ich frage in einer Woche noch einmal. Mit /whatsapp geht es jederzeit.")
    else:
        update(wa_angebot={**a, "offen": False, "nein": time.time()})
        sende_ohne_tastatur("Alles klar, ich frage nicht mehr. Überlegst du es dir anders, schick mir /whatsapp.")
    return True


def wa_werkzeug(*args):
    p = subprocess.run([sys.executable, os.path.join(HERE, "werkzeuge", "whatsapp.py"), *args], cwd=HOME_DIR,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout.strip() or p.stderr.strip())[-500:]


def wa_begleiten(was, seit):
    """Läuft in einem eigenen Faden, bis der Server fertig ist. Übersteht einen Neustart über state["whatsapp"].
    Den QR-Code zeigt die Seite, im Chat kommen nur die Meldungen."""
    qr_seit, gemeldet = 0.0, set()

    def ende(fehler=None):
        with _lock:
            st = load()
            st.pop("whatsapp", None)
            if fehler:
                st["wa_fehler"] = fehler
            save(st)

    def fehlschlag(text):
        ende(text)
        sende_knopf(text, "Noch einmal versuchen", wa_url())

    while time.time() - seit < WA_MAX:
        time.sleep(WA_TAKT)
        if os.path.exists(WA_AUFTRAG) and time.time() - seit > 90:
            os.remove(WA_AUFTRAG)
            sende_text("Auf deinem Server fehlt dafür noch ein Teil. Er kommt mit dem nächsten Update, spätestens in "
                       "einer Stunde. Versuch es danach noch einmal mit /whatsapp.")
            return ende("Auf deinem Server fehlte noch ein Teil. Er ist jetzt da, versuch es noch einmal.")
        stand, um = wa_stand()
        if um < seit - 1:   # Stand von einem früheren Auftrag
            continue
        if stand.startswith("fehler"):
            return fehlschlag(stand.partition(":")[2].strip() or "Die Einrichtung ist schiefgegangen.")
        if was == "aus":
            if stand == "aus":
                sende_text("WhatsApp ist getrennt, auf dem Server ist davon nichts mehr. Deine Notizen zu den Menschen "
                           "bleiben. Am Handy kannst du unter Verknüpfte Geräte den Eintrag noch löschen.")
                return ende()
            continue
        if stand == "baut" and "baut" not in gemeldet and time.time() - seit > 20 * 60:
            gemeldet.add("baut")
            sende_text("Der Server baut noch, das dauert beim ersten Mal etwas. Ich melde mich mit dem QR-Code.")
        elif stand == "bereit":
            if not eingerichtet():   # aus dem Onboarding: der Code kommt, wenn die Seite durch ist
                continue
            if not qr_seit:
                qr_seit = time.time()
                sende_knopf("Der QR-Code ist da. Öffne ihn am besten am Laptop, scannen musst du mit dem Handy.",
                            "QR-Code zeigen", wa_url())
            if time.time() - qr_seit > WA_QR_ZEIT:
                return fehlschlag("Der QR-Code ist abgelaufen, gescannt hat ihn niemand.")
        elif stand == "verknuepft" and "verknuepft" not in gemeldet:
            gemeldet.add("verknuepft")
            sende_text("Verbunden. Jetzt kommen deine Kontakte an, das dauert zwei, drei Minuten.")
        elif stand == "fertig":
            if not eingerichtet():
                continue
            code, aus = wa_werkzeug("einrichten")
            log(f"WhatsApp einrichten: Exit {code}: {aus[:200]}")
            if code:
                return fehlschlag("WhatsApp ist verbunden, aber bei mir hakt noch etwas.")
            sende_text("WhatsApp ist eingerichtet. Gleich frage ich dich, wer dir nah ist, ab heute Nacht halte ich "
                       "fest, was sich bei diesen Menschen tut. /whatsapp zeigt dir den Stand, /whatsapp_aus "
                       "trennt alles wieder.")
            return ende()
    fehlschlag("Die Einrichtung hat zu lange gedauert.")


def wa_starten(was, seit=None):
    global _wa_faden
    if _wa_faden and _wa_faden.is_alive():
        return False
    seit = seit or time.time()
    if not load().get("whatsapp"):
        wa_auftrag(was)
        update(whatsapp={"was": was, "seit": seit})
    _wa_faden = threading.Thread(target=wa_begleiten, args=(was, seit), daemon=True)
    _wa_faden.start()
    return True


def wa_fortsetzen():
    """Beim Start: eine Einrichtung, die vor dem Neustart lief, weiter begleiten."""
    w = load().get("whatsapp")
    if w and time.time() - w.get("seit", 0) < WA_MAX:
        wa_starten(w["was"], w["seit"])
    elif w:
        update(whatsapp=None)


def wa_befehl(cmd):
    if not wa_moeglich():
        return sende_text("WhatsApp gibt es nur auf einem eigenen Server. Hier geht es leider nicht.")
    laeuft = wa_laeuft()
    stand, _ = wa_stand()
    if cmd == "/whatsapp":
        if laeuft and (load().get("whatsapp") or {}).get("was") == "an":
            return sende_knopf("Ich bin gerade dabei, WhatsApp einzurichten. Hier siehst du, wie weit ich bin.",
                               "Stand zeigen", wa_url())
        if laeuft:
            return sende_text("Ich trenne gerade WhatsApp, ich melde mich.")
        if wa_status() != "an":
            return wa_anbieten(immer=True)
        code, aus = wa_werkzeug("status")
        try:
            s = json.loads(aus)
            zeile = f"gelesen bis {s['lesestand_lesbar'][-11:]}" + \
                (f", {len(s['warteschlange'])} Chats folgen nächste Nacht" if s.get("warteschlange") else "")
        except (ValueError, KeyError):
            zeile = "noch kein Lauf"
        verbunden = "verbunden" if stand == "fertig" else f"Server meldet „{stand or 'nichts'}“"
        return sende_text(f"WhatsApp: {verbunden}, {zeile}. Hakt etwas, hilft /whatsapp_neu. /whatsapp_aus trennt "
                          "alles wieder, deine Notizen bleiben.")
    if cmd in ("/whatsapp_ja", "/whatsapp_neu"):
        if laeuft:
            return sende_text("Läuft schon, ich melde mich.")
        if not load().get("whatsapp_ja"):   # eingewilligt wird auf der Seite
            return wa_anbieten(immer=True)
        update(whatsapp=None, wa_fehler=None)
        wa_starten("an")
        return sende_knopf("Ich richte WhatsApp noch einmal ein. Das dauert ein paar Minuten, hier siehst du den Stand.",
                           "Stand zeigen", wa_url())
    if cmd == "/whatsapp_aus":
        if laeuft:
            return sende_text("Warte kurz, bis die laufende Einrichtung durch ist, dann geht das.")
        code, aus = wa_werkzeug("abschalten")
        log(f"WhatsApp abschalten: Exit {code}: {aus[:200]}")
        update(whatsapp=None, wa_fehler=None)
        wa_starten("aus")
        return sende_text("Ich trenne WhatsApp und räume den Server auf. Das dauert eine Minute.")


def befehl(text):
    cmd = text.split()[0].lower().split("@")[0]
    if cmd.startswith("/whatsapp"):
        return wa_befehl(cmd)
    if cmd == "/neu":
        einreihen("wechsel", f"{PERSON} hat /neu geschickt")
        sende_text("Neues Gespräch. Was wir besprochen haben, halte ich vorher noch fest.")
    elif cmd == "/google" and WEB and GOOGLE_PORT:
        einrichten_hinweis(immer=True, wo="google")
    elif cmd == "/google":
        google_anmelden()
    elif cmd == "/ki" and WEB and eingerichtet():
        ki_uebersicht()
    elif cmd.startswith("/ki_") and eingerichtet():
        name = cmd[4:]
        if name == KI_ANBIETER:
            sende_text(f"Ich bin schon mit {KI_NAME} verbunden.")
        elif (f := ki_wechseln(name)):
            sende_text(f + " Tipp auf /ki, dann kannst du sie verbinden.")
        elif load().get("sitzung"):
            sende_text(f"Ich halte noch fest, was wir besprochen haben, dann wechsle ich zu {anbieter.name(name)}.")
    elif cmd in ("/anmelden", "/ki"):
        einrichten_hinweis(immer=True, wo="ki" if eingerichtet() else "")
    elif cmd in ("/start", "/einrichten") and WEB and not eingerichtet():
        einrichten_hinweis(immer=True)
    elif cmd == "/start" and not KI_ANBIETER:
        if not load().get("begruesst"):   # dann begrüßt anmeldung_fertig nicht noch einmal
            update(begruesst=time.time())
            sende_text(f"Hallo {PERSON}, ich bin {NAME}. " + BEGRUESSUNG)
        einrichten_hinweis(immer=True)
    elif cmd == "/start" and not load().get("begruesst"):
        update(begruesst=time.time())
        sende_text(f"Hallo {PERSON}, ich bin {NAME}. " + BEGRUESSUNG)
    elif cmd == "/status":
        st = load()
        aktiv = st.get("sitzung") and st.get("tag") == gespraechstag()
        seit = datetime.datetime.fromtimestamp(st.get("seit", 0), TZ).strftime("%d.%m. %H:%M")
        auch = [anbieter.name(k) for k in verbundene() if k != KI_ANBIETER]
        zeilen = [f"KI: {KI_NAME if KI_ANBIETER else 'noch nicht angemeldet'}"
                  + (f" (auch verbunden: {', '.join(auch)}, wechseln mit /ki)" if auch and KI_ANBIETER else ""),
                  f"Gespräch: {f'seit {seit}, {st.get('kontext', 0) // 1000}k Tokens' if aktiv else 'noch keins offen'}",
                  f"Warteschlange: {_arbeit.qsize()}"]
        if (ip := server_ip()):
            zeilen.append(f"Server-IP: {ip}")
        if wa_moeglich() and wa_status() == "an":
            zeilen.append(f"WhatsApp: {'verbunden' if wa_stand()[0] == 'fertig' else wa_stand()[0] or 'unklar'}, mehr mit /whatsapp")
        for m, bis in st.get("gesperrt", {}).items():
            if bis > time.time():
                zeilen.append(f"Kontingent {m.capitalize()}: aufgebraucht bis {zeitpunkt(bis)}")
        if st.get("zu_sichern"):
            zeilen.append(f"Noch zu sichern: {len(st['zu_sichern'])} Gespräch(e)")
        if st.get("nachholen"):
            zeilen.append("Hole nach: " + ", ".join(LAUF_NAMEN.get(n, n) for n in st["nachholen"]))
        for name, j in jobs().items():
            if j.get("alle"):
                zeilen.append(f"{name}: alle {j['alle']} min, {j.get('fenster') or 'rund um die Uhr'}")
            else:
                wann = j.get("datum") or j.get("tage") or "täglich"
                zeilen.append(f"{name}: {wann} {j['zeit']}, zuletzt {st.get('jobs', {}).get(name, 'nie')}")
        sende_text("\n".join(zeilen))
    else:
        sende_text(HILFE + ("\n/whatsapp  WhatsApp verbinden" if wa_moeglich() else ""))


BEGRUESSUNG = ("Ich helfe dir im Alltag: Termine, Erinnerungen, Fristen, Ideen, alles, was sonst untergeht. "
               "Schreib mir einfach, auch Sprachnachrichten und Fotos. Was du mir erzählst, merke ich mir. In den "
               "ersten Tagen frage ich dich morgens ab und zu etwas, damit ich dich kennenlerne.")


def zwischenstand(taetigkeit_text):
    sende_text(f"Dauert einen Moment länger, ich bin gerade dabei, {taetigkeit_text}.")


def arbeiter():
    """Ein Faden fuer alles, was die KI braucht: Chat-Nachrichten gebuendelt, Laeufe einzeln. Solange keine KI
    angemeldet ist, bleibt die Arbeit in der Schlange liegen."""
    while True:
        while not (KI_ANBIETER and eingerichtet()):
            time.sleep(2)
        art, inhalt, pfad = _arbeit.get()
        pfade = [pfad]
        try:
            if art == "job":
                name, j = inhalt
                puls(arbeit={"was": LAUF_NAMEN.get(name, name), "seit": time.time()})
                try:
                    fuehre_job_aus(name, j)
                finally:
                    _eingereiht.discard(name)
                continue
            if art == "sichern":
                try:
                    sichere_offene()
                finally:
                    _eingereiht.discard("__sichern__")
                continue
            if art == "wechsel":
                wechsel(inhalt)
                sichere_offene()
                continue
            if art == "ki":   # erst mit der bisherigen KI sichern, dann wechseln
                if inhalt != KI_ANBIETER and anbieter.angemeldet(inhalt):
                    wechsel(f"Wechsel zu {anbieter.name(inhalt)}")
                    sichere_offene()
                    anmeldung_fertig(inhalt)
                continue
            nachrichten = [inhalt]
            time.sleep(BUENDELN)
            while True:
                try:
                    a, i, p = _arbeit.get_nowait()
                except queue.Empty:
                    break
                if a == "msg":
                    nachrichten.append(i)
                    pfade.append(p)
                else:
                    _arbeit.put((a, i, p))
                    break
            puls(arbeit={"was": f"Antwort an {PERSON}", "seit": time.time()})
            for n in nachrichten:
                if not n.get("neustart"):
                    notiere(PERSON, n["t"])
            nach = load().get("nachgereicht", [])
            prompt = "".join(f"[Das hast du {PERSON} zwischendurch von dir aus geschrieben, {n}]\n" for n in nach)
            if any(n.get("neustart") for n in nachrichten):
                prompt += ("[Ich wurde neu gestartet, während du an dieser Nachricht gearbeitet hast. Prüf kurz, ob du "
                           "schon etwas davon erledigt hast, bevor du es doppelt machst.]\n")
            prompt += "\n".join(f"[Telegram, {stempel(datetime.datetime.fromtimestamp(n['d'], TZ))}] {PERSON}: {n['t']}"
                                for n in nachrichten)
            t0 = time.time()
            antwort, fehler, kontext = im_gespraech(prompt, zwischen=zwischenstand)
            while gesperrt(fehler):   # kein Modell frei: Nachrichten bleiben liegen, die Person weiß Bescheid
                warte_auf_kontingent()
                antwort, fehler, kontext = im_gespraech(prompt, zwischen=zwischenstand)
            log(f"Antwort nach {time.time() - t0:.0f} s, {len(antwort)} Zeichen, {kontext // 1000}k Tokens"
                + (", Fehler" if fehler else ""))
            if fehler:
                log(f"Fehler: {fehler[:300]}")
                notiere("System", f"Fehler: {fehler[:300]}")
                sende_text(fehlertext(fehler))
                continue
            with _lock:
                st = load()
                st["nachgereicht"] = st.get("nachgereicht", [])[len(nach):]
                save(st)
            if antwort and antwort.strip().upper() != "STILL":
                antworte(antwort)
                notiere(NAME, antwort)
            if kontext > GESPRAECH_MAX:   # gleich sichern, solange der Cache warm ist
                wechsel(f"Gespräch über {kontext // 1000}k Tokens")
                sichere_offene()
        except Exception as e:
            log(f"Fehler im Arbeiter: {type(e).__name__}: {e}")
            try:
                sende_text("Da ist bei mir etwas schiefgegangen, versuch es bitte noch einmal.")
            except Exception:
                pass
        finally:
            for p in pfade:
                erledigt(p)
            puls(arbeit=None)


def sichern(melden=True):
    """Jede Nacht ein Git-Stand der Instanz (home/, config, settings). Ist ein Remote `origin` eingetragen,
    geht der Stand auch dorthin (Einrichtung: box/sicherung.md). Rückgabe: hat es geklappt?"""
    git = ["git", "-C", INSTANZ]
    try:
        if not os.path.isdir(os.path.join(INSTANZ, ".git")):
            subprocess.run(git + ["init", "-q", "-b", "main"], capture_output=True, timeout=60)
        subprocess.run(git + ["add", "-A"], capture_output=True, timeout=120)
        subprocess.run(git + ["-c", f"user.name={NAME}", "-c", "user.email=assistent@localhost", "commit", "-qm",
                              f"sicherung: {jetzt():%Y-%m-%d}"], capture_output=True, timeout=120)
        remote = subprocess.run(git + ["remote", "get-url", "origin"], capture_output=True, timeout=30).returncode == 0
        if not remote:
            ok, grund = True, ""
        else:
            r = subprocess.run(git + ["push", "-q", "origin", "HEAD:main"], capture_output=True, text=True, timeout=300)
            ok, grund = r.returncode == 0, r.stderr.strip()[-200:]
    except (subprocess.TimeoutExpired, OSError) as e:
        ok, grund = False, type(e).__name__
    log("Sicherung: " + ("ok" if ok else f"fehlgeschlagen ({grund})"))
    if not ok and melden:
        log("Sicherung fehlgeschlagen, die Wache meldet das")
    return ok


def kaputt_melden():
    """Dateien in jobs/ mit unlesbarem Kopf laufen nie, die Person soll das wissen (einmal je Datei)."""
    jobs()
    with _lock:
        st = load()
        neu = sorted(_kaputt - set(st.get("kaputt_gemeldet", [])))
        st["kaputt_gemeldet"] = sorted(_kaputt)
        save(st)
    if neu:
        sende_text("Diese Zeitpläne kann ich nicht lesen, sie laufen deshalb nie: " + ", ".join(f"jobs/{n}.md" for n in neu)
                   + ". Schreib mir, was dort passieren soll, dann lege ich sie neu an.")


def zeitplan():
    runde = 0
    while True:
        try:
            fertig = liegen_geblieben()
            for name, j in (faellig() if KI_ANBIETER and fertig else ()):
                _eingereiht.add(name)
                einreihen("job", [name, j])
            t, st = jetzt(), load()
            heute = t.date().isoformat()
            if (t.hour, t.minute) >= (TAGESWECHSEL, 15) and st.get("gesichert") != heute \
                    and time.time() >= st.get("sicherung_wieder", 0):
                if sichern(melden=st.get("sicherung_gemeldet") != heute):
                    update(gesichert=heute)
                else:
                    update(sicherung_wieder=time.time() + 3600, sicherung_gemeldet=heute)
            if runde % 60 == 0 and st.get("zu_sichern") and "__sichern__" not in _eingereiht:
                _eingereiht.add("__sichern__")
                einreihen("sichern", None)
            if runde % 10 == 0:
                wa_anbieten()
            if runde % 120 == 0:
                aufraeumen()
                if t.hour >= 8:
                    kaputt_melden()
        except Exception as e:
            log(f"Fehler im Zeitplan: {type(e).__name__}: {e}")
        runde += 1
        time.sleep(30)


# --- Google-Anmeldung über den Chat --------------------------------------------
# Mit Einrichtungsseite ([web] adresse) leitet Google nach der Freigabe dorthin um, die Seite gibt den Rückweg an den
# Google-Dienst weiter (google_web). Ohne leitet Google auf http://localhost:<port>/oauth2callback?... um. Auf dem
# Handy lädt die Seite nicht, die Person schickt die Adresse an den Bot, der reicht sie an den Google-Dienst weiter.

GOOGLE_ANLEITUNG_WEB = (
    "Hier ist dein Link: [Bei Google anmelden]({url})\n\n"
    "1. Mit deinem Google-Konto anmelden und alles freigeben. Kommt „Google hat diese App nicht überprüft“, "
    "tippst du auf „Erweitert“ und dann auf „Weiter“\n"
    "2. Am Ende siehst du „Fertig“, dann bin ich verbunden\n\n"
    "Klappt die Anmeldung im Telegram-Fenster nicht, halt den Link gedrückt, kopier ihn und öffne ihn in Safari "
    "oder Chrome.")
GOOGLE_ANLEITUNG = (
    "Hier ist dein Link: [Bei Google anmelden]({url})\n\n"
    "1. Link gedrückt halten, kopieren und in Safari oder Chrome einfügen. Im Telegram-Fenster klappt die "
    "Anmeldung oft nicht\n"
    "2. Mit deinem Google-Konto anmelden und alles freigeben. Kommt „Google hat diese App nicht überprüft“, "
    "tippst du auf „Erweitert“ und dann auf „Weiter“\n"
    "3. Am Ende lädt eine Seite mit „localhost“ nicht, das ist richtig. Kopier ihre ganze Adresse oben aus der "
    "Leiste und schick sie mir")


def mcp_antwort(r):
    """Antwort des Google-Dienstes, als JSON oder als Event-Stream."""
    if "text/event-stream" in r.headers.get("content-type", ""):
        for z in r.text.splitlines():
            if z.startswith("data:"):
                try:
                    d = json.loads(z[5:])
                except ValueError:
                    continue
                if "result" in d or "error" in d:
                    return d
        return {}
    return r.json()


def google_link():
    """Anmelde-Link direkt beim Google-Dienst holen (MCP über HTTP), damit die KI die lange Adresse nicht abschreibt."""
    url = f"http://127.0.0.1:{GOOGLE_PORT}/mcp"
    kopf = {"Authorization": f"Bearer {secret('GOOGLE_MCP_TOKEN')}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}

    def rpc(nachricht):
        r = requests.post(url, json=nachricht, headers=kopf, timeout=30)
        r.raise_for_status()
        return r

    r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "assistent", "version": "1"}}})
    if r.headers.get("mcp-session-id"):
        kopf["mcp-session-id"] = r.headers["mcp-session-id"]
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    d = mcp_antwort(rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "start_google_auth",
                                    "arguments": {"service_name": "Google", "user_google_email": GOOGLE_KONTO}}}))
    text = " ".join(c.get("text", "") for c in (d.get("result") or {}).get("content", []) if isinstance(c, dict))
    m = re.search(r"https://accounts\.google\.com/[^\s)\]>\"'`]+", text)
    return m.group(0).rstrip(".,;") if m else None


def google_anmelden():
    """/google: Link direkt vom Google-Dienst holen und mit Anleitung schicken. Klappt das nicht, macht es die KI."""
    if not GOOGLE_KONTO:
        return sende_text("Google ist bei mir nicht eingerichtet.")
    try:
        link = google_link()
    except (requests.RequestException, ValueError) as e:
        link = None
        log(f"Google-Link nicht direkt geholt ({type(e).__name__})")
    if link:
        return sende_text((GOOGLE_ANLEITUNG_WEB if WEB else GOOGLE_ANLEITUNG).format(url=link))
    if WEB:
        return sende_text("Ich komme gerade nicht an den Google-Link. Versuch /google in ein paar Minuten noch einmal.")
    einreihen("msg", {"d": time.time(), "t": (
        f"/google – Ich möchte mich bei Google anmelden. Ruf `start_google_auth` mit user_google_email "
        f"{GOOGLE_KONTO} auf und schick mir den Link mit dieser Anleitung: Link kopieren und in Safari oder Chrome "
        "öffnen, mit dem Google-Konto anmelden und alles freigeben. Kommt die Warnung, dass Google die App nicht geprüft hat, auf „Erweitert“ "
        "und „Weiter“ tippen. Am Ende lädt eine Seite mit „localhost“ nicht, das ist richtig. Ihre komplette "
        "Adresse kopieren und dir schicken.")})


def google_rueckruf(text):
    """Adresse mit localhost:<port>/oauth2callback aus dem Chat an den Google-Dienst geben. Rückgabe: war es eine?"""
    m = GOOGLE_RUECKRUF.search(text or "")
    if not m:
        return False
    if not GOOGLE_PORT or int(m.group(1)) != GOOGLE_PORT:
        sende_text("Diese Anmelde-Adresse gehört nicht zu mir. Schick /google, dann bekommst du einen neuen Link.")
        return True
    try:
        r = requests.get(f"http://127.0.0.1:{GOOGLE_PORT}/oauth2callback?{m.group(2)}", timeout=60,
                         headers={"Authorization": f"Bearer {secret('GOOGLE_MCP_TOKEN')}"})
        ok = r.ok
        log(f"Google-Anmeldung: HTTP {r.status_code}")
    except requests.RequestException as e:
        ok = False
        log(f"Google-Anmeldung: Dienst nicht erreicht ({type(e).__name__})")
    sende_text("Google ist verbunden, ab jetzt sehe ich deinen Kalender und deine Mails." if ok else
               "Das hat nicht geklappt, der Link war vielleicht schon alt. Schick /google, dann versuchen wir es neu.")
    if ok:
        notiere("System", "Google-Anmeldung erfolgreich")
    return True


def handle(u):
    m = u.get("message")
    if not m:
        return
    frm, chat = m.get("from") or {}, m.get("chat") or {}
    text = (m.get("text") or "").strip()
    if chat.get("type") == "private" and not PERSON_ID and frm.get("id"):
        return binden(frm, text)
    if chat.get("type") != "private" or frm.get("id") != PERSON_ID:
        log(f"Fremdes Konto {frm.get('id')} ignoriert")
        return
    if text.startswith("/") and text.split()[0].lower().split("@")[0] in ("/neu", "/status", "/start", "/hilfe", "/help", "/google",
                                                                       "/anmelden", "/einrichten", "/ki",
                                                                       "/whatsapp", "/whatsapp_ja", "/whatsapp_neu",
                                                                       "/whatsapp_aus") or \
            re.fullmatch(r"/ki_[a-z]+", text.split()[0].lower().split("@")[0]):
        return befehl(text)
    if google_rueckruf(text) or wa_antwort(text):
        return
    try:
        inhalt = als_text(m)
    except Exception as e:
        log(f"Nachricht nicht lesbar: {type(e).__name__}: {e}")
        sende_text("Deine letzte Nachricht kam bei mir nicht richtig an (Foto, Datei oder Sprachnachricht). "
                   "Schick sie bitte noch einmal.")
        return
    log(f"Nachricht ({len(inhalt)} Zeichen)")
    if inhalt:
        einreihen("msg", {"d": m.get("date") or time.time(), "t": inhalt})
        if not KI_ANBIETER or not eingerichtet():
            einrichten_hinweis()


def dienst():
    global _puls_zuletzt
    log(f"{NAME} startet ({PERSON})")
    while not secret("TG_TOKEN"):
        log("Kein TG_TOKEN, ich schaue jede Minute nach")
        time.sleep(60)
    try:
        global BOT
        me = telegram("getMe")["result"]
        BOT = me.get("username") or ""
        log(f"Bot @{BOT}" + ("" if PERSON_ID else f", wartet auf die Einladung {einladungslink()}"))
        befehle = [{"command": "status", "description": "Was gerade läuft"}]
        if GOOGLE_KONTO or (WEB and GOOGLE_PORT):
            befehle.append({"command": "google", "description": "Kalender und Mail verbinden"})
        if WEB:
            befehle.append({"command": "ki", "description": "KI wechseln oder eine weitere verbinden"})
        if wa_moeglich():
            befehle.append({"command": "whatsapp", "description": "WhatsApp verbinden"})
        befehle.append({"command": "neu", "description": "Neues Gespräch (brauchst du nicht)"})
        telegram("setMyCommands", {"commands": befehle})
        if WEB:
            telegram("setMyDescription", {"description": "Dein persönlicher Assistent. Tipp auf Starten, dann richtest "
                                                          "du ihn in drei Minuten ein."})
        if NAME and me.get("first_name") != NAME:
            telegram("setMyName", {"name": NAME})
    except Exception as e:
        log(f"Telegram beim Start nicht erreicht ({e})")
    pforte_starten()
    if not KI_ANBIETER or not eingerichtet():
        log("Noch nicht eingerichtet, Nachrichten warten auf die Einrichtungsseite")
    jobs()
    liegen = schlange_laden()
    puls(arbeit=None)
    if liegen:
        log(f"{liegen} Nachricht(en) aus der Zeit vor dem Neustart")
        try:
            sende_text("Ich war kurz weg und mache jetzt mit deiner Nachricht weiter.")
        except Exception:
            pass
    threading.Thread(target=arbeiter, daemon=True).start()
    threading.Thread(target=zeitplan, daemon=True).start()
    try:
        wa_fortsetzen()
    except Exception as e:
        log(f"WhatsApp-Einrichtung nicht fortgesetzt ({e})")
    try:
        fragebogen_anbieten()
    except Exception as e:
        log(f"Fragebogen nicht angeboten ({e})")
    while True:
        try:
            res = telegram("getUpdates", {"timeout": 50, "offset": load().get("offset", 0),
                                          "allowed_updates": ["message"]}, timeout=65)
        except TelegramFehler as e:
            log(f"Telegram lehnt ab ({e})" + (", holt noch ein zweiter Dienst mit demselben Bot ab?" if e.code == 409 else ""))
            time.sleep(300 if e.code in (401, 404) else 60 if e.code == 409 else 15)
            continue
        except Exception as e:
            log(f"Telegram nicht erreicht ({type(e).__name__})")
            time.sleep(10)
            continue
        if time.time() - _puls_zuletzt > 60:
            _puls_zuletzt = time.time()
            puls(telegram=_puls_zuletzt)
        for u in res.get("result", []):
            update(offset=u["update_id"] + 1)
            try:
                handle(u)
            except Exception as e:
                log(f"Fehler bei einer Nachricht: {type(e).__name__}: {e}")


def pulsbefund(st=None):
    """Was die Wache melden soll: Liste von {schluessel, stufe (1 Warnung, 2 kritisch), text}."""
    st, jetzt_ts, out = st or load(), time.time(), []
    p = st.get("puls", {})
    if p.get("telegram") and jetzt_ts - p["telegram"] > 15 * 60:
        out.append({"schluessel": "telegram", "stufe": 2,
                    "text": f"erreicht Telegram seit {int((jetzt_ts - p['telegram']) // 60)} Minuten nicht"})
    a = p.get("arbeit")
    if a and jetzt_ts - a.get("seit", jetzt_ts) > LAUF_MAX + 10 * 60:
        out.append({"schluessel": "haengt", "stufe": 2,
                    "text": f"hängt seit {int((jetzt_ts - a['seit']) // 60)} Minuten an „{a.get('was')}“"})
    alle_gesperrt = all(st.get("gesperrt", {}).get(m, 0) > jetzt_ts for m in MODELLE)
    msgs = glob.glob(os.path.join(SCHLANGE, "*-msg.json"))
    if not KI_ANBIETER or not eingerichtet():
        if msgs and jetzt_ts - min(os.path.getmtime(f) for f in msgs) > 24 * 3600:
            out.append({"schluessel": "nicht-angemeldet", "stufe": 1,
                        "text": "ist seit über einem Tag nicht eingerichtet, Nachrichten warten"})
    elif msgs and not alle_gesperrt:
        alt = jetzt_ts - min(os.path.getmtime(f) for f in msgs)
        if alt > 30 * 60:
            out.append({"schluessel": "wartet", "stufe": 2,
                        "text": f"eine Nachricht wartet seit {int(alt // 60)} Minuten auf Antwort"})
    if KI_ANBIETER and alle_gesperrt:
        bis = min(st["gesperrt"][m] for m in MODELLE)
        out.append({"schluessel": "kontingent", "stufe": 1,
                    "text": f"{KI_NAME}-Kontingent aufgebraucht bis {zeitpunkt(bis)}, holt danach nach"})
    if p.get("anmeldung") and jetzt_ts - p["anmeldung"] < 6 * 3600:
        out.append({"schluessel": "anmeldung", "stufe": 2, "text": f"{KI_NAME} lehnt die Anmeldung ab: " + (
            "die Person hat den Link zur Einrichtungsseite bekommen, sonst /anmelden" if WEB else
            "neu anmelden mit `person-anlegen.sh` oder `claude setup-token` (box/README)")})
    for e in st.get("zu_sichern", []):
        if e.get("versuche", 0) >= 2:
            out.append({"schluessel": "sichern", "stufe": 1,
                        "text": f"konnte das Gespräch vom {e['tag']} {e['versuche']}-mal nicht ins Gedächtnis übernehmen"})
            break
    t = jetzt()
    seit_anlage = datetime.date.fromtimestamp(os.path.getmtime(kit.CONFIG)) if os.path.exists(kit.CONFIG) else t.date()
    if t.hour >= 7 and st.get("gesichert") != t.date().isoformat() and seit_anlage < t.date():
        out.append({"schluessel": "sicherung", "stufe": 1, "text": "nächtliche Sicherung ist heute noch nicht gelungen"})
    seit = secret("CLAUDE_TOKEN_SEIT") if KI_ANBIETER == "claude" else ""
    if seit and re.fullmatch(r"\d{4}-\d\d-\d\d", seit):
        tage = (t.date() - datetime.date.fromisoformat(seit)).days
        if tage >= 330:
            out.append({"schluessel": "token-alter", "stufe": 1,
                        "text": f"Claude-Token ist {tage} Tage alt und läuft nach etwa einem Jahr ab, bald erneuern"})
    return out


def pruefen():
    """Selbsttest nach dem Einrichten: Rückgabe Liste von (ok, text). Ohne angemeldete KI ist das kein Fehler,
    solange die Einrichtungsseite eingerichtet ist: die Person meldet sich dort selbst an."""
    out = [(True, f"Config: {PERSON or 'Name kommt bei der Einrichtung'}, Assistent {NAME or 'Name kommt bei der Einrichtung'}, "
                  f"KI {KI_NAME if KI_ANBIETER else 'noch nicht angemeldet'}"
                  + (f", Modell {CHAT_MODELL}/{KLEIN_MODELL}, Rechte {RECHTE}" if KI_ANBIETER else ""))]
    out.append((bool(secret("TG_TOKEN")), "TG_TOKEN vorhanden" if secret("TG_TOKEN") else "TG_TOKEN fehlt"))
    try:
        me = telegram("getMe")["result"]
        out.append((True, f"Telegram: @{me.get('username')}"))
        if not PERSON_ID:
            out.append((bool(WEB), f"Telegram-Konto: wartet auf die Einladung {einladungslink()}" if WEB else
                        "Telegram-Konto: keine ID und keine Einrichtungsseite, person-anlegen.sh noch einmal laufen lassen"))
    except Exception as e:
        out.append((False, f"Telegram: {e}"))
    if not KI_ANBIETER:
        out.append((bool(WEB), "KI: meldet die Person selbst auf der Einrichtungsseite an" if WEB else
                    "KI: keine angemeldet und keine Einrichtungsseite, person-anlegen.sh noch einmal laufen lassen"))
    elif not anbieter.angemeldet(KI_ANBIETER):
        out.append((False, f"{KI_NAME}: kein Zugang gespeichert" + (", die Person meldet sich über /anmelden an" if WEB else "")))
    else:
        if KI_ANBIETER == "claude" and secret("CLAUDE_TOKEN_SEIT"):
            out.append((True, f"Claude: Langzeit-Token vom {secret('CLAUDE_TOKEN_SEIT')}"))
        antwort, _, fehler, _ = ki_lauf("Antworte nur mit dem Wort OK.", modell=KLEIN_MODELL, effort="low")
        out.append((not fehler and "OK" in antwort.upper(), f"{KI_NAME}-Lauf: {fehler or antwort[:60]}"))
    if WEB_PORT:
        try:
            r = requests.get(f"http://127.0.0.1:{WEB_PORT}/einrichten?t=pruefen", timeout=5)
            out.append((r.status_code == 403, f"Einrichtungsseite auf Port {WEB_PORT}: antwortet"
                        + (f", erreichbar unter {WEB}" if WEB else "")))
        except requests.RequestException as e:
            out.append((False, f"Einrichtungsseite auf Port {WEB_PORT} nicht erreichbar ({type(e).__name__}, läuft der Dienst?)"))
    if GOOGLE_KONTO:
        try:
            r = requests.post(f"http://127.0.0.1:{GOOGLE_PORT}/mcp", timeout=10,
                              headers={"Authorization": f"Bearer {secret('GOOGLE_MCP_TOKEN')}",
                                       "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "pruefen", "version": "1"}}})
            out.append((r.ok, f"Google-Dienst auf Port {GOOGLE_PORT}: HTTP {r.status_code}"
                        + ("" if r.ok else " (läuft google-mcp.service?)")))
        except requests.RequestException as e:
            out.append((False, f"Google-Dienst auf Port {GOOGLE_PORT} nicht erreichbar ({type(e).__name__})"))
    if eingerichtet() or not WEB:
        out.append((eingerichtet(), f"home/CLAUDE.md in {HOME_DIR}"))
    else:
        out.append((True, "home/ entsteht, wenn die Person die Einrichtung abschließt"))
    jobs()
    out.append((not _kaputt, "Zeitpläne lesbar" if not _kaputt else f"Zeitpläne unlesbar: {', '.join(sorted(_kaputt))}"))
    return out


def main():
    args = sys.argv[1:]
    if not args:
        return dienst()
    if args[0] == "frag" and len(args) > 1:
        antwort, sitzung, fehler, kontext = ki_lauf(f"[Telegram, {stempel()}] {PERSON}: {' '.join(args[1:])}")
        print(fehler and f"FEHLER: {fehler}" or antwort)
        return print(f"\n(Sitzung {sitzung}, {kontext // 1000}k Tokens)", file=sys.stderr)
    if args[0] == "job" and len(args) > 1:
        j = jobs().get(args[1])
        if not j:
            return print(f"Kein Lauf {args[1]}, vorhanden: {', '.join(jobs())}")
        return print(fuehre_job_aus(args[1], j, senden="--senden" in args))
    if args[0] == "neu":
        wechsel("assistent.py neu")
        return print("Die nächste Nachricht beginnt ein neues Gespräch, das alte sichert der Dienst innerhalb einer halben Stunde")
    if args[0] == "status":
        return print(json.dumps({k: v for k, v in load().items() if k != "offset"}, ensure_ascii=False, indent=1))
    if args[0] == "puls":
        befund = pulsbefund()
        print(json.dumps(befund, ensure_ascii=False) if "--json" in args else
              "\n".join(f"{'KRITISCH' if b['stufe'] == 2 else 'Warnung'}: {b['text']}" for b in befund) or f"{NAME} ist in Ordnung")
        sys.exit(1 if befund else 0)
    if args[0] == "einladung":
        link = einladungslink()
        return print(link or f"{PERSON or 'Die Person'} ist schon verbunden, eine Einladung braucht es nicht mehr")
    if args[0] == "pruefen":
        ergebnis = pruefen()
        for ok, text in ergebnis:
            print(("ok    " if ok else "FEHLT ") + text)
        sys.exit(0 if all(ok for ok, _ in ergebnis) else 1)
    print(__doc__)


if __name__ == "__main__":
    main()
