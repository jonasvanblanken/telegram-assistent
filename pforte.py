"""Einrichtungsseite des Assistenten. Läuft im Dienst (assistent.py) auf 127.0.0.1:[web] port, davor Caddy mit HTTPS
unter [web] adresse (https://<box-domain>/<kurzname>, Caddy schneidet /<kurzname> ab). Der Bot öffnet sie als
Web App direkt in Telegram, Anmeldungen bei Anbietern und Google gehen im Browser auf.

  GET  /einrichten?t=<link>        Namen, KI (Claude oder ChatGPT), Kalender und Mail. /anmelden ist dasselbe
  GET  /einrichten/stand?t=<link>  Stand als JSON, mit dem laufenden Login
  POST /einrichten/namen           {t, person, assistent}
  POST /anmelden/start             {t, anbieter}  startet das Login-Programm des Anbieters
  POST /anmelden/code              {t, code}      gibt den Code der Person an das Login-Programm
  POST /anmelden/wechseln          {t, anbieter}  wechselt zu einer schon verbundenen KI, ohne neue Anmeldung
  POST /einrichten/google          {t, konto, dienste}  Google-Dienst mit den Schaltern starten, Anmeldelink zurück
  POST /einrichten/google-app      {t, id, schluessel}  eigene Google-App der Person speichern (eigener Server)
  POST /einrichten/google-aus      {t}            ohne Google weiter
  POST /einrichten/fragebogen      {t, antworten} Kennenlern-Fragebogen speichern, die KI meldet sich danach
  POST /einrichten/fragebogen-aus  {t}            Fragebogen überspringen
  POST /einrichten/whatsapp        {t, an}        WhatsApp dazu (der Server baut sofort) oder ohne
  POST /einrichten/lou             {t}            Lou verbinden: der Server holt einen Zugang bei Lou (einladung: alter Weg)
  POST /einrichten/lou-aus         {t}            Lou aus (Schalter auf der Karte) oder ohne Lou weiter

Lou kommt in der Einrichtung nur vor, wenn sie vorab angehakt ist: Die Seite verbindet Lou dann im Hintergrund und zeigt
den Schritt Lou nur, wenn das nicht klappt. In den Einstellungen ist Lou ein Schalter.
  POST /brain/schalten             {t, an}        Kit Brain an oder aus
  POST /brain/teilen               {t, an}        Ordner Brain in Google Drive teilen oder nicht mehr
  POST /einrichten/fertig          {t}            home/ anlegen, der Assistent stellt sich im Chat vor
  GET  /oauth2callback?...         Rückweg von Google, geht an den Google-Dienst der Instanz
  GET  /whatsapp?t=<wa-link>       WhatsApp verbinden (eigener Link, gilt sieben Tage)
  GET  /whatsapp/stand?t=<wa-link> Schritt als JSON, beim QR-Code mit Bild
  POST /whatsapp/start             {t}  Einwilligung, der Server richtet die Bridge ein
  POST /whatsapp/sitzung           {initData}  frischer Link für die WhatsApp-Seite aus einem älteren Knopf
  POST /whatsapp/nummer            {t, nummer}  Kopplungscode für WhatsApp per Telefonnummer statt QR-Code

Die Login-Programme laufen als die Person in einem Pseudo-Terminal, genau wie am Rechner: `claude setup-token`,
`codex login --device-auth`, `gemini` mit NO_BROWSER=true. Die Seite liest daraus den Link (bei ChatGPT den
Gerätecode) und tippt den Code ein, den die Person nach der Anmeldung zurückbringt. Ein Link gilt eine Stunde.
"""
import html
import http.server
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import threading
import time
import urllib.parse

import anbieter
import kit

ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>78DEHM]")
LOGIN_MAX = 15 * 60
MUSTER = {
    "claude": {"url": re.compile(r"https://claude\.(?:ai|com)/\S*oauth/authorize\?\S+"),
               "token": re.compile(r"sk-ant-oat\d*-[A-Za-z0-9_-]{8,}")},
    "chatgpt": {"url": re.compile(r"https://auth\.openai\.com/codex/device\S*"),
                "code": re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")},
    "gemini": {"url": re.compile(r"https://accounts\.google\.com/o/oauth2/v2/auth\?\S+")},
}
GEMINI_ZUGANG = os.path.join(anbieter.GEMINI_HOME, ".gemini", "oauth_creds.json")
CODEX_ZUGANG = os.path.join(anbieter.CODEX_HOME, "auth.json")


def code_aus(name, eingabe):
    """Was die Person einfügt: der Code oder die ganze Adresse, auf der die Anmeldung endete."""
    e = (eingabe or "").strip()
    if "code=" in e:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(e).query)
        code, state = (q.get("code") or [""])[0], (q.get("state") or [""])[0]
        if name == "claude" and code and state:
            return f"{code}#{state}"   # so erwartet setup-token einen eingefügten Code
        return code
    return re.sub(r"\s+", "", e)


def login_befehl(name):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY",
                        "GEMINI_API_KEY", "GOOGLE_API_KEY")}
    env["PATH"] = f"{kit.HOME}/.local/bin:{anbieter.KI_BIN}:/usr/local/bin:/usr/bin:/bin"
    env["TERM"] = "xterm-256color"
    if name == "claude":
        return [anbieter.programm("claude"), "setup-token"], env, kit.HOME
    if name == "chatgpt":
        os.makedirs(anbieter.CODEX_HOME, mode=0o700, exist_ok=True)
        return [anbieter.programm("chatgpt"), "login", "--device-auth"], {**env, "CODEX_HOME": anbieter.CODEX_HOME}, kit.HOME
    ordner = os.path.join(anbieter.GEMINI_HOME, "anmeldung")
    os.makedirs(ordner, mode=0o700, exist_ok=True)
    return [anbieter.programm("gemini")], {**env, "GEMINI_CLI_HOME": anbieter.GEMINI_HOME, "NO_BROWSER": "true"}, ordner


class Anmeldung:
    """Ein Login-Programm im Pseudo-Terminal. stand: startet, bereit (Link da), prueft, fertig, fehler."""

    def __init__(self, name, fertig):
        self.name, self.fertig_melden = name, fertig
        self.stand, self.url, self.geraetecode, self.fehler, self.puffer = "startet", "", "", "", ""
        self.t0, self.lock = time.time(), threading.Lock()
        vorher = {"chatgpt": CODEX_ZUGANG, "gemini": GEMINI_ZUGANG}.get(name)
        self.zugang_alt = os.path.getmtime(vorher) if vorher and os.path.exists(vorher) else 0
        cmd, env, cwd = login_befehl(name)
        master, slave = pty.openpty()
        import fcntl
        import termios
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 4000, 0, 0))   # breit: Links nicht umbrechen
        try:
            self.p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                                      start_new_session=True, close_fds=True)
        except OSError as e:
            os.close(master)
            self.p, self.stand, self.fehler = None, "fehler", f"{anbieter.name(name)} lässt sich nicht starten ({e})"
            return
        finally:
            os.close(slave)
        self.fd = master
        threading.Thread(target=self._lesen, daemon=True).start()

    def _zugang_neu(self, pfad):
        return os.path.exists(pfad) and os.path.getmtime(pfad) > self.zugang_alt

    def _lesen(self):
        while True:
            if time.time() - self.t0 > LOGIN_MAX:
                self._fehler("Die Anmeldung hat zu lange gedauert. Fang einfach noch einmal an.")
                break
            if self.name == "gemini" and self.stand == "prueft" and self._zugang_neu(GEMINI_ZUGANG):
                self._fertig()
                break
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
                if not r:
                    if self.p.poll() is not None:
                        break
                    continue
                daten = os.read(self.fd, 65536)
            except OSError:
                break
            if not daten:
                break
            with self.lock:
                self.puffer = (self.puffer + ANSI.sub("", daten.decode("utf-8", "replace")))[-20000:]
                self._auswerten()
            if self.stand in ("fertig", "fehler"):
                break
        self._ende()

    def _auswerten(self):
        m = MUSTER[self.name]
        if not self.url and m["url"].search(self.puffer):
            self.url = m["url"].search(self.puffer).group(0)
        if self.name == "chatgpt" and self.url and not self.geraetecode:
            rest = self.puffer[self.puffer.find("codex/device"):]
            c = m["code"].search(rest)
            self.geraetecode = c.group(0) if c else ""
        if self.stand == "startet" and self.url and (self.name != "chatgpt" or self.geraetecode):
            self.stand = "bereit" if self.name != "chatgpt" else "prueft"
        if self.name == "claude" and self.stand == "prueft":
            t = m["token"].search(self.puffer)
            if t:
                kit.setze_secret("CLAUDE_CODE_OAUTH_TOKEN", t.group(0))
                kit.setze_secret("CLAUDE_TOKEN_SEIT", time.strftime("%Y-%m-%d"))
                self._fertig()

    def _ende(self):
        """Programm ist zu Ende oder wird beendet: Ergebnis festhalten, aufräumen."""
        if self.stand not in ("fertig", "fehler"):
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if self.name == "chatgpt" and self.p.returncode == 0 and self._zugang_neu(CODEX_ZUGANG):
                self._fertig()
            elif self.name == "gemini" and self._zugang_neu(GEMINI_ZUGANG):
                self._fertig()
            else:
                self._fehler("Das hat nicht geklappt. War der Code vollständig? Fang einfach noch einmal an.")
        self.beenden()
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _fertig(self):
        self.stand = "fertig"
        threading.Thread(target=self.fertig_melden, args=(self.name,), daemon=True).start()

    def _fehler(self, text):
        self.stand, self.fehler = "fehler", text
        letzte = [z.strip() for z in self.puffer.splitlines() if z.strip()][-3:]
        print(f"Anmeldung {self.name} fehlgeschlagen: {' | '.join(letzte)[-300:]}", flush=True)

    def code(self, eingabe):
        if self.stand != "bereit":
            return False
        code = code_aus(self.name, eingabe)
        if not code or len(code) > 2000:
            return False
        with self.lock:
            self.stand = "prueft"
        os.write(self.fd, code.encode() + b"\r")
        return True

    def beenden(self):
        if self.p and self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGTERM)
                self.p.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def json(self):
        return {"stand": self.stand, "url": self.url if self.stand in ("bereit", "prueft") else "",
                "geraetecode": self.geraetecode, "fehler": self.fehler, "anbieter": self.name}


# --- Seiten --------------------------------------------------------------------

