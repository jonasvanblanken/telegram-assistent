"""Einrichtungsseite des Assistenten. Läuft im Dienst (assistent.py) auf 127.0.0.1:[web] port, davor Caddy mit HTTPS
unter [web] adresse (https://<box-domain>/<kurzname>, Caddy schneidet /<kurzname> ab). Der Bot öffnet sie als
Web App direkt in Telegram, Anmeldungen bei Anbietern und Google gehen im Browser auf.

  GET  /einrichten?t=<link>        Namen, KI (Claude oder ChatGPT), Kalender und Mail. /anmelden ist dasselbe
  GET  /einrichten/stand?t=<link>  Stand als JSON, mit dem laufenden Login
  POST /einrichten/namen           {t, person, assistent}
  POST /anmelden/start             {t, anbieter}  startet das Login-Programm des Anbieters
  POST /anmelden/code              {t, code}      gibt den Code der Person an das Login-Programm
  POST /einrichten/google          {t, konto, dienste}  Google-Dienst mit den Schaltern starten, Anmeldelink zurück
  POST /einrichten/google-aus      {t}            ohne Google weiter
  POST /einrichten/fertig          {t}            home/ anlegen, der Assistent stellt sich im Chat vor
  GET  /oauth2callback?...         Rückweg von Google, geht an den Google-Dienst der Instanz

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
body{margin:0;background:var(--vb-grund);color:var(--vb-tinte);font:16px/1.5 "Schibsted Grotesk",system-ui,-apple-system,sans-serif;
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
.einfuegen{border:0;border-radius:16px;background:#E7EDFB;color:var(--vb-blau);font-family:inherit;font-weight:600;font-size:15px;
padding:0 16px;margin-bottom:12px;cursor:pointer}
.knopf+.knopf{margin-top:10px}
.knopf.ohne{background:none;color:var(--vb-grau);margin-top:6px}
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
    "kreuz": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" '
             'stroke-linecap="round"><path d="M7 7l10 10M17 7 7 17"/></svg>',
}


def seite(name, titel, h1, h2, inhalt, skript=""):
    e = html.escape
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>{e(titel)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>{STIL}</style></head><body>
<div class="leiste"><span class="zeichen" id="zeichen">{e(name[:1].upper())}</span><span id="name">{e(name)}</span></div>
<main><h1>{e(h1)}</h1><h2>{e(h2)}</h2>{inhalt}</main>{skript}</body></html>"""


def kopf(schritt, icon, titel):
    return (f'<header><span class="kachel">{ICON[icon]}</span><div><b>{titel}</b><small class="erledigt-text"></small></div>'
            f'<button class="aendern" data-schritt="{schritt}" hidden>Ändern</button></header>')