STIL = """
:root{--vb-blau:#002FA7;--vb-blau-hell:#1247D8;--vb-blau-tief:#001E6B;--vb-gruen:#83FD80;--vb-gruen-zart:#DFF9DD;
--vb-gruen-tief:#0B5D2A;--vb-grund:#EFF2F6;--vb-karte:#fff;--vb-tinte:#0E1525;--vb-grau:#5B6577;--vb-linie:#DCE2EA;
--vb-schatten:0 1px 2px rgba(14,21,37,.06),0 12px 32px rgba(14,21,37,.08);color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:var(--vb-grund);color:var(--vb-tinte);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
padding:0 16px 48px}
.leiste{max-width:620px;margin:0 auto;padding:16px 0;display:flex;align-items:center;gap:12px;font-weight:600}
.zeichen{width:38px;height:38px;border-radius:11px;background:#fff;box-shadow:var(--vb-schatten);display:grid;
place-items:center;color:var(--vb-blau);font-weight:800}
main{max-width:620px;margin:0 auto}
h1{font-size:clamp(40px,11vw,64px);line-height:1;letter-spacing:-.03em;margin:28px 0 10px}
h2{font-size:19px;font-weight:500;color:var(--vb-grau);margin:0 0 28px;line-height:1.4}
.karte{background:var(--vb-karte);border-radius:26px;box-shadow:var(--vb-schatten);padding:22px;margin-bottom:14px}
.wahl{display:flex;align-items:center;gap:16px;width:100%;border:0;text-align:left;font:inherit;color:inherit;cursor:pointer;
transition:transform .15s ease,box-shadow .15s ease}
.wahl:hover{transform:translateY(-2px);box-shadow:0 2px 4px rgba(14,21,37,.06),0 18px 40px rgba(0,47,167,.16)}
.kachel{width:48px;height:48px;border-radius:14px;display:grid;place-items:center;flex:none;background:#E7EDFB;color:var(--vb-blau)}
.kachel.gruen{background:var(--vb-gruen-zart);color:var(--vb-gruen-tief)}
.wahl b{display:block;font-size:19px}
.pille{display:inline-block;border-radius:999px;padding:3px 11px;font-size:13px;font-weight:600;background:#E7EDFB;color:var(--vb-blau)}
.pille.gruen{background:var(--vb-gruen-zart);color:var(--vb-gruen-tief)}
.knopf{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:0;border-radius:999px;background:var(--vb-blau);
color:#fff;font-family:inherit;font-weight:600;font-size:17px;line-height:1;padding:16px 24px;text-decoration:none;cursor:pointer;width:100%}
.knopf:disabled{opacity:.5}
.knopf.leise{background:#E7EDFB;color:var(--vb-blau)}
.schritt{display:flex;gap:14px;align-items:flex-start}
.schritt p{margin:2px 0 14px;color:var(--vb-grau)}
input{width:100%;font:inherit;font-size:17px;padding:15px 18px;border-radius:16px;border:1.5px solid var(--vb-linie);margin-bottom:12px;
background:#fff;color:var(--vb-tinte)}
input:focus{outline:none;border-color:var(--vb-blau)}
.code{font-size:clamp(34px,10vw,48px);font-weight:800;letter-spacing:.06em;text-align:center;background:var(--vb-gruen-zart);
color:var(--vb-gruen-tief);border-radius:20px;padding:18px;margin:0 0 14px;user-select:all}
.code.klein{font-size:14px;font-weight:600;letter-spacing:0;text-align:left;word-break:break-all;padding:12px 14px;margin:10px 0 8px}
.schritte{padding-left:22px;margin:14px 0 18px}
.schritte li{margin-bottom:12px;color:var(--vb-grau)}
.schritte a{color:var(--vb-blau);font-weight:600}
.buehne{background:radial-gradient(120% 120% at 20% 0%,var(--vb-blau-hell),var(--vb-blau) 55%,var(--vb-blau-tief));color:#fff;
border-radius:28px;padding:34px 26px;box-shadow:0 30px 60px -20px rgba(131,253,128,.45),var(--vb-schatten);position:relative;overflow:hidden}
.buehne::after{content:"";position:absolute;width:260px;height:260px;right:-80px;top:-90px;border-radius:50%;
background:radial-gradient(circle,rgba(131,253,128,.55),transparent 65%)}
.buehne .kachel{background:var(--vb-gruen);color:var(--vb-blau-tief);margin-bottom:14px}
.buehne b{font-size:24px;display:block;margin-bottom:4px}
.fehler{color:#9B1C1C;margin:0 0 12px}
.warte{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--vb-gruen-tief);animation:puls 1.2s infinite}
@keyframes puls{50%{opacity:.25}}
.fortschritt{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}
.fortschritt span{border-radius:999px;padding:7px 14px;font-size:14px;font-weight:600;background:#fff;color:var(--vb-grau);
box-shadow:var(--vb-schatten)}
.fortschritt span.aktiv{background:var(--vb-tinte);color:#fff}
.fortschritt span.fertig{background:var(--vb-gruen-zart);color:var(--vb-gruen-tief)}
.stufe header{display:flex;align-items:center;gap:14px}
.stufe header b{display:block;font-size:19px}
.stufe header small{display:block;color:var(--vb-grau);font-size:14px}
.stufe header div{flex:1;min-width:0}
.stufe .inhalt{display:none;margin-top:18px}
.stufe.offen .inhalt{display:block}
.stufe:not(.offen):not(.erledigt){opacity:.55}
.stufe.erledigt .kachel{background:var(--vb-gruen-zart);color:var(--vb-gruen-tief)}
.aendern{border:0;background:#E7EDFB;color:var(--vb-blau);font-family:inherit;font-weight:600;font-size:14px;border-radius:999px;
padding:8px 14px;cursor:pointer}
label{display:block;font-weight:600;font-size:15px;margin-bottom:4px}
label input{margin-top:6px;font-weight:400}
.zeile,.schalter{display:flex;align-items:center;gap:14px;width:100%;border:0;background:var(--vb-grund);border-radius:20px;
padding:14px;margin-bottom:10px;text-align:left;font:inherit;color:inherit;cursor:pointer}
.zeile:hover{background:#E7EDFB}
.zeile b,.schalter b{display:block;font-size:17px}
.zeile small,.schalter small{display:block;color:var(--vb-grau);font-weight:400;font-size:14px}
.schalter>span:nth-child(2){flex:1}
.schalter input{position:absolute;opacity:0;width:1px;height:1px}
.schalter i{width:50px;height:30px;border-radius:999px;background:var(--vb-linie);position:relative;flex:none;transition:background .15s}
.schalter i::after{content:"";position:absolute;left:3px;top:3px;width:24px;height:24px;border-radius:50%;background:#fff;
box-shadow:0 1px 3px rgba(14,21,37,.2);transition:transform .15s}
.schalter input:checked+i{background:var(--vb-blau)}
.schalter input:checked+i::after{transform:translateX(20px)}
.schalter input:focus-visible+i{outline:2px solid var(--vb-blau);outline-offset:2px}
.feld{display:flex;gap:8px}
.feld input{flex:1}
textarea{width:100%;font:inherit;font-size:17px;padding:15px 18px;border-radius:16px;border:1.5px solid var(--vb-linie);
  margin:6px 0 12px;resize:vertical;font-weight:400;background:#fff;color:inherit}
textarea:focus{outline:none;border-color:var(--vb-blau)}
.frage{margin:4px 0 0;font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 16px}
.chip{border:1.5px solid var(--vb-linie);background:#fff;color:inherit;border-radius:999px;padding:9px 15px;font:inherit;
  font-size:15px;cursor:pointer;transition:background .15s,color .15s}
.chip[aria-pressed=true]{background:var(--vb-blau);border-color:var(--vb-blau);color:#fff}
.einfuegen{border:0;border-radius:16px;background:#E7EDFB;color:var(--vb-blau);font-family:inherit;font-weight:600;font-size:15px;
padding:0 16px;margin-bottom:12px;cursor:pointer}
.knopf+.knopf{margin-top:10px}
.knopf.ohne{background:none;color:var(--vb-grau);margin-top:6px}
.link{display:block;border:0;background:none;color:var(--vb-blau);font-family:inherit;font-weight:600;font-size:15px;
padding:14px 0 2px;margin:0 auto;cursor:pointer}
.knopf.hell{background:#fff;color:var(--vb-blau);margin-top:22px;position:relative;z-index:1}
.inhalt p{color:var(--vb-grau)}
@media (prefers-reduced-motion:reduce){.wahl,.schalter i,.schalter i::after{transition:none}.warte{animation:none}}
[hidden]{display:none!important}
"""
ICON = {
    "funke": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5'
             'M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/></svg>',
    "haken": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
             'stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5 10 17 19 7"/></svg>',
    "schloss": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
               'stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/>'
               '<path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
    "kalender": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="15" rx="2"/>'
                '<path d="M4 10h16M9 3v4M15 3v4"/></svg>',
    "person": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
    "mail": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/>'
            '<path d="m3 7 9 6 9-6"/></svg>',
    "sprechblase": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                   'stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>',
    "handy": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="2.5" width="12" height="19" rx="2.5"/>'
             '<path d="M10.5 18.5h3"/></svg>',
    "ordner": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5'
              'a2 2 0 0 1-2-2z"/></svg>',
    "paket": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round"><path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/></svg>',
    "herz": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 '
            '7.5 2.6C19.5 15.4 12 20 12 20z"/></svg>',
    "stapel": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 9 5-9 5-9-5z"/><path d="m3 12.5 9 5 9-5"/>'
              '<path d="m3 17 9 5 9-5"/></svg>',
    "kreuz": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
             'stroke-linecap="round"><path d="M7 7l10 10M17 7 7 17"/></svg>',
}


def seite(name, titel, h1, h2, inhalt, skript=""):
    e = html.escape
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>{e(titel)}</title>
<style>{STIL}</style></head><body>
<div class="leiste"><span class="zeichen" id="zeichen">{e(name[:1].upper())}</span><span id="name">{e(name)}</span></div>
<main><h1>{e(h1)}</h1><h2>{e(h2)}</h2>{inhalt}</main>{skript}</body></html>"""


def kopf(schritt, icon, titel):
    return (f'<header><span class="kachel">{ICON[icon]}</span><div><b>{titel}</b><small class="erledigt-text"></small></div>'
            f'<button class="aendern" data-schritt="{schritt}" hidden>Ändern</button></header>')


def einrichtungsseite(name, bot, einstellungen=False):
    e = html.escape
    wahl = "".join(
        f'<button class="zeile" data-anbieter="{k}"><span class="kachel">{ICON["funke"]}</span>'
        f'<span><b>{e(v["name"])}</b><small data-abo="{e(v["abo"])}">{e(v["abo"])}</small></span></button>'
        for k, v in ((k, anbieter.ANBIETER[k]) for k in anbieter.ANGEBOTEN))
    schalter = "".join(
        f'<label class="schalter"><span class="kachel">{ICON[icon]}</span><span><b>{titel}</b><small>{was}</small></span>'
        f'<input type="checkbox" data-dienst="{k}" checked><i aria-hidden="true"></i></label>'
        for k, icon, titel, was in (("kalender", "kalender", "Kalender", "Termine und Aufgaben"),
                                    ("mail", "mail", "Mail", "Lesen, Entwürfe, Drive")))
    inhalt = f"""
<nav class="fortschritt"><span data-zu="namen">Namen</span><span data-zu="ki">KI</span><span data-zu="google">Kalender und Mail</span><span data-zu="whatsapp">WhatsApp</span><span data-zu="lou">Lou</span><span data-zu="profil">Über dich</span></nav>
<p class="karte" id="abgelaufen" hidden>Dieser Link ist abgelaufen. Schick deinem Bot /start, dann bekommst du einen neuen.</p>
<section class="karte stufe" id="s-namen">{kopf("namen", "person", "Namen")}
  <div class="inhalt">
    <label>Dein Vorname<input id="person" autocomplete="given-name" maxlength="30"></label>
    <label>Name deines Assistenten<input id="assistent" maxlength="30" placeholder="zum Beispiel Lotte" autocomplete="off"></label>
    <p class="fehler" id="namen-fehler" hidden></p>
    <button class="knopf" id="namen-weiter">Weiter</button>
  </div>
</section>
<section class="karte stufe" id="s-ki">{kopf("ki", "funke", "Deine KI")}
  <div class="inhalt">
    <div id="wahl">{wahl}</div>
    <div id="login" hidden>
      <p id="laedt"><span class="warte"></span> Einen Moment …</p>
      <div id="link-teil" hidden>
        <p id="link-text"></p>
        <div class="code" id="geraetecode" hidden></div>
        <button class="knopf leise" id="kopieren" hidden>Code kopieren</button>
        <button class="knopf" id="link">Anmelden</button>
      </div>
      <div id="code-teil" hidden>
        <p id="code-text"></p>
        <div class="feld"><input id="code" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Code">
          <button class="einfuegen" id="einfuegen">Einfügen</button></div>
        <p class="fehler" id="code-fehler" hidden></p>
        <button class="knopf" id="senden">Verbinden</button>
      </div>
      <p id="pruefen" hidden><span class="warte"></span> <span id="pruefen-text"></span></p>
      <div id="fehlschlag" hidden><p class="fehler" id="fehler-text"></p><button class="knopf leise" id="nochmal">Noch einmal</button></div>
    </div>
  </div>
</section>
<section class="karte stufe" id="s-google">{kopf("google", "kalender", "Kalender und Mail")}
  <div class="inhalt">
    <div id="google-app" hidden>
      <p>Kalender und Mail laufen über eine kleine Google-App, die nur dir gehört. Du legst sie einmal in deinem Google-Konto an, das dauert zehn Minuten und geht am besten am Computer.</p>
      <ol class="schritte">
        <li><a href="https://console.cloud.google.com/projectcreate" data-extern>Projekt anlegen</a>, Name zum Beispiel „Assistent“.</li>
        <li><a href="https://console.cloud.google.com/flows/enableapi?apiid=gmail.googleapis.com,calendar-json.googleapis.com,drive.googleapis.com,tasks.googleapis.com" data-extern>Gmail, Kalender, Drive und Tasks einschalten</a>, im eben angelegten Projekt.</li>
        <li><a href="https://console.cloud.google.com/auth/overview" data-extern>Google Auth Platform</a> öffnen und auf „Get started“ tippen. App-Name „Assistent“, deine Adresse als Support-Mail. Bei Zielgruppe wählst du mit einem Firmenkonto „Intern“, mit einer Gmail-Adresse „Extern“.</li>
        <li id="domain-schritt">Unter <a href="https://console.cloud.google.com/auth/branding" data-extern>Branding</a> bei „Autorisierte Domains“ diese Domain eintragen und speichern:
          <div class="code klein" id="domain"></div></li>
        <li>Nur bei „Extern“: unter <a href="https://console.cloud.google.com/auth/audience" data-extern>Zielgruppe</a> auf „App veröffentlichen“ tippen und bestätigen. Ohne das lässt Google dich gar nicht erst rein.</li>
        <li><a href="https://console.cloud.google.com/auth/clients/create" data-extern>Client erstellen</a>: Anwendungstyp „Webanwendung“, unter „Autorisierte Weiterleitungs-URIs“ diese Adresse einfügen:
          <div class="code klein" id="rueckruf"></div><button class="knopf leise" id="rueckruf-kopieren">Adresse kopieren</button></li>
        <li>Nach „Erstellen“ zeigt Google Client-ID und Clientschlüssel. Kopier beide hierher.</li>
      </ol>
      <label>Client-ID<input id="google-id" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="…apps.googleusercontent.com"></label>
      <label>Clientschlüssel<input id="google-schluessel" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="GOCSPX-…"></label>
      <p class="fehler" id="google-app-fehler" hidden></p>
      <button class="knopf" id="google-app-los">Google-App speichern</button>
    </div>
    <div id="google-konto-teil">
    {schalter}
    <label>Dein Google-Konto<input id="konto" type="email" autocomplete="email" inputmode="email" placeholder="name@gmail.com"></label>
    <p class="fehler" id="google-fehler" hidden></p>
    <button class="knopf" id="google-los">Mit Google verbinden</button>
    <div id="google-link-teil" hidden>
      <p>Google warnt gleich, dass es die App nicht kennt, weil sie nur für dich läuft. Tipp auf „Erweitert“ und dann auf „Zu Assistent (unsicher) wechseln“.</p>
      <button class="knopf" id="google-link">Zu Google</button>
      <p id="google-warte"><span class="warte"></span> Warte auf Google …</p>
      <div id="google-hinweis" hidden>
        <p>Von Google kommt noch nichts. Zeigt Google dir einen Fehler, liegt es meist an der Google-App:</p>
        <ul class="schritte">
          <li>„redirect_uri_mismatch“: Die Weiterleitungsadresse im Client weicht ab, sie muss genau stimmen.</li>
          <li>„invalid_client“: Client-ID oder Clientschlüssel passen nicht zusammen.</li>
          <li>„org_internal“: Die Zielgruppe steht auf „Intern“. Mit einer Gmail-Adresse braucht es „Extern“.</li>
          <li>„access_denied“ oder „Zugriff blockiert“: Die App ist noch nicht veröffentlicht.</li>
        </ul>
        <button class="knopf leise" id="google-app-aendern">Google-App neu eintragen</button>
      </div>
    </div>
    </div>
    <button class="knopf ohne" id="google-aus">Ohne Google weiter</button>
  </div>
</section>
<section class="karte stufe" id="s-whatsapp">{kopf("whatsapp", "handy", "WhatsApp")}
  <div class="inhalt">
    <p>Auf Wunsch liest <span class="an">dein Assistent</span> nachts deine Einzelchats mit und sagt dir morgens, wem
    du noch antworten wolltest und was bei den Menschen los ist, die dir wichtig sind. Gruppen bleiben außen vor,
    geschrieben wird weiter nur in Telegram.</p>
    <p>Die Verbindung läuft über ein inoffizielles Programm, das sich wie WhatsApp Web anmeldet. WhatsApp kann so eine
    Verknüpfung im schlimmsten Fall sperren. Deine Nachrichten bleiben verschlüsselt auf deinem Server.</p>
    <p class="fehler" id="whatsapp-fehler" hidden></p>
    <button class="knopf" id="whatsapp-ja">Ja, WhatsApp dazu</button>
    <button class="knopf ohne" id="whatsapp-ohne">Ohne WhatsApp weiter</button>
  </div>
</section>
<section class="karte stufe" id="s-lou">{kopf("lou", "herz", "Lou")}
  <div class="inhalt">
    <p>Lou denkt mit dir über das nach, was dich beschäftigt, mit therapeutischen Techniken. Ihr schreibt im selben Chat,
    du beginnst mit „Hallo Lou“.</p>
    <label class="schalter" id="lou-schalter" hidden><span class="kachel">{ICON["sprechblase"]}</span><span><b>Lou</b><small id="lou-schalter-text"></small></span>
      <input type="checkbox" id="lou-an"><i aria-hidden="true"></i></label>
    <p class="fehler" id="lou-fehler" hidden></p>
    <button class="knopf" id="lou-los">Lou verbinden</button>
    <button class="knopf ohne" id="lou-ohne">Ohne Lou weiter</button>
  </div>
</section>
<section class="karte stufe offen" id="s-brain" hidden><header><span class="kachel">{ICON["stapel"]}</span><div><b>Brain</b><small id="brain-text"></small></div></header>
  <div class="inhalt">
    <label class="schalter"><span class="kachel">{ICON["ordner"]}</span><span><b>Brain</b><small>Themen geordnet in einem eigenen Ordner</small></span>
      <input type="checkbox" id="brain-an"><i aria-hidden="true"></i></label>
    <div id="brain-teil" hidden>
      <p id="brain-hinweis"></p>
      <button class="knopf leise" id="brain-teilen">In Google Drive teilen</button>
    </div>
    <p class="fehler" id="brain-fehler" hidden></p>
  </div>
</section>
<section class="karte stufe" id="s-profil">{kopf("profil", "sprechblase", "Über dich")}
  <div class="inhalt">
    <p>Damit <span class="an">dein Assistent</span> dich von Anfang an kennt.</p>
    <label>Wo wohnst du?<input id="f-wohnort" maxlength="200" placeholder="zum Beispiel Köln-Ehrenfeld"></label>
    <label>Was machst du beruflich oder tagsüber?<input id="f-arbeit" maxlength="300" placeholder="zum Beispiel Lehrerin an einer Grundschule"></label>
    <label>Wer gehört zu deinem Alltag?<textarea id="f-menschen" rows="3" maxlength="600" placeholder="zum Beispiel Partner Tim, Tochter Mia (6), Mama in Bonn"></textarea></label>
    <label>Was steht jede Woche fest an?<textarea id="f-woche" rows="2" maxlength="600" placeholder="zum Beispiel dienstags Sport, freitags Mia vom Turnen abholen"></textarea></label>
    <p class="frage">Wobei soll <span class="an">dein Assistent</span> vor allem helfen?</p><div class="chips" id="f-hilfe"></div>
    <p class="frage">Wie soll <span class="an">dein Assistent</span> mit dir reden?</p><div class="chips" id="f-ton"></div>
    <label>Was sollte <span class="an">dein Assistent</span> sonst noch wissen?<textarea id="f-sonst" rows="2" maxlength="600"></textarea></label>
    <p class="fehler" id="profil-fehler" hidden></p>
    <button class="knopf" id="profil-weiter">Speichern</button>
    <button class="knopf ohne" id="profil-aus">Überspringen</button>
  </div>