def einrichtungsseite(name, bot):
    e = html.escape
    wahl = "".join(
        f'<button class="zeile" data-anbieter="{k}"><span class="kachel">{ICON["funke"]}</span>'
        f'<span><b>{e(v["name"])}</b><small>{e(v["abo"])}</small></span></button>'
        for k, v in ((k, anbieter.ANBIETER[k]) for k in anbieter.ANGEBOTEN))
    schalter = "".join(
        f'<label class="schalter"><span class="kachel">{ICON[icon]}</span><span><b>{titel}</b><small>{was}</small></span>'
        f'<input type="checkbox" data-dienst="{k}" checked><i aria-hidden="true"></i></label>'
        for k, icon, titel, was in (("kalender", "kalender", "Kalender", "Termine und Aufgaben"),
                                    ("mail", "mail", "Mail", "Lesen, Entwürfe, Drive")))
    inhalt = f"""
<nav class="fortschritt"><span data-zu="namen">Namen</span><span data-zu="ki">KI</span><span data-zu="google">Kalender und Mail</span></nav>
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
    {schalter}
    <label>Dein Google-Konto<input id="konto" type="email" autocomplete="email" inputmode="email" placeholder="name@gmail.com"></label>
    <p class="fehler" id="google-fehler" hidden></p>
    <button class="knopf" id="google-los">Mit Google verbinden</button>
    <div id="google-link-teil" hidden>
      <p>Google warnt gleich, dass es die App nicht kennt, weil sie nur für dich läuft. Tipp dort auf Erweitert und Weiter.</p>
      <button class="knopf" id="google-link">Zu Google</button>
      <p><span class="warte"></span> Warte auf Google …</p>
    </div>
    <button class="knopf ohne" id="google-aus">Ohne Google weiter</button>
  </div>
</section>
<section id="fertig" hidden><div class="buehne"><span class="kachel">{ICON["haken"]}</span><b id="fertig-titel">Fertig</b>
  <span id="fertig-text"></span><button class="knopf hell" id="zu-telegram">Zu Telegram</button></div></section>"""
    skript = """<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
const BOT=__BOT__,tg=window.Telegram&&Telegram.WebApp&&Telegram.WebApp.initData?Telegram.WebApp:null;
if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#EFF2F6');tg.setBackgroundColor('#EFF2F6')}catch(e){}}
const t=new URLSearchParams(location.search).get('t'),$=id=>document.getElementById(id);
const NAMEN={claude:'Claude',chatgpt:'ChatGPT',gemini:'Gemini'},STUFEN=['namen','ki','google'];
let S=null,offen='',zeit=null,googleZeit=null,bearbeitet=false;
async function stand(){try{const r=await fetch('einrichten/stand?t='+encodeURIComponent(t));return r.ok?r.json():null}catch(e){return null}}
async function post(pfad,d){try{const r=await fetch(pfad,{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify(Object.assign({t},d))});return await r.json()}catch(e){return{ok:false,fehler:'Die Verbindung ist weg. Versuch es noch einmal.'}}}
function oeffne(url){if(tg&&tg.openLink)tg.openLink(url);else window.open(url,'_blank','noopener')}
function fehler(id,text){$(id).textContent=text||'';$(id).hidden=!text}
function erledigt(){return{namen:!!(S.person&&S.assistent),ki:!!S.anbieter,
  google:!S.google.moeglich||S.google.verbunden||S.google.uebersprungen}}
function zeichne(){
  if(S.assistent){$('name').textContent=S.assistent;$('zeichen').textContent=S.assistent[0].toUpperCase()}
  const e=erledigt();$('s-google').hidden=!S.google.moeglich;
  document.querySelector('[data-zu=google]').hidden=!S.google.moeglich;
  const texte={namen:S.person&&S.assistent?S.person+' und '+S.assistent:'',ki:NAMEN[S.anbieter]||'',
    google:S.google.verbunden?S.google.konto:(S.google.uebersprungen?'Ohne Google':'')};
  for(const k of STUFEN){const el=$('s-'+k),fertig=e[k]&&k!==offen;
    el.classList.toggle('offen',k===offen);el.classList.toggle('erledigt',fertig);
    el.querySelector('.aendern').hidden=!fertig||!!zeit;el.querySelector('.erledigt-text').textContent=fertig?texte[k]:'';
    const n=document.querySelector('[data-zu='+k+']');n.classList.toggle('aktiv',k===offen);n.classList.toggle('fertig',fertig)}}
function weiter(){
  if(bearbeitet&&S.eingerichtet){offen='';zeichne();return fertig('Gespeichert','Du kannst zurück zu Telegram.')}
  const e=erledigt();offen=STUFEN.find(k=>!e[k])||'';zeichne();if(!offen)abschluss()}
async function abschluss(){
  if(!S.eingerichtet){const r=await post('einrichten/fertig',{});
    if(r.ok===false){offen='ki';zeichne();return fehler('code-fehler',r.fehler)}}
  fertig('Fertig',(S.assistent||'Dein Assistent')+' schreibt dir jetzt in Telegram.')}
function fertig(titel,text){clearInterval(zeit);clearInterval(googleZeit);document.querySelectorAll('.aendern').forEach(b=>b.hidden=true);$('fertig-titel').textContent=titel;
  $('fertig-text').textContent=text;$('fertig').hidden=false;
  for(const k of STUFEN)$('s-'+k).classList.remove('offen');$('fertig').scrollIntoView({behavior:'smooth',block:'center'})}
function oeffnen(k){bearbeitet=true;offen=k;$('fertig').hidden=true;zeichne();if(k==='ki')zuruecksetzen()}
document.querySelectorAll('.aendern').forEach(b=>b.onclick=()=>oeffnen(b.dataset.schritt));
$('zu-telegram').onclick=()=>{if(tg)tg.close();else location.href='https://t.me/'+BOT};
$('namen-weiter').onclick=async()=>{const p=$('person').value.trim(),a=$('assistent').value.trim();
  if(!p||!a)return fehler('namen-fehler','Bitte beide Namen eintragen.');
  $('namen-weiter').disabled=true;const r=await post('einrichten/namen',{person:p,assistent:a});$('namen-weiter').disabled=false;
  if(r.ok===false)return fehler('namen-fehler',r.fehler);fehler('namen-fehler','');S.person=p;S.assistent=a;weiter()};
const zeige=(...ids)=>{for(const id of ['laedt','link-teil','code-teil','pruefen','fehlschlag'])$(id).hidden=!ids.includes(id)};
function zuruecksetzen(){clearInterval(zeit);zeit=null;$('wahl').hidden=false;$('login').hidden=true;$('code').value='';fehler('code-fehler','')}
function login(s){
  if(s.stand==='fertig'){clearInterval(zeit);zeit=null;S.anbieter=s.anbieter;zuruecksetzen();return weiter()}
  if(s.stand==='fehler'||s.ok===false){clearInterval(zeit);zeit=null;$('fehler-text').textContent=s.fehler||'Das hat nicht geklappt.';return zeige('fehlschlag')}
  if(s.stand==='startet'||!s.url)return zeige('laedt');
  const n=NAMEN[s.anbieter]||'';$('link').dataset.url=s.url;
  if(s.anbieter==='chatgpt'){$('link-text').textContent='Melde dich bei OpenAI mit deinem ChatGPT-Konto an und gib diesen Code ein. Danach geht es hier von selbst weiter.';
    $('geraetecode').textContent=s.geraetecode;$('geraetecode').hidden=$('kopieren').hidden=false;$('link').textContent='Zu OpenAI';
    $('pruefen-text').textContent='Warte auf OpenAI …';return zeige('link-teil','pruefen')}
  $('geraetecode').hidden=$('kopieren').hidden=true;$('link').textContent='Bei '+n+' anmelden';
  $('link-text').textContent=s.anbieter==='claude'?'Mit dem Konto, zu dem dein Claude-Abo gehört.':'Mit deinem privaten Google-Konto.';
  $('code-text').textContent='Danach zeigt dir '+n+' einen Code. Kopier ihn und füg ihn hier ein, am Handy mit langem Tippen ins Feld.';
  if(s.stand==='prueft'){$('pruefen-text').textContent='Ich prüfe die Anmeldung …';return zeige('pruefen')}
  zeige('link-teil','code-teil')}
function beobachten(){clearInterval(zeit);zeit=setInterval(async()=>{const s=await stand();if(s&&s.login)login(s.login)},2000)}
document.querySelectorAll('[data-anbieter]').forEach(b=>b.onclick=async()=>{
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
  clearInterval(googleZeit);googleZeit=setInterval(async()=>{const s=await stand();
    if(s&&s.google.verbunden){clearInterval(googleZeit);S=s;$('google-los').hidden=false;$('google-link-teil').hidden=true;weiter()}},2000)};
$('google-link').onclick=()=>oeffne($('google-link').dataset.url);
$('google-aus').onclick=async()=>{await post('einrichten/google-aus',{});S.google.uebersprungen=true;weiter()};
(async()=>{S=await stand();
  if(!S){$('abgelaufen').hidden=false;for(const k of STUFEN)$('s-'+k).hidden=true;return}
  $('person').value=S.person||'';$('assistent').value=S.assistent||'';$('konto').value=S.google.konto||'';
  if(S.google.dienste.length)document.querySelectorAll('[data-dienst]').forEach(x=>x.checked=S.google.dienste.includes(x.dataset.dienst));
  const sprung=location.hash.slice(1);
  if(STUFEN.includes(sprung)&&S.eingerichtet){oeffnen(sprung);return}
  weiter()})();
</script>""".replace("__BOT__", json.dumps(bot or ""))
    return seite(name, "Einrichten", "Einrichten",
                 "Namen, KI, Kalender, dann meldet sich dein Assistent in Telegram.", inhalt, skript)