</section>
<section class="karte stufe offen" id="s-ablage" hidden><header><span class="kachel">{ICON["ordner"]}</span><div><b>Ablage</b><small id="ablage-text"></small></div></header>
  <div class="inhalt">
    <p>Auf Wunsch legt <span class="an">dein Assistent</span> jede Nacht eine Kopie von Gedächtnis und Chat-Protokoll in einen Ordner bei dir.</p>
    <div id="ablage-wahl"></div>
    <div id="ablage-dropbox" hidden>
      <p>Dropbox zeigt dir nach der Freigabe einen Code. Kopier ihn und füg ihn hier ein.</p>
      <button class="knopf" id="ablage-link">Zu Dropbox</button>
      <div class="feld" style="margin-top:12px"><input id="ablage-code" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Code"></div>
      <button class="knopf" id="ablage-senden">Verbinden</button>
    </div>
    <div id="ablage-geraet" hidden>
      <p>Melde dich bei Microsoft an und gib diesen Code ein. Danach geht es hier von selbst weiter.</p>
      <div class="code" id="ablage-geraetecode"></div>
      <button class="knopf leise" id="ablage-kopieren">Code kopieren</button>
      <button class="knopf" id="ablage-ms">Zu Microsoft</button>
      <p><span class="warte"></span> Warte auf Microsoft …</p>
    </div>
    <p class="fehler" id="ablage-fehler" hidden></p>
    <button class="knopf ohne" id="ablage-aus" hidden>Nur auf dem Server lassen</button>
  </div>
</section>
<section class="karte stufe offen" id="s-daten" hidden><header><span class="kachel">{ICON["paket"]}</span><div><b>Meine Daten</b><small>Gedächtnis, Profil und Chat-Protokoll als ZIP</small></div></header>
  <div class="inhalt">
    <label class="schalter"><span class="kachel">{ICON["kalender"]}</span><span><b>Jeden Monat</b><small>Am Ersten kommt alles als Datei in den Chat</small></span>
      <input type="checkbox" id="monatlich"><i aria-hidden="true"></i></label>
    <p class="fehler" id="daten-fehler" hidden></p>
    <button class="knopf leise" id="daten-jetzt">Jetzt in den Chat schicken</button>
  </div>
</section>
<section class="karte stufe offen" id="s-support" hidden><header><span class="kachel">{ICON["sprechblase"]}</span><div><b>Support</b></div></header>
  <div class="inhalt"><button class="knopf leise" id="support-schreiben">Support schreiben</button></div>
</section>
<section id="fertig" hidden><div class="buehne"><span class="kachel">{ICON["haken"]}</span><b id="fertig-titel">Fertig</b>
  <span id="fertig-text"></span><button class="knopf hell" id="zu-telegram">Zu Telegram</button></div></section>"""
    skript = """<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
const BOT=__BOT__,ORDNER=__ORDNER__,EINSTELLUNGEN=__EINSTELLUNGEN__,tg=window.Telegram&&Telegram.WebApp&&Telegram.WebApp.initData?Telegram.WebApp:null;
if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#EFF2F6');tg.setBackgroundColor('#EFF2F6')}catch(e){}}
let t=new URLSearchParams(location.search).get('t');const $=id=>document.getElementById(id);
const NAMEN={claude:'Claude',chatgpt:'ChatGPT',gemini:'Gemini'},STUFEN=['namen','ki','google','whatsapp','lou','profil'],FELDER=['wohnort','arbeit','menschen','woche','sonst'];
let S=null,offen='',zeit=null,googleZeit=null,bearbeitet=false,louGezeigt=false,appNeu=false;
async function stand(){try{const r=await fetch('einrichten/stand?t='+encodeURIComponent(t));return r.ok?r.json():null}catch(e){return null}}
async function post(pfad,d){try{const r=await fetch(pfad,{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify(Object.assign({t},d))});return await r.json()}catch(e){return{ok:false,fehler:'Die Verbindung ist weg. Versuch es noch einmal.'}}}
function oeffne(url){if(tg&&tg.openLink)tg.openLink(url);else window.open(url,'_blank','noopener')}
// Support schreiben: WhatsApp an die Nummer aus der Anleitung, mit Stand, KI, Kits und letzter Meldung, nie mit Schlüsseln
function supportZeigen(){const s=S.support||{};$('s-support').hidden=!s.whatsapp;if(!s.whatsapp)return;
  $('support-schreiben').onclick=()=>{const kits=[],meldung=[];
    if(S.whatsapp&&S.whatsapp.an)kits.push('Stay Close');if(S.brain&&S.brain.an)kits.push('Brain');
    if(S.lou&&S.lou.an)kits.push('Lou');if(S.google&&S.google.verbunden)kits.push('Kalender und Mail');
    for(const f of [S.lou&&S.lou.fehler,S.brain&&S.brain.fehler,S.ablage&&S.ablage.fehler])if(f)meldung.push(f);
    const text='Hallo, ich brauche Hilfe mit meinem Assistenten.\\n'+(S.eingerichtet?'Eingerichtet':'Noch in der Einrichtung')+
      (s.fassung?', Stand '+s.fassung:'')+(S.anbieter?', KI '+S.anbieter:'')+', Kits: '+(kits.join(', ')||'keine')+'.'+
      (meldung.length?'\\nLetzte Meldung: '+meldung.join(' / '):'')+'\\n\\nMein Problem: ';
    oeffne('https://wa.me/'+s.whatsapp+'?text='+encodeURIComponent(text))}}
function fehler(id,text){$(id).textContent=text||'';$(id).hidden=!text}
function hat(k){return S.eingerichtet||!S.kits||S.kits.includes(k)}   // vorab gewählte Kits, in den Einstellungen alle
function erledigt(){return{namen:!!(S.person&&S.assistent),ki:!!S.anbieter,
  google:!S.google.moeglich||!hat('kalender')||S.google.verbunden||S.google.uebersprungen,
  whatsapp:!S.whatsapp||!S.whatsapp.moeglich||!hat('whatsapp')||!!S.whatsapp.wahl||S.whatsapp.an||S.eingerichtet,
  lou:S.eingerichtet||!louSchritt()||S.lou.an||!!S.lou.wahl,
  profil:!!(S.fragebogen&&S.fragebogen.erledigt)}}
function zeichne(){
  if(S.assistent){$('name').textContent=S.assistent;$('zeichen').textContent=S.assistent[0].toUpperCase()}
  const e=erledigt(),wa=!!(S.whatsapp&&S.whatsapp.moeglich&&hat('whatsapp')),go=!!(S.google.moeglich&&hat('kalender'));
  $('s-google').hidden=document.querySelector('[data-zu=google]').hidden=!go;
  const app=!!S.google.app&&!appNeu;$('google-app').hidden=app;$('google-konto-teil').hidden=!app;$('rueckruf').textContent=S.google.rueckruf||'';
  $('domain').textContent=S.google.domain||'';$('domain-schritt').hidden=!S.google.domain;
  $('s-whatsapp').hidden=document.querySelector('[data-zu=whatsapp]').hidden=!wa;
  $('s-lou').hidden=document.querySelector('[data-zu=lou]').hidden=!(S.lou&&(S.eingerichtet||louSchritt()));
  const texte={namen:S.person&&S.assistent?S.person+' und '+S.assistent:'',ki:NAMEN[S.anbieter]||'',
    google:S.google.verbunden?S.google.konto:S.google.abgelaufen?'Abgelaufen, bitte neu verbinden':(S.google.uebersprungen?'Ohne Google':''),
    whatsapp:S.whatsapp&&(S.whatsapp.an?'Verbunden':S.whatsapp.wahl==='ja'?'Wird eingerichtet, der QR-Code kommt im Chat':
      S.whatsapp.wahl==='ohne'?'Ohne WhatsApp':'Später im Chat mit /whatsapp')||'',
    lou:S.lou&&(S.lou.an?'Verbunden':S.eingerichtet?'Aus':S.lou.wahl==='ohne'?'Ohne Lou':'')||'',
    profil:S.fragebogen&&Object.keys(S.fragebogen.antworten||{}).length?'Beantwortet':S.fragebogen&&S.fragebogen.erledigt?'Übersprungen':'Noch offen'};
  document.querySelectorAll('.an').forEach(x=>x.textContent=S.assistent||'dein Assistent');
  for(const k of STUFEN){const el=$('s-'+k),karte=karteImmerOffen(k),fertig=!karte&&(e[k]||S.eingerichtet)&&k!==offen;   // in den Einstellungen ist jeder Bereich offen zum Ändern
    el.classList.toggle('offen',k===offen||karte);el.classList.toggle('erledigt',fertig||(karte&&!!S.lou.an));
    el.querySelector('.aendern').hidden=!fertig||!!zeit;el.querySelector('.erledigt-text').textContent=fertig||karte?texte[k]:'';
    const n=document.querySelector('[data-zu='+k+']');n.classList.toggle('aktiv',k===offen);n.classList.toggle('fertig',fertig)}
  wahlZeichnen();louZeichnen();if(S.eingerichtet){ablageZeichnen();brainZeichnen()}}
function karteImmerOffen(k){return k==='lou'&&S.eingerichtet}   // Lou ist in den Einstellungen eine Karte wie Ablage
function louSchritt(){   // in der Einrichtung nur, wenn Lou vorab angehakt ist und das Verbinden im Hintergrund scheiterte
  if(S.lou&&S.kits&&S.kits.includes('lou')&&S.lou.fehler&&!S.lou.an)louGezeigt=true;return louGezeigt}
function louVorab(){if(S.eingerichtet||!S.kits||!S.kits.includes('lou')||!S.lou||S.lou.an||S.lou.wahl||S.lou.fehler)return;
  post('einrichten/lou',{}).then(r=>{S.lou=r.lou||Object.assign(S.lou,{fehler:r.ok===false?r.fehler:''});if(S.lou.fehler)zeichne()})}
function weiter(){
  if(bearbeitet&&S.eingerichtet){offen='';zeichne();return fertig('Gespeichert','Du kannst zurück zu Telegram.')}
  const e=erledigt();offen=STUFEN.find(k=>!e[k])||'';zeichne();if(!offen)abschluss()}
async function abschluss(){
  if(!S.eingerichtet){const r=await post('einrichten/fertig',{});
    if(r.ok===false){offen='ki';zeichne();return fehler('code-fehler',r.fehler)}}
  fertig('Fertig',(S.assistent||'Dein Assistent')+' schreibt dir jetzt in Telegram.');
  if(S.kits&&S.kits.includes('ablage')&&!S.eingerichtet){const s=await stand();   // vorab gewählt: Ablage gleich hier verbinden
    if(s&&s.ablage){S.ablage=s.ablage;ablageZeichnen();if(!$('s-ablage').hidden)$('s-ablage').scrollIntoView({behavior:'smooth',block:'start'})}}}
function fertig(titel,text){clearInterval(zeit);clearInterval(googleZeit);zeit=null;offen='';zeichne();$('fertig-titel').textContent=titel;
  $('fertig-text').textContent=text;$('fertig').hidden=false;
  for(const k of STUFEN)if(!karteImmerOffen(k))$('s-'+k).classList.remove('offen');$('fertig').scrollIntoView({behavior:'smooth',block:'center'})}
function oeffnen(k){bearbeitet=true;offen=k;$('fertig').hidden=true;zeichne();if(k==='ki'){zuruecksetzen();wahlZeichnen()}}
document.querySelectorAll('.aendern').forEach(b=>b.onclick=()=>oeffnen(b.dataset.schritt));
$('zu-telegram').onclick=()=>{if(tg)tg.close();else location.href='https://t.me/'+BOT};
$('namen-weiter').onclick=async()=>{const p=$('person').value.trim(),a=$('assistent').value.trim();
  if(!p||!a)return fehler('namen-fehler','Bitte beide Namen eintragen.');
  $('namen-weiter').disabled=true;const r=await post('einrichten/namen',{person:p,assistent:a});$('namen-weiter').disabled=false;
  if(r.ok===false)return fehler('namen-fehler',r.fehler);fehler('namen-fehler','');S.person=p;S.assistent=a;weiter()};
const zeige=(...ids)=>{for(const id of ['laedt','link-teil','code-teil','pruefen','fehlschlag'])$(id).hidden=!ids.includes(id)};
function zuruecksetzen(){clearInterval(zeit);zeit=null;$('wahl').hidden=false;$('login').hidden=true;$('code').value='';fehler('code-fehler','')}
function login(s){
  if(s.stand==='fertig'){clearInterval(zeit);zeit=null;S.anbieter=s.anbieter;
    S.verbunden=[...new Set([...(S.verbunden||[]),s.anbieter])];zuruecksetzen();return weiter()}
  if(s.stand==='fehler'||s.ok===false){clearInterval(zeit);zeit=null;$('fehler-text').textContent=s.fehler||'Das hat nicht geklappt.';return zeige('fehlschlag')}
  if(s.stand==='startet'||!s.url)return zeige('laedt');
  const n=NAMEN[s.anbieter]||'';$('link').dataset.url=s.url;
  if(s.anbieter==='chatgpt'){$('link-text').textContent='Schalte vorher in ChatGPT unter Einstellungen bei Sicherheit die Anmeldung per Gerätecode für Codex ein. Dann melde dich bei OpenAI mit deinem ChatGPT-Konto an und gib diesen Code ein, danach geht es hier von selbst weiter.';
    $('geraetecode').textContent=s.geraetecode;$('geraetecode').hidden=$('kopieren').hidden=false;$('link').textContent='Zu OpenAI';
    $('pruefen-text').textContent='Warte auf OpenAI …';return zeige('link-teil','pruefen')}
  $('geraetecode').hidden=$('kopieren').hidden=true;$('link').textContent='Bei '+n+' anmelden';
  $('link-text').textContent=s.anbieter==='claude'?'Mit dem Konto, zu dem dein Claude-Abo gehört.':'Mit deinem privaten Google-Konto.';
  $('code-text').textContent='Danach zeigt dir '+n+' einen Code. Kopier ihn und füg ihn hier ein, am Handy mit langem Tippen ins Feld.';
  if(s.stand==='prueft'){$('pruefen-text').textContent='Ich prüfe die Anmeldung …';return zeige('pruefen')}
  zeige('link-teil','code-teil')}
function beobachten(){clearInterval(zeit);zeit=setInterval(async()=>{const s=await stand();if(s&&s.login)login(s.login)},2000)}
function wahlZeichnen(){document.querySelectorAll('[data-anbieter]').forEach(b=>{const k=b.dataset.anbieter,x=b.querySelector('small');
  x.textContent=k===S.anbieter?'Aktiv, tippen zum neu Anmelden':((S.verbunden||[]).includes(k)?'Verbunden, tippen zum Wechseln':x.dataset.abo)})}
document.querySelectorAll('[data-anbieter]').forEach(b=>b.onclick=async()=>{
  const k=b.dataset.anbieter;
  if(k!==S.anbieter&&(S.verbunden||[]).includes(k)){const r=await post('anmelden/wechseln',{anbieter:k});
    if(r.ok===false)return fehler('code-fehler',r.fehler);S.anbieter=k;return weiter()}
  $('wahl').hidden=true;$('login').hidden=false;zeige('laedt');zeichne();
  login(await post('anmelden/start',{anbieter:b.dataset.anbieter}));if(!$('fehlschlag').hidden)return;beobachten()});
$('link').onclick=()=>oeffne($('link').dataset.url);
$('kopieren').onclick=async()=>{try{await navigator.clipboard.writeText($('geraetecode').textContent);$('kopieren').textContent='Kopiert'}catch(e){}};
// Am Handy fragt das System beim Lesen der Zwischenablage noch einmal nach, dort reicht Lang-Tippen ins Feld.
if(!(navigator.clipboard&&navigator.clipboard.readText)||matchMedia('(pointer: coarse)').matches)$('einfuegen').hidden=true;
$('einfuegen').onclick=async()=>{try{$('code').value=(await navigator.clipboard.readText()).trim();$('senden').click()}catch(e){$('code').focus()}};
$('code').addEventListener('paste',()=>setTimeout(()=>{if($('code').value.trim().length>=20&&!$('senden').disabled)$('senden').click()},60));
$('senden').onclick=async()=>{const c=$('code').value.trim();if(!c)return;$('senden').disabled=true;
  const s=await post('anmelden/code',{code:c});$('senden').disabled=false;
  if(s.ok===false)return fehler('code-fehler',s.fehler);fehler('code-fehler','');login(s);beobachten()};
$('nochmal').onclick=zuruecksetzen;
$('google-los').onclick=async()=>{
  const dienste=[...document.querySelectorAll('[data-dienst]')].filter(x=>x.checked).map(x=>x.dataset.dienst);
  $('google-los').disabled=true;$('google-los').textContent='Einen Moment …';
  const r=await post('einrichten/google',{konto:$('konto').value.trim(),dienste});
  $('google-los').disabled=false;$('google-los').textContent='Mit Google verbinden';
  if(r.ok===false)return fehler('google-fehler',r.fehler);fehler('google-fehler','');
  $('google-link').dataset.url=r.url;$('google-los').hidden=true;$('google-link-teil').hidden=false;
  $('google-warte').hidden=false;$('google-hinweis').hidden=true;const seit=Date.now();
  clearInterval(googleZeit);googleZeit=setInterval(async()=>{const s=await stand();if(!s)return;
    if(s.google.verbunden){clearInterval(googleZeit);S=s;$('google-los').hidden=false;$('google-link-teil').hidden=true;return weiter()}
    if(s.google.fehler){clearInterval(googleZeit);$('google-los').hidden=false;$('google-link-teil').hidden=true;return fehler('google-fehler',s.google.fehler)}
    if(Date.now()-seit>180000){$('google-warte').hidden=true;$('google-hinweis').hidden=false}},2000)};
$('google-app-aendern').onclick=()=>{clearInterval(googleZeit);appNeu=true;$('google-los').hidden=false;$('google-link-teil').hidden=true;zeichne()};
$('google-link').onclick=()=>oeffne($('google-link').dataset.url);
document.querySelectorAll('[data-extern]').forEach(a=>a.onclick=e=>{e.preventDefault();oeffne(a.href)});
$('rueckruf-kopieren').onclick=async()=>{try{await navigator.clipboard.writeText($('rueckruf').textContent);$('rueckruf-kopieren').textContent='Kopiert'}catch(e){}};
$('google-app-los').onclick=async()=>{$('google-app-los').disabled=true;
  const r=await post('einrichten/google-app',{id:$('google-id').value.trim(),schluessel:$('google-schluessel').value.trim()});
  $('google-app-los').disabled=false;if(r.ok===false)return fehler('google-app-fehler',r.fehler);fehler('google-app-fehler','');
  S.google.app=true;appNeu=false;zeichne()};
$('google-aus').onclick=async()=>{await post('einrichten/google-aus',{});S.google.uebersprungen=true;weiter()};
function chips(id,werte,mehrere,gewaehlt){const box=$(id);box.innerHTML='';for(const w of werte){const c=document.createElement('button');
  c.className='chip';c.type='button';c.textContent=w;c.setAttribute('aria-pressed',String(gewaehlt.includes(w)));
  c.onclick=()=>{if(!mehrere)box.querySelectorAll('.chip').forEach(x=>{if(x!==c)x.setAttribute('aria-pressed','false')});
    c.setAttribute('aria-pressed',String(c.getAttribute('aria-pressed')!=='true'))};box.appendChild(c)}}
const gewaehlt=id=>[...$(id).querySelectorAll('.chip[aria-pressed=true]')].map(x=>x.textContent);
async function whatsappWahl(an){const b=$(an?'whatsapp-ja':'whatsapp-ohne');b.disabled=true;
  const r=await post('einrichten/whatsapp',{an});b.disabled=false;
  if(r.ok===false)return fehler('whatsapp-fehler',r.fehler);fehler('whatsapp-fehler','');
  S.whatsapp=Object.assign(S.whatsapp||{},{wahl:an?'ja':'ohne'});weiter()}
$('whatsapp-ja').onclick=()=>whatsappWahl(true);
$('whatsapp-ohne').onclick=()=>whatsappWahl(false);
function louZeichnen(){const l=S.lou||{},karte=!!S.eingerichtet;
  $('lou-schalter').hidden=!karte;$('lou-an').checked=!!l.an;$('lou-schalter-text').textContent=l.an?'Verbunden, im Chat mit „Hallo Lou“':'Aus';
  $('lou-los').hidden=!!l.an||karte;$('lou-ohne').hidden=karte||!!l.an;fehler('lou-fehler',l.an?'':l.fehler||'')}