def ergebnisseite(name, bot, gut):
    zurueck = f'<a class="knopf hell" href="https://t.me/{html.escape(bot)}">Zurück zu Telegram</a>' if bot else ""
    if gut:
        inhalt = (f'<div class="buehne"><span class="kachel">{ICON["haken"]}</span><b>Verbunden</b>'
                  f'Google ist verbunden, die Einrichtung geht in Telegram weiter.{zurueck}</div>')
        return seite(name, "Google verbunden", "Google", "Kalender und Mail sind verbunden.", inhalt)
    inhalt = (f'<div class="karte"><div class="schritt"><span class="kachel">{ICON["kreuz"]}</span>'
              f'<p>Geh zurück zu Telegram und tipp noch einmal auf „Mit Google verbinden“.</p></div>'
              + (f'<a class="knopf" href="https://t.me/{html.escape(bot)}">Zurück zu Telegram</a>' if bot else "") + '</div>')
    return seite(name, "Google", "Google", "Das hat nicht geklappt, der Link war vielleicht schon alt.", inhalt)


# --- Server --------------------------------------------------------------------

class Pforte:
    """hooks: name(), bot(), link_gueltig(t), fertig(anbieter), google(query) -> bool, stand() -> dict,
    namen(person, assistent) -> fehler, google_start(konto, dienste) -> (url, fehler), google_aus(),
    abschliessen() -> fehler"""

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
            self.end_headers()
            self.wfile.write(daten)

        def do_GET(self):
            h = pforte.hooks
            teile = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(teile.query)
            t = (q.get("t") or [""])[0]
            name = h["name"]()
            if teile.path == "/oauth2callback":
                return self.senden(200, ergebnisseite(name, h["bot"](), h["google"](teile.query)), "text/html")
            if teile.path in ("/einrichten", "/anmelden"):
                if not h["link_gueltig"](t):
                    return self.senden(403, seite(name, "Link abgelaufen", "Abgelaufen",
                                                  "Schick deinem Bot /start, dann bekommst du einen neuen Link.", ""),
                                       "text/html")
                return self.senden(200, einrichtungsseite(name, h["bot"]()), "text/html")
            if teile.path in ("/einrichten/stand", "/anmelden/stand"):
                login = pforte.anmeldung.json() if pforte.anmeldung else None
                if not h["link_gueltig"](t) and not (login and login["stand"] == "fertig"):
                    return self.senden(403, {"ok": False, "fehler": "Der Link ist abgelaufen."})
                return self.senden(200, {**h["stand"](), "login": login})
            self.senden(404, "Nicht gefunden", "text/plain")

        def do_POST(self):
            h = pforte.hooks
            laenge = int(self.headers.get("content-length") or 0)
            if laenge > 4096:
                return self.senden(413, {"ok": False})
            try:
                d = json.loads(self.rfile.read(laenge) or b"{}")
            except ValueError:
                return self.senden(400, {"ok": False})
            if not isinstance(d, dict) or not h["link_gueltig"](str(d.get("t") or "")):
                return self.senden(403, {"ok": False, "stand": "fehler",
                                         "fehler": "Der Link ist abgelaufen. Schick deinem Bot /start."})
            if self.path == "/anmelden/start":
                name = d.get("anbieter")
                if name not in anbieter.ANBIETER:
                    return self.senden(400, {"ok": False})
                return self.senden(200, pforte.starte(name))
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
            if self.path == "/einrichten/google-aus":
                h["google_aus"]()
                return self.senden(200, {"ok": True})
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