async function louVerbinden(){const b=$('lou-los');
  b.disabled=true;b.textContent='Einen Moment …';fehler('lou-fehler','');
  const r=await post('einrichten/lou',{});b.disabled=false;b.textContent='Lou verbinden';
  S.lou=Object.assign(S.lou||{},r.lou||{},r.ok===false?{fehler:r.fehler}:{});if(r.ok===false)return louZeichnen();
  if(S.eingerichtet)zeichne();else weiter()}
$('lou-los').onclick=louVerbinden;
$('lou-an').onchange=async()=>{const x=$('lou-an'),an=x.checked;x.disabled=true;fehler('lou-fehler','');
  $('lou-schalter-text').textContent='Einen Moment …';const r=await post(an?'einrichten/lou':'einrichten/lou-aus',{});x.disabled=false;
  S.lou=Object.assign(S.lou||{},r.lou||{},r.ok===false?{fehler:r.fehler}:{});zeichne()};
$('lou-ohne').onclick=async()=>{await post('einrichten/lou-aus',{});S.lou=Object.assign(S.lou||{},{an:false,wahl:'ohne',fehler:''});weiter()};
function brainZeichnen(){const b=S.brain;$('s-brain').hidden=!b||!S.eingerichtet;if(!b)return;
  $('brain-an').checked=!!b.an;$('brain-teil').hidden=!b.an;
  $('brain-text').textContent=!b.an?'Aus':b.teilen?'Geteilt in Google Drive'+(b.zuletzt?', zuletzt '+wann(b.zuletzt):''):'An, nur auf deinem Server';
  $('brain-teilen').textContent=b.teilen?'Nicht mehr teilen':'In Google Drive teilen';
  $('brain-hinweis').textContent=b.teilen?'Andere lesen mit, Änderungen im Drive kommen nicht zurück. Freigeben kannst du den Ordner in Google Drive.':
    b.moeglich?'Der Ordner „'+(b.ordner||'Brain')+'“ kommt in dein Google Drive, jede Nacht neu. Andere lesen mit, Änderungen im Drive kommen nicht zurück.':
    'Zum Teilen verbinde zuerst Google mit Mail, darin steckt Drive.';
  fehler('brain-fehler',b.fehler)}
$('brain-an').onchange=async()=>{const x=$('brain-an'),an=x.checked;x.disabled=true;
  const r=await post('brain/schalten',{an});x.disabled=false;
  if(r.ok===false){x.checked=!an;return fehler('brain-fehler',r.fehler)}S.brain=r.brain||S.brain;brainZeichnen()};
$('brain-teilen').onclick=async()=>{const b=S.brain,k=$('brain-teilen');
  if(!b.teilen&&!b.moeglich){if(S.google.moeglich)return oeffnen('google');return fehler('brain-fehler','Teilen geht nur über Google Drive, das fehlt auf diesem Server.')}
  k.disabled=true;k.textContent='Einen Moment …';fehler('brain-fehler','');
  const r=await post('brain/teilen',{an:!b.teilen});k.disabled=false;
  if(r.brain)S.brain=r.brain;brainZeichnen();if(r.ok===false)fehler('brain-fehler',r.fehler)};
function fragebogenFuellen(){const f=S.fragebogen||{antworten:{},hilfe:[],ton:[]},a=f.antworten||{};
  for(const k of FELDER)$('f-'+k).value=a[k]||'';
  chips('f-hilfe',f.hilfe||[],true,a.hilfe||[]);chips('f-ton',f.ton||[],false,a.ton?[a.ton]:[])}
$('profil-weiter').onclick=async()=>{const a={};for(const k of FELDER)a[k]=$('f-'+k).value.trim();
  a.hilfe=gewaehlt('f-hilfe');a.ton=gewaehlt('f-ton')[0]||'';
  $('profil-weiter').disabled=true;const r=await post('einrichten/fragebogen',{antworten:a});$('profil-weiter').disabled=false;
  if(r.ok===false)return fehler('profil-fehler',r.fehler);fehler('profil-fehler','');
  S.fragebogen=Object.assign(S.fragebogen||{},{erledigt:true,antworten:a});weiter()};
$('profil-aus').onclick=async()=>{await post('einrichten/fragebogen-aus',{});
  S.fragebogen=Object.assign(S.fragebogen||{},{erledigt:true});weiter()};
const ABLAGE={drive:'Google Drive',dropbox:'Dropbox',onedrive:'OneDrive'};let ablageZeit=null;
function wann(s){const d=new Date(s*1000),tag=d.toDateString()===new Date().toDateString()?'heute':
  d.toLocaleDateString('de-DE',{day:'numeric',month:'long'});return tag+' um '+d.toLocaleTimeString('de-DE',{hour:'2-digit',minute:'2-digit'})}
function ablageZeichnen(){const a=S.ablage;if(!a)return;
  const ziele=Object.keys(ABLAGE).filter(k=>a.angebot.includes(k)||(k==='drive'&&S.google.moeglich));
  $('s-ablage').hidden=!ziele.length&&!a.ziel;
  $('ablage-text').textContent=a.ziel?ABLAGE[a.ziel]+(a.zuletzt?', zuletzt '+wann(a.zuletzt):', die erste Kopie läuft'):'Nur auf deinem Server';
  fehler('ablage-fehler',a.fehler);$('ablage-aus').hidden=!a.ziel;
  const box=$('ablage-wahl');box.innerHTML='';
  for(const k of ziele){const b=document.createElement('button'),fehlt=!a.angebot.includes(k);b.className='zeile';b.type='button';
    b.innerHTML='<span class="kachel'+(k===a.ziel?' gruen':'')+'">'+ORDNER+'</span><span><b></b><small></small></span>';
    b.querySelector('b').textContent=ABLAGE[k];
    b.querySelector('small').textContent=k===a.ziel?'Aktiv, tippen zum neu Verbinden':fehlt?'Verbinde zuerst Google mit Mail, darin steckt Drive':'Tippen zum Verbinden';
    b.onclick=()=>ablageWaehlen(k,fehlt);box.appendChild(b)}}
function geraetZeigen(g){$('ablage-geraetecode').textContent=g.code;$('ablage-ms').dataset.url=g.url;$('ablage-geraet').hidden=false}
function ablageBeobachten(){clearInterval(ablageZeit);ablageZeit=setInterval(async()=>{const s=await stand();if(!s||!s.ablage)return;
  if(!s.ablage.geraet){clearInterval(ablageZeit);$('ablage-geraet').hidden=true;S.ablage=s.ablage;ablageZeichnen()}},2000)}
async function ablageWaehlen(k,fehlt){if(fehlt)return oeffnen('google');
  fehler('ablage-fehler','');$('ablage-dropbox').hidden=$('ablage-geraet').hidden=true;
  const r=await post('ablage/verbinden',{ziel:k});if(r.ok===false)return fehler('ablage-fehler',r.fehler);
  if(r.url&&k==='dropbox'){$('ablage-link').dataset.url=r.url;$('ablage-code').value='';return $('ablage-dropbox').hidden=false}
  if(r.code){geraetZeigen(r);return ablageBeobachten()}
  S.ablage=r.ablage;ablageZeichnen()}
$('ablage-link').onclick=()=>oeffne($('ablage-link').dataset.url);
$('ablage-ms').onclick=()=>oeffne($('ablage-ms').dataset.url);
$('ablage-kopieren').onclick=async()=>{try{await navigator.clipboard.writeText($('ablage-geraetecode').textContent);$('ablage-kopieren').textContent='Kopiert'}catch(e){}};
$('ablage-code').addEventListener('paste',()=>setTimeout(()=>{if($('ablage-code').value.trim().length>=20)$('ablage-senden').click()},60));
$('ablage-senden').onclick=async()=>{const c=$('ablage-code').value.trim();if(!c)return;$('ablage-senden').disabled=true;
  const r=await post('ablage/code',{code:c});$('ablage-senden').disabled=false;if(r.ok===false)return fehler('ablage-fehler',r.fehler);
  $('ablage-dropbox').hidden=true;S.ablage=r.ablage;ablageZeichnen()};
$('ablage-aus').onclick=async()=>{const r=await post('ablage/aus',{});if(r.ok===false)return fehler('ablage-fehler',r.fehler);
  S.ablage=r.ablage;ablageZeichnen()};
$('monatlich').onchange=async()=>{const r=await post('ablage/monatlich',{an:$('monatlich').checked});
  if(r.ok===false){$('monatlich').checked=!$('monatlich').checked;fehler('daten-fehler',r.fehler)}};
$('daten-jetzt').onclick=async()=>{const b=$('daten-jetzt');b.disabled=true;b.textContent='Einen Moment …';fehler('daten-fehler','');
  const r=await post('daten/senden',{});b.disabled=false;b.textContent=r.ok===false?'Jetzt in den Chat schicken':'Liegt im Chat';
  if(r.ok===false)fehler('daten-fehler',r.fehler)};
(async()=>{
  if(!t&&tg){try{const r=await fetch('einstellungen/sitzung',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({initData:tg.initData})});const j=await r.json();t=j.t||''}catch(e){}}
  S=t?await stand():null;
  if(!S){$('abgelaufen').textContent=EINSTELLUNGEN?'Öffne die Einstellungen in Telegram über den Knopf Einstellungen neben dem Eingabefeld.':
    'Dieser Link ist abgelaufen. Schick deinem Bot /start, dann bekommst du einen neuen.';
    $('abgelaufen').hidden=false;for(const k of STUFEN)$('s-'+k).hidden=true;document.querySelector('.fortschritt').hidden=true;return}
  $('person').value=S.person||'';$('assistent').value=S.assistent||'';$('konto').value=S.google.konto||'';
  if(S.google.dienste.length)document.querySelectorAll('[data-dienst]').forEach(x=>x.checked=S.google.dienste.includes(x.dataset.dienst));
  fragebogenFuellen();louVorab();
  const sprung=new URLSearchParams(location.search).get('wo')||location.hash.slice(1);
  if(S.eingerichtet){document.querySelector('.fortschritt').hidden=true;ablageZeichnen();$('s-daten').hidden=false;
    $('monatlich').checked=!!(S.ablage&&S.ablage.monatlich);
    if(S.ablage&&S.ablage.geraet){geraetZeigen(S.ablage.geraet);ablageBeobachten()}}
  supportZeigen();
  if(['ablage','daten','lou','brain'].includes(sprung)&&S.eingerichtet){zeichne();return $('s-'+sprung).scrollIntoView({block:'start'})}
  if(STUFEN.includes(sprung)&&S.eingerichtet){oeffnen(sprung);return}
  if(S.eingerichtet){offen='';return zeichne()}
  weiter()})();
</script>""".replace("__BOT__", json.dumps(bot or "")).replace("__ORDNER__", json.dumps(ICON["ordner"])) \
        .replace("__EINSTELLUNGEN__", "true" if einstellungen else "false")
    if einstellungen:
        return seite(name, "Einstellungen", "Einstellungen",
                     "Hier änderst du, welche KI antwortet, was dein Assistent sehen darf und wo deine Daten liegen.",
                     inhalt, skript)
    return seite(name, "Einrichten", "Einrichten",
                 "Namen, KI, Kalender und ein paar Fragen, dann meldet sich dein Assistent in Telegram.", inhalt, skript)


def google_fehler(query):
    """Fehler, mit dem Google zurückschickt (?error=…), als Satz für die Person. Ohne error: ''."""
    fehler = (urllib.parse.parse_qs(query or "").get("error") or [""])[0]
    if not fehler:
        return ""
    fehler = re.sub(r"[^a-z_]", "", fehler.lower())[:40]
    return {
        "access_denied": "Google hat den Zugriff nicht erlaubt. Hast du bei Google abgebrochen, versuch es einfach noch "
                         "einmal. Sonst ist deine Google-App noch nicht veröffentlicht.",
        "admin_policy_enforced": "Dein Google-Konto gehört zu einer Firma, die diese App nicht erlaubt. Das kann nur "
                                 "die Person freigeben, die eure Konten verwaltet.",
        "org_internal": "Deine Google-App steht auf „Intern“. Mit einer Gmail-Adresse braucht sie „Extern“.",
        "redirect_uri_mismatch": "Die Weiterleitungsadresse in deiner Google-App stimmt nicht genau. Trag sie noch "
                                 "einmal ein.",
        "invalid_client": "Client-ID oder Clientschlüssel deiner Google-App passen nicht. Trag sie noch einmal ein.",
        "unauthorized_client": "Deine Google-App lässt diese Anmeldung nicht zu. Leg den Client als „Webanwendung“ an.",
        "invalid_scope": "Google kennt eine der Freigaben nicht. Schalte in deinem Projekt Gmail, Kalender, Drive und "
                         "Tasks ein.",
        "server_error": "Google hatte gerade ein Problem. Versuch es in ein paar Minuten noch einmal.",
        "temporarily_unavailable": "Google hatte gerade ein Problem. Versuch es in ein paar Minuten noch einmal.",
    }.get(fehler, f"Google hat die Anmeldung abgebrochen ({fehler or 'ohne Grund'}). Versuch es noch einmal.")


def ergebnisseite(name, bot, gut, grund=""):
    zurueck = f'<a class="knopf hell" href="https://t.me/{html.escape(bot)}">Zurück zu Telegram</a>' if bot else ""
    if gut:
        inhalt = (f'<div class="buehne"><span class="kachel">{ICON["haken"]}</span><b>Verbunden</b>'
                  f'Google ist verbunden, die Einrichtung geht in Telegram weiter.{zurueck}</div>')
        return seite(name, "Google verbunden", "Google", "Kalender und Mail sind verbunden.", inhalt)
    inhalt = (f'<div class="karte"><div class="schritt"><span class="kachel">{ICON["kreuz"]}</span>'
              f'<p>Geh zurück zu Telegram und tipp noch einmal auf „Mit Google verbinden“.</p></div>'
              + (f'<a class="knopf" href="https://t.me/{html.escape(bot)}">Zurück zu Telegram</a>' if bot else "") + '</div>')
    return seite(name, "Google", "Google", grund or "Das hat nicht geklappt, der Link war vielleicht schon alt.", inhalt)


def whatsapp_seite(name, bot):
    """WhatsApp verbinden: Einwilligung, Bau auf dem Server, QR-Code, fertig. Die Seite fragt alle drei Sekunden
    /whatsapp/stand ab und zeigt immer nur den Abschnitt zum aktuellen Schritt."""
    inhalt = f"""
<section id="frage" hidden>
  <div class="karte"><div class="schritt"><span class="kachel">{ICON["sprechblase"]}</span><div><b>Was ich mache</b>
    <p>Jede Nacht lese ich deine Einzelchats und halte zu den Menschen, die dir wichtig sind, kurz fest, was sich tut.
    Morgens sage ich dir, wem du noch antworten wolltest, und du kannst mich fragen, was jemand geschrieben hat.
    Gruppen lasse ich aus. In WhatsApp schreibe ich nie, wir bleiben hier in Telegram.</p></div></div></div>
  <div class="karte"><div class="schritt"><span class="kachel">{ICON["schloss"]}</span><div><b>Gut zu wissen</b>
    <p>Die Verbindung läuft über ein inoffizielles Programm, das sich wie WhatsApp Web anmeldet. WhatsApp sieht so
    etwas nicht gern und kann die Verknüpfung im schlimmsten Fall sperren.</p>
    <p>Deine Kontakte haben nicht zugestimmt. Sag es den Menschen, die dir nah sind, eine Vorlage dafür schreibe ich
    dir gern. Deine Nachrichten liegen verschlüsselt auf deinem Server, niemand sonst kommt dran.</p></div></div></div>
  <p class="fehler" id="start-fehler" hidden></p>
  <button class="knopf" id="start">Einverstanden, verbinden</button>
</section>
<section id="baut" hidden><div class="karte"><div class="schritt"><span class="kachel gruen"><span class="warte"></span></span>
  <div><b>Ich richte WhatsApp ein</b><p>Beim ersten Mal dauert das zehn bis fünfzehn Minuten. Du kannst die Seite
  schließen, im Chat sage ich dir Bescheid, sobald der QR-Code da ist.</p></div></div></div></section>
<section id="qr" hidden><div class="karte">
  <img id="qr-bild" alt="QR-Code für WhatsApp" width="300" height="300"
   style="display:block;margin:0 auto 18px;width:min(300px,100%);height:auto;background:#fff;border-radius:12px">
  <div class="schritt"><span class="kachel">1</span><p>Öffne WhatsApp auf deinem Handy.</p></div>
  <div class="schritt"><span class="kachel">2</span><p>iPhone: Einstellungen. Android: Menü ⋮ oben rechts. Dann
  „Verknüpfte Geräte“ und „Gerät hinzufügen“.</p></div>
  <div class="schritt"><span class="kachel">3</span><p>Halte das Handy auf diesen Code. Er erneuert sich von selbst.</p></div>
  <p style="color:var(--vb-grau);font-size:15px">Hast du nur das Handy, verbindest du mit deiner Telefonnummer.</p>
  <button class="knopf leise" id="nummer-auf">Mit Telefonnummer verbinden</button>
  <div id="nummer-teil" hidden>
    <label>Deine WhatsApp-Nummer<input id="nummer" type="tel" inputmode="tel" autocomplete="tel"
     placeholder="+49 170 1234567" maxlength="24"></label>
    <p class="fehler" id="nummer-fehler" hidden></p>
    <button class="knopf" id="nummer-los">Code holen</button>
    <div id="nummer-ergebnis" hidden>
      <div class="code" id="nummer-code"></div>
      <p>In WhatsApp unter „Verknüpfte Geräte“ auf „Gerät hinzufügen“ tippen, dann unten auf „Stattdessen mit
      Telefonnummer verknüpfen“. Dort gibst du diesen Code ein.</p>
    </div>
  </div>
</div></section>
<section id="verknuepft" hidden><div class="karte"><div class="schritt"><span class="kachel gruen"><span class="warte"></span></span>
  <div><b>Verbunden</b><p>Gerade kommen deine Kontakte an, das dauert zwei, drei Minuten.</p></div></div></div></section>
<section id="fertig" hidden><div class="buehne"><span class="kachel">{ICON["haken"]}</span><b>Fertig</b>
  Im Chat frage ich dich gleich, wer dir nah ist. Ab heute Nacht lese ich mit.
  <button class="knopf hell" id="zu-telegram">Zu Telegram</button></div></section>
<section id="fehler" hidden><div class="karte"><div class="schritt"><span class="kachel">{ICON["kreuz"]}</span>
  <div><b>Das hat nicht geklappt</b><p id="fehler-text"></p></div></div>
  <button class="knopf" id="nochmal">Noch einmal versuchen</button></div></section>"""
    skript = """<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
const BOT=__BOT__,tg=window.Telegram&&Telegram.WebApp&&Telegram.WebApp.initData?Telegram.WebApp:null;
if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#EFF2F6');tg.setBackgroundColor('#EFF2F6')}catch(e){}}
let T=new URLSearchParams(location.search).get('t')||'',neu=false;const $=id=>document.getElementById(id);
const TEILE=['frage','baut','qr','verknuepft','fertig','fehler'];
let qr='';
function zeige(s){TEILE.forEach(n=>$(n).hidden=n!==s)}
async function stand(){
  try{const r=await fetch('whatsapp/stand?t='+encodeURIComponent(T),{cache:'no-store'});const d=await r.json();
    if(!r.ok){
      if(tg&&!neu){neu=true;try{const s=await fetch('whatsapp/sitzung',{method:'POST',headers:{'content-type':'application/json'},
          body:JSON.stringify({initData:tg.initData})});const j=await s.json();if(j.t){T=j.t;return stand()}}catch(e){}}
      $('fehler-text').textContent=d.fehler||'Der Link ist abgelaufen. Schick mir /whatsapp.';$('nochmal').hidden=true;
      return zeige('fehler')}
    if(d.schritt==='qr'&&d.qr&&d.qr!==qr){qr=d.qr;$('qr-bild').src=d.qr}
    if(d.schritt==='fehler'){$('fehler-text').textContent=d.fehler||'';$('nochmal').hidden=false}
    zeige(d.schritt)}catch(e){}
}
async function starten(knopf){
  knopf.disabled=true;$('start-fehler').hidden=true;
  try{const r=await fetch('whatsapp/start',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({t:T})});const d=await r.json();
    if(!d.ok){$('start-fehler').textContent=d.fehler||'Das ging gerade nicht.';$('start-fehler').hidden=false;zeige('frage')}
    else zeige('baut')}
  catch(e){$('start-fehler').textContent='Keine Verbindung zum Server. Versuch es gleich noch einmal.';$('start-fehler').hidden=false}
  knopf.disabled=false;
}
$('nummer-auf').onclick=()=>{$('nummer-teil').hidden=false;$('nummer-auf').hidden=true;$('nummer').focus()};
$('nummer-los').onclick=async()=>{
  const k=$('nummer-los');k.disabled=true;$('nummer-fehler').hidden=true;
  try{const r=await fetch('whatsapp/nummer',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({t:T,nummer:$('nummer').value})});const d=await r.json();
    if(d.ok){$('nummer-code').textContent=d.code;$('nummer-ergebnis').hidden=false}
    else{$('nummer-fehler').textContent=d.fehler||'Das ging gerade nicht.';$('nummer-fehler').hidden=false}}
  catch(e){$('nummer-fehler').textContent='Keine Verbindung zum Server. Versuch es gleich noch einmal.';$('nummer-fehler').hidden=false}
  k.disabled=false;
};
$('start').onclick=()=>starten($('start'));
$('nochmal').onclick=()=>starten($('nochmal'));
$('zu-telegram').onclick=()=>{if(tg)tg.close();else if(BOT)location.href='https://t.me/'+BOT};
stand();setInterval(stand,3000);
</script>""".replace("__BOT__", json.dumps(bot or ""))
    return seite(name, "WhatsApp", "WhatsApp", "Ich lese deine Einzelchats mit, wir schreiben weiter hier in Telegram.",
                 inhalt, skript)


# --- Server --------------------------------------------------------------------

class Pforte:
    """hooks: name(), bot(), link_gueltig(t), fertig(anbieter), google(query) -> bool, stand() -> dict,
    namen(person, assistent) -> fehler, google_start(konto, dienste) -> (url, fehler), google_aus(),
    abschliessen() -> fehler, wa_link_gueltig(t), wa_stand() -> dict, wa_start() -> fehler, wa_nummer(n) -> (code, fehler),
    sitzung(initData) -> t oder "", ablage(aktion, daten) -> (fehler, dict)"""

    def __init__(self, hooks):
        self.hooks, self.anmeldung, self.lock = hooks, None, threading.Lock()

    def starte(self, name):
        with self.lock:
            if self.anmeldung:
                self.anmeldung.beenden()
            self.anmeldung = Anmeldung(name, self.hooks["fertig"])
            a = self.anmeldung
        ende = time.time() + 20   # Link abwarten, damit die Seite ihn gleich zeigen kann
        while time.time() < ende and a.stand == "startet":
            time.sleep(0.2)
        if a.stand == "startet":
            a.beenden()
            a.stand, a.fehler = "fehler", f"{anbieter.name(name)} antwortet gerade nicht. Versuch es gleich noch einmal."
        return a.json()


def handler_fuer(pforte):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "assistent"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def senden(self, code, body, art="application/json"):
            daten = (json.dumps(body) if art == "application/json" else body).encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", f"{art}; charset=utf-8")
            self.send_header("content-length", str(len(daten)))
            self.send_header("cache-control", "no-store")
            self.send_header("referrer-policy", "no-referrer")
            # In Telegram am Handy läuft die Seite als Web App ohne Rahmen, in Telegram Web in einem iframe
            self.send_header("content-security-policy", "frame-ancestors 'self' https://web.telegram.org")
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("strict-transport-security", "max-age=31536000")   # nur über HTTPS wirksam (Caddy)
            self.end_headers()
            self.wfile.write(daten)

        def do_GET(self):
            h = pforte.hooks
            teile = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(teile.query)
            t = (q.get("t") or [""])[0]
            name = h["name"]()
            if teile.path == "/oauth2callback":
                gut = h["google"](teile.query)
                return self.senden(200, ergebnisseite(name, h["bot"](), gut, "" if gut else google_fehler(teile.query)),
                                   "text/html")
            if teile.path == "/einstellungen":   # feste Adresse für den Menüknopf, Zugang über Telegram (initData)
                return self.senden(200, einrichtungsseite(name, h["bot"](), einstellungen=h["stand"]()["eingerichtet"]),
                                   "text/html")
            if teile.path in ("/einrichten", "/anmelden"):
                if not h["link_gueltig"](t):
                    return self.senden(403, seite(name, "Link abgelaufen", "Abgelaufen",
                                                  "Schick deinem Bot /start, dann bekommst du einen neuen Link.", ""),
                                       "text/html")
                return self.senden(200, einrichtungsseite(name, h["bot"]()), "text/html")
            if teile.path in ("/einrichten/stand", "/anmelden/stand"):
                login = pforte.anmeldung.json() if pforte.anmeldung else None
                if not h["link_gueltig"](t):
                    # Nach einer neuen Anmeldung ist der Link verbraucht, die offene Seite braucht nur noch „fertig“.
                    # Sonst nichts ohne gültigen Link: Namen, Google-Adresse, Antworten und Gerätecodes bleiben drin.
                    if login and login["stand"] == "fertig":
                        return self.senden(200, {"login": {"stand": "fertig", "anbieter": login.get("anbieter")}})
                    return self.senden(403, {"ok": False, "fehler": "Der Link ist abgelaufen."})
                return self.senden(200, {**h["stand"](), "login": login})
            if teile.path == "/whatsapp" and "wa_stand" in h:
                if not h["wa_link_gueltig"](t):
                    return self.senden(403, seite(name, "Link abgelaufen", "Abgelaufen",
                                                  "Schick deinem Bot /whatsapp, dann bekommst du einen neuen Link.", ""),
                                       "text/html")
                return self.senden(200, whatsapp_seite(name, h["bot"]()), "text/html")
            if teile.path == "/whatsapp/stand" and "wa_stand" in h:
                if not h["wa_link_gueltig"](t):
                    return self.senden(403, {"ok": False, "fehler": "Der Link ist abgelaufen. Schick deinem Bot /whatsapp."})
                return self.senden(200, h["wa_stand"]())
            self.senden(404, "Nicht gefunden", "text/plain")

        def do_POST(self):
            h = pforte.hooks
            laenge = int(self.headers.get("content-length") or 0)
            if laenge > 16384:
                return self.senden(413, {"ok": False})
            try:
                d = json.loads(self.rfile.read(laenge) or b"{}")
            except ValueError:
                return self.senden(400, {"ok": False})
            if self.path == "/whatsapp/start" and "wa_start" in h:
                if not isinstance(d, dict) or not h["wa_link_gueltig"](str(d.get("t") or "")):
                    return self.senden(403, {"ok": False, "fehler": "Der Link ist abgelaufen. Schick deinem Bot /whatsapp."})
                f = h["wa_start"]()
                return self.senden(200, {"ok": not f, "fehler": f})
            if self.path == "/whatsapp/sitzung" and "wa_sitzung" in h:
                t = h["wa_sitzung"](str(d.get("initData") or "")) if isinstance(d, dict) else ""
                return self.senden(200 if t else 403, {"ok": bool(t), "t": t})
            if self.path == "/whatsapp/nummer" and "wa_nummer" in h:
                if not isinstance(d, dict) or not h["wa_link_gueltig"](str(d.get("t") or "")):
                    return self.senden(403, {"ok": False, "fehler": "Der Link ist abgelaufen. Schick deinem Bot /whatsapp."})
                code, f = h["wa_nummer"](str(d.get("nummer") or "")[:40])
                return self.senden(200, {"ok": bool(code), "code": code, "fehler": f})
            if self.path == "/einstellungen/sitzung":
                t = h["sitzung"](str(d.get("initData") or "")) if isinstance(d, dict) and "sitzung" in h else ""
                return self.senden(200 if t else 403, {"ok": bool(t), "t": t})
            if not isinstance(d, dict) or not h["link_gueltig"](str(d.get("t") or "")):
                return self.senden(403, {"ok": False, "stand": "fehler",
                                         "fehler": "Der Link ist abgelaufen. Schick deinem Bot /start."})
            if self.path == "/anmelden/start":
                name = d.get("anbieter")
                if name not in anbieter.ANBIETER:
                    return self.senden(400, {"ok": False})
                return self.senden(200, pforte.starte(name))
            if self.path == "/anmelden/wechseln":
                f = h["wechseln"](str(d.get("anbieter") or ""))
                return self.senden(200, {"ok": not f, "fehler": f})
            if self.path == "/anmelden/code":
                a = pforte.anmeldung
                if not a or not a.code(str(d.get("code") or "")):
                    return self.senden(200, {"ok": False, "fehler": "Das sieht nicht nach einem Code aus."})
                return self.senden(200, a.json())
            if self.path == "/einrichten/namen":
                f = h["namen"](str(d.get("person") or ""), str(d.get("assistent") or ""))
                return self.senden(200, {"ok": not f, "fehler": f})
            if self.path == "/einrichten/google":
                dienste = d.get("dienste") if isinstance(d.get("dienste"), list) else []
                url, f = h["google_start"](str(d.get("konto") or ""), [str(x) for x in dienste])
                return self.senden(200, {"ok": bool(url), "url": url or "", "fehler": f})
            if self.path == "/einrichten/fragebogen":
                antworten = d.get("antworten") if isinstance(d.get("antworten"), dict) else {}
                f = h["fragebogen"](antworten)
                return self.senden(200, {"ok": not f, "fehler": f})
            if self.path == "/einrichten/fragebogen-aus":
                h["fragebogen_aus"]()
                return self.senden(200, {"ok": True})
            if self.path == "/einrichten/google-app" and "google_app" in h:
                f = h["google_app"](str(d.get("id") or ""), str(d.get("schluessel") or ""))
                return self.senden(200, {"ok": not f, "fehler": f})
            if self.path == "/einrichten/google-aus":
                h["google_aus"]()
                return self.senden(200, {"ok": True})
            if self.path == "/einrichten/whatsapp" and "wa_onboarding" in h:
                f = h["wa_onboarding"](d.get("an") is True)
                return self.senden(200, {"ok": not f, "fehler": f})
            if self.path == "/einrichten/lou" and "lou" in h:
                f = h["lou"](str(d.get("einladung") or "")[:500])
                return self.senden(200, {"ok": not f, "fehler": f, "lou": h["stand"]().get("lou")})
            if self.path == "/einrichten/lou-aus" and "lou_aus" in h:
                f = h["lou_aus"]()
                return self.senden(200, {"ok": not f, "fehler": f, "lou": h["stand"]().get("lou")})
            if self.path in ("/brain/schalten", "/brain/teilen") and "brain_schalten" in h:
                f = h["brain_" + self.path.rsplit("/", 1)[1]](d.get("an") is True)
                return self.senden(200, {"ok": not f, "fehler": f, "brain": h["stand"]().get("brain")})
            if self.path.startswith(("/ablage/", "/daten/")) and "ablage" in h:
                f, mehr = h["ablage"](self.path.split("/", 1)[1], d)
                return self.senden(200, {"ok": not f, "fehler": f, **mehr})
            if self.path == "/einrichten/fertig":
                f = h["abschliessen"]()
                return self.senden(200, {"ok": not f, "fehler": f})
            self.senden(404, {"ok": False})
    return Handler


def starten(port, hooks):
    """Server im Hintergrund, nur auf 127.0.0.1. Rückgabe: (server, pforte)."""
    p = Pforte(hooks)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler_fuer(p))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, p
