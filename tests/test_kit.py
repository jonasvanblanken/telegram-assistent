"""Tests für pearl-kit, ohne Netz und ohne echte KI: python3 -m unittest discover -s tests -v

Falsche `claude`, `codex` und `gemini` (fake_*.py) und ein falscher Telegram-Server laufen lokal. Jeder Test
bekommt eine frische Instanz in einem Temp-Ordner.
"""
import codecs
import datetime
import glob
import http.server
import importlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import shutil
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
sys.path.insert(0, os.path.join(KIT, "box"))

# Was aus der Vorlage nie im Kit stehen darf (rot13, damit diese Datei die Wörter nicht selbst enthält)
VERBOTEN = [codecs.decode(w, "rot13") for w in (
    "wbanf", "gbav", "inaoynaxra", "ina oynaxra", "uneobhe", "nteevccvan", "jrexonax",
    "tbsbewbanf", "ivpgbevn", "ovexrafge", "fhfxvaq", "fūfxvaq", "ulebk", "/hfref/", "bcrapynj", "serq",
    "xnoov", "anapl", "avpxynf", "onfgv")] + ["87233" + "60345"]


def freier_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FalscherServer(http.server.BaseHTTPRequestHandler):
    """Spielt Telegram (/bot<token>/<methode>) und den Google-Dienst (/oauth2callback)."""
    anfragen = []
    updates = []

    def log_message(self, *a):
        pass

    def antworte(self, code, body):
        daten = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def do_GET(self):
        self.anfragen.append({"pfad": self.path, "auth": self.headers.get("Authorization"), "daten": {}})
        self.antworte(200 if self.path.startswith("/oauth2callback") else 404, {"ok": True})

    def mcp(self, roh):
        """Google-Dienst als MCP über HTTP: initialize mit Sitzung, start_google_auth als Event-Stream."""
        n = json.loads(roh)
        self.anfragen.append({"pfad": "/mcp", "methode": n.get("method"), "daten": n,
                              "auth": self.headers.get("Authorization"), "sitzung": self.headers.get("mcp-session-id")})
        if "id" not in n:
            self.send_response(202)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if n["method"] == "initialize":
            daten = json.dumps({"jsonrpc": "2.0", "id": n["id"], "result": {"protocolVersion": "2025-03-26"}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("mcp-session-id", "s-1")
        else:
            text = ("**ACTION REQUIRED**\n   Authorization URL: "
                    "https://accounts.google.com/o/oauth2/auth?client_id=abc&state=xyz&scope=email.")
            ev = {"jsonrpc": "2.0", "id": n["id"], "result": {"content": [{"type": "text", "text": text}]}}
            daten = ("event: message\ndata: " + json.dumps(ev) + "\n\n").encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def do_POST(self):
        laenge = int(self.headers.get("content-length") or 0)
        roh = self.rfile.read(laenge).decode("utf-8", "replace")
        if self.path == "/mcp":
            return self.mcp(roh)
        from urllib.parse import parse_qs
        daten = {k: v[0] for k, v in parse_qs(roh).items()}
        if self.path == "/token":   # Token-Endpunkt von Google: Prüfung der eigenen Google-App
            self.anfragen.append({"pfad": "/token", "daten": daten})
            if daten.get("client_secret", "").startswith("falsch"):
                return self.antworte(401, {"error": "invalid_client"})
            return self.antworte(400, {"error": "invalid_grant"})
        methode = self.path.rsplit("/", 1)[-1]
        self.anfragen.append({"pfad": self.path, "methode": methode, "daten": daten})
        if methode == "getUpdates":
            u, FalscherServer.updates[:] = list(FalscherServer.updates), []
            return self.antworte(200, {"ok": True, "result": u})
        if methode == "getMe":
            return self.antworte(200, {"ok": True, "result": {"username": "test_bot"}})
        self.antworte(200, {"ok": True, "result": {"message_id": 1}})


def server_starten(port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), FalscherServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Instanz:
    """Frische Instanz in einem Temp-Ordner, Umgebung so gesetzt, dass kit und assistent dorthin zeigen."""

    def __init__(self, google=True, port=None, anbieter="claude", web_port=0, einladung=False, kits=""):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        fake = os.path.join(KIT, "tests")
        self.env = {"ASSISTENT_DIR": f"{t}/assistent", "ASSISTENT_STATE": f"{t}/state", "ASSISTENT_DATEN": f"{t}/daten",
                    "ASSISTENT_SECRETS": f"{t}/secrets.env", "ASSISTENT_CLAUDE": f"{fake}/fake_claude.py",
                    "ASSISTENT_CODEX": f"{fake}/fake_codex.py", "ASSISTENT_GEMINI": f"{fake}/fake_gemini.py",
                    "ASSISTENT_KI_BIN": f"{t}/ki-bin", "ASSISTENT_BUENDELN": "0.1", "FAKE_LOG": f"{t}/ki.log",
                    "FAKE_MODUS": "ok", "ASSISTENT_GOOGLE_NEUSTART": "true"}
        self.port = port or freier_port()
        args = [sys.executable, os.path.join(KIT, "einrichten.py"), "--admin", "Betreuer", "--anbieter", anbieter]
        if not einladung:
            args += ["--person", "Testperson", "--telegram-id", "4242", "--assistent", "Testi"]
        if google:
            args += (["--google", "test@example.org"] if not einladung else []) + ["--port", str(self.port)]
        if web_port:
            args += ["--web-adresse", "https://box.example.de/test", "--web-port", str(web_port)]
        if kits:
            args += ["--kits", kits]
        r = subprocess.run(args, env={**os.environ, **self.env}, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        with open(self.env["ASSISTENT_SECRETS"], "w") as f:
            f.write("TG_TOKEN=123:abc\nGOOGLE_MCP_TOKEN=" + "g" * 40 + "\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-test\n"
                    "CLAUDE_TOKEN_SEIT=2025-11-01\n")

    def laden(self, telegram_api):
        os.environ.update(self.env)
        os.environ["ASSISTENT_TELEGRAM_API"] = telegram_api
        os.environ["ASSISTENT_GOOGLE_TOKEN"] = telegram_api + "/token"
        for m in ("kit", "ablage", "anbieter", "pforte", "einrichten", "assistent", "brain"):
            sys.modules.pop(m, None)
        return importlib.import_module("assistent")

    def ki_log(self):
        try:
            return [json.loads(z) for z in open(self.env["FAKE_LOG"], encoding="utf-8")]
        except OSError:
            return []

    def weg(self):
        self.tmp.cleanup()


def dateien_im_kit():
    for pfad in glob.glob(os.path.join(KIT, "**", "*"), recursive=True):
        rel = os.path.relpath(pfad, KIT)
        if os.path.isdir(pfad) or rel.startswith((".git", ".venv")) or "__pycache__" in rel or rel.startswith("tests/ergebnisse"):
            continue
        yield rel, pfad


class Sauberkeit(unittest.TestCase):
    def test_nichts_aus_der_vorlage(self):
        funde = []
        for rel, pfad in dateien_im_kit():
            text = open(pfad, encoding="utf-8", errors="replace").read().lower()
            for w in VERBOTEN:
                if re.search(r"(?<![a-zäöü])" + re.escape(w) + r"(?![a-zäöü])", text):
                    funde.append(f"{rel}: {w}")
        self.assertEqual(funde, [], "Persönliches aus der Vorlage im Kit")

    def test_keine_schluessel(self):
        muster = re.compile(r"sk-ant-(?!oat-test)[A-Za-z0-9_-]{10,}|\d{8,10}:[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{30,}")
        for rel, pfad in dateien_im_kit():
            self.assertIsNone(muster.search(open(pfad, encoding="utf-8", errors="replace").read()), rel)

    def test_skripte_syntax(self):
        for rel, pfad in dateien_im_kit():
            if rel.endswith(".sh"):
                r = subprocess.run(["bash", "-n", pfad], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{rel}: {r.stderr}")
            elif rel.endswith(".py"):
                compile(open(pfad, encoding="utf-8").read(), rel, "exec")


    def test_seiten_skripte_js(self):
        """Jede Seite der Pforte: eingebettetes JavaScript ist gültig (ein \\n im Python-String wird sonst ein echter Umbruch)."""
        node = shutil.which("node") or (os.path.exists("/opt/pearl-kit-ki/bin/node") and "/opt/pearl-kit-ki/bin/node")
        if not node:
            self.skipTest("node fehlt")
        import pforte
        seiten = [pforte.einrichtungsseite("anna", "anna_bot"), pforte.einrichtungsseite("anna", "anna_bot", einstellungen=True),
                  pforte.whatsapp_seite("anna", "anna_bot"), pforte.ergebnisseite("anna", "anna_bot", True),
                  pforte.ergebnisseite("anna", "anna_bot", False, "x")]
        with tempfile.TemporaryDirectory() as tmp:
            for i, html in enumerate(seiten):
                for j, js in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
                    datei = os.path.join(tmp, f"s{i}-{j}.js")
                    open(datei, "w", encoding="utf-8").write(js)
                    r = subprocess.run([node, "--check", datei], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, f"Seite {i}, Skript {j}: {r.stderr[-400:]}")

class Vorlage(unittest.TestCase):
    def pruefe(self, google):
        inst = Instanz(google=google)
        try:
            a = inst.env["ASSISTENT_DIR"]
            for wurzel, _, dateien in os.walk(a):
                for d in dateien:
                    text = open(os.path.join(wurzel, d), encoding="utf-8").read()
                    self.assertNotIn("{{", text, d)
                    self.assertNotIn("<!--", text, d)
                    if not google and d.endswith(".md"):   # nur der Text der Vorlage, nicht die eingesetzten Pfade
                        vorlage = text.replace(KIT, "").replace(inst.tmp.name, "")
                        self.assertNotIn("google", vorlage.lower(), d)
            settings = json.load(open(f"{a}/settings.json"))
            self.assertTrue(settings["autoMemoryDirectory"].endswith("/home/gedaechtnis"))
            mcp = json.load(open(f"{a}/mcp.json"))
            self.assertEqual(bool(mcp["mcpServers"]), google)
            self.assertEqual(os.path.exists(f"{a}/google.env"), google)
            self.assertEqual(oct(os.stat(f"{a}/config.toml").st_mode & 0o777), "0o600")
            # zweiter Lauf überschreibt nichts, was der Assistent gelernt hat
            with open(f"{a}/home/profil/person.md", "a") as f:
                f.write("- gelernt\n")
            subprocess.run([sys.executable, os.path.join(KIT, "einrichten.py"), "--nachziehen"],
                           env={**os.environ, **inst.env}, check=True, capture_output=True)
            self.assertIn("- gelernt", open(f"{a}/home/profil/person.md").read())
        finally:
            inst.weg()

    def test_mit_google(self):
        self.pruefe(True)

    def test_ohne_google(self):
        self.pruefe(False)

    def test_config_fehler(self):
        import kit
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[person]\nname = "A"\ntelegram_id = 1\n[assistent]\nname = "B"\n')
        with self.assertRaises(kit.ConfigFehler) as e:
            kit.lies_config(f.name)
        self.assertIn("[admin] name", str(e.exception))
        os.unlink(f.name)

    def test_kits_wahl(self):
        import kit
        self.assertIsNone(kit.kits_gewaehlt(""))
        self.assertEqual(kit.kits_gewaehlt("ohne"), set())
        self.assertEqual(kit.kits_gewaehlt("whatsapp, ablage"), {"whatsapp", "ablage"})
        self.assertEqual(kit.kits_gewaehlt("lou,brain,kalender"), {"lou", "brain", "kalender"})
        self.assertIs(kit.kits_gewaehlt("whatsapp,lotto"), False)
        inst = Instanz(google=False, kits="whatsapp,lotto")   # unbekannt: die Einrichtung läuft, alles wird angeboten
        try:
            cfg = kit.lies_config(os.path.join(inst.env["ASSISTENT_DIR"], "config.toml"))
            self.assertEqual(cfg["kits"]["wahl"], "")
        finally:
            inst.weg()
        inst = Instanz(google=False, kits="whatsapp")
        try:
            pfad = os.path.join(inst.env["ASSISTENT_DIR"], "config.toml")
            self.assertEqual(kit.lies_config(pfad)["kits"]["wahl"], "whatsapp")
            kit.setze_config("kits", "wahl", "lotto", pfad)
            with self.assertRaises(kit.ConfigFehler):
                kit.lies_config(pfad)
        finally:
            inst.weg()


class Chat:
    """Nachrichten an den Assistenten schicken und lesen, was er über Telegram sendet."""

    def gesendet(self, warten=8):
        ende = time.time() + warten
        while time.time() < ende:
            n = [x for x in FalscherServer.anfragen if x.get("methode") == "sendMessage"]
            if n:
                time.sleep(0.3)
                return [x["daten"]["text"] for x in FalscherServer.anfragen if x.get("methode") == "sendMessage"]
            time.sleep(0.1)
        return []

    def knoepfe(self, warten=8):
        """[(text, knopf, url)] der gesendeten Nachrichten mit Knopf."""
        self.gesendet(warten)
        out = []
        for x in FalscherServer.anfragen:
            if x.get("methode") == "sendMessage" and "inline_keyboard" in x["daten"].get("reply_markup", ""):
                k = json.loads(x["daten"]["reply_markup"])["inline_keyboard"][0][0]
                out.append((x["daten"]["text"], k["text"], (k.get("web_app") or {}).get("url") or k.get("url")))
        return out

    def nachricht(self, text, von=4242, vorname=""):
        frm = {"id": von, **({"first_name": vorname} if vorname else {})}
        self.a.handle({"update_id": 1, "message": {"message_id": 1, "date": int(time.time()), "text": text,
                                                     "from": frm, "chat": {"id": von, "type": "private"}}})


class Dienst(Chat, unittest.TestCase):
    """Ende-zu-Ende mit falschem Claude und falschem Telegram."""

    @classmethod
    def setUpClass(cls):
        cls.port = freier_port()
        cls.srv = server_starten(cls.port)
        cls.inst = Instanz(google=True, port=cls.port)   # Google-Dienst = derselbe falsche Server
        cls.a = cls.inst.laden(f"http://127.0.0.1:{cls.port}")
        threading.Thread(target=cls.a.arbeiter, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.inst.weg()

    def setUp(self):
        FalscherServer.anfragen.clear()
        os.environ["FAKE_MODUS"] = "ok"
        self.a.save({})

    def test_frag_und_rechte(self):
        antwort, sitzung, fehler, kontext = self.a.ki_lauf("[Telegram, jetzt] Testperson: Hallo")
        self.assertIsNone(fehler)
        self.assertIn("Hallo", antwort)
        aufruf = self.inst.ki_log()[-1]
        self.assertIn("auto", aufruf["args"][aufruf["args"].index("--permission-mode") + 1])
        self.assertTrue(aufruf["oauth"])
        self.assertTrue(aufruf["google"])
        self.assertTrue(aufruf["cwd"].endswith("/assistent/home"))

    def test_nachricht_bekommt_antwort(self):
        self.nachricht("Was steht morgen an?")
        texte = self.gesendet()
        self.assertTrue(texte, "keine Antwort gesendet")
        self.assertIn("Was steht morgen an?", texte[-1])
        self.assertIn("Testperson: Was steht morgen an?", self.inst.ki_log()[-1]["prompt"])
        logbuch = open(self.a.logbuch_pfad(), encoding="utf-8").read()
        self.assertIn("Testperson", logbuch)
        self.assertIn("Testi", logbuch)

    def test_fremdes_konto_ignoriert(self):
        n = len(self.inst.ki_log())
        self.nachricht("Hallo", von=999)
        time.sleep(1)
        self.assertEqual(len(self.inst.ki_log()), n)
        self.assertEqual(self.gesendet(warten=0.5), [])

    def test_limit_sperrt_und_meldet(self):
        os.environ["FAKE_MODUS"] = "limit"
        antwort, sid, fehler, _ = self.a.lauf("x", ausweichen=True)
        self.assertTrue(self.a.gesperrt(fehler))
        self.assertGreater(self.a.load()["gesperrt"]["sonnet"], time.time())
        self.assertTrue(any("Kontingent" in t for t in self.gesendet()))
        befund = {b["schluessel"] for b in self.a.pulsbefund()}
        self.assertIn("kontingent", befund)

    def test_anmeldung_kaputt_geht_an_die_wache(self):
        os.environ["FAKE_MODUS"] = "auth"
        _, _, fehler, _ = self.a.lauf("x")
        self.assertIn("Betreuer", self.a.fehlertext(fehler))
        self.assertIn("anmeldung", {b["schluessel"] for b in self.a.pulsbefund()})
        os.environ["FAKE_MODUS"] = "ok"
        self.a.lauf("x")
        self.assertNotIn("anmeldung", {b["schluessel"] for b in self.a.pulsbefund()})

    def test_google_rueckruf_mit_token(self):
        self.nachricht(f"http://localhost:{self.port}/oauth2callback?state=abc&code=4/xyz&scope=email")
        texte = self.gesendet()
        rueck = [x for x in FalscherServer.anfragen if x["pfad"].startswith("/oauth2callback")]
        self.assertEqual(len(rueck), 1)
        self.assertEqual(rueck[0]["auth"], "Bearer " + "g" * 40)
        self.assertIn("code=4/xyz", rueck[0]["pfad"])
        self.assertTrue(any("verbunden" in t for t in texte))

    def test_google_link_direkt_vom_dienst(self):
        self.nachricht("/google")
        texte = self.gesendet()
        self.assertTrue(any('href="https://accounts.google.com/o/oauth2/auth?client_id=abc&amp;state=xyz&amp;scope=email"'
                            in t and "Safari" in t for t in texte), texte)
        aufruf = [x for x in FalscherServer.anfragen if x.get("methode") == "tools/call"]
        self.assertEqual(len(aufruf), 1)
        self.assertEqual(aufruf[0]["sitzung"], "s-1")
        self.assertEqual(aufruf[0]["auth"], "Bearer " + "g" * 40)
        self.assertEqual(aufruf[0]["daten"]["params"]["arguments"]["user_google_email"], "test@example.org")

    def test_google_rueckruf_fremder_port(self):
        self.nachricht("http://localhost:1/oauth2callback?state=abc&code=x")
        self.assertFalse([x for x in FalscherServer.anfragen if x["pfad"].startswith("/oauth2callback")])
        self.assertTrue(any("nicht zu mir" in t for t in self.gesendet()))

    def test_job_still_schickt_nichts(self):
        os.environ["FAKE_MODUS"] = "still"
        self.a.fuehre_job_aus("herzschlag", self.a.jobs()["herzschlag"])
        self.assertEqual(self.gesendet(warten=0.5), [])

    def test_wochenrueckblick_prueft_gedaechtnis(self):
        j = self.a.jobs()["wochenrueckblick"]
        code, ausgabe = self.a.werkzeug(j["pruefen"])
        self.assertEqual(code, 0, ausgabe)

    def test_werkzeug_nur_aus_dem_kit(self):
        self.assertEqual(self.a.werkzeug("werkzeuge/gibtsnicht.py")[0], 2)
        self.assertEqual(self.a.werkzeug("../box/wache.py")[0], 2)

    def test_sicherung_ohne_remote(self):
        self.assertTrue(self.a.sichern(melden=False))
        r = subprocess.run(["git", "-C", self.a.INSTANZ, "log", "--oneline"], capture_output=True, text=True)
        self.assertIn("sicherung", r.stdout)
        r = subprocess.run(["git", "-C", self.a.INSTANZ, "ls-files"], capture_output=True, text=True)
        self.assertNotIn("google.env", r.stdout)

    def test_puls_und_token_alter(self):
        befund = {b["schluessel"] for b in self.a.pulsbefund()}
        self.assertIn("token-alter", befund)   # CLAUDE_TOKEN_SEIT=2025-11-01

    def test_pruefen(self):
        ergebnis = self.a.pruefen()
        self.assertTrue(all(ok for ok, _ in ergebnis), ergebnis)
        self.assertTrue(any("@test_bot" in t for _, t in ergebnis))

    def test_befehle(self):
        self.nachricht("/start")
        self.assertTrue(any("Hallo Testperson, ich bin Testi" in t for t in self.gesendet()))
        FalscherServer.anfragen.clear()
        self.nachricht("/status")
        self.assertTrue(self.gesendet())

    def test_ablegen(self):
        os.makedirs(os.path.join(self.a.EINGANG), exist_ok=True)
        with open(os.path.join(self.a.EINGANG, "foto.jpg"), "wb") as f:
            f.write(b"\xff\xd8test")
        r = subprocess.run([sys.executable, os.path.join(KIT, "werkzeuge", "ablegen.py"), "eingang/foto.jpg", "Rechnung Zahnarzt"],
                           capture_output=True, text=True, cwd=self.a.HOME_DIR)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"leben/ablage/\d{4}/\d{4}-\d\d-\d\d-rechnung-zahnarzt\.jpg")
        r = subprocess.run([sys.executable, os.path.join(KIT, "werkzeuge", "ablegen.py"), "../config.toml", "x"],
                           capture_output=True, text=True, cwd=self.a.HOME_DIR)
        self.assertNotEqual(r.returncode, 0)


class AndereKI(Chat, unittest.TestCase):
    """ChatGPT (Codex) und Gemini als Gehirn: derselbe Ablauf, ihre eigenen Formate."""

    def mit(self, name):
        port = freier_port()
        srv = server_starten(port)
        inst = Instanz(google=True, port=port, anbieter=name)
        self.addCleanup(inst.weg)
        self.addCleanup(srv.shutdown)
        os.environ["FAKE_MODUS"] = "ok"
        FalscherServer.anfragen.clear()
        self.inst, self.a = inst, inst.laden(f"http://127.0.0.1:{port}")
        return self.a

    def lauf_und_fortsetzung(self, name):
        a = self.mit(name)
        os.makedirs(os.path.join(a.HOME_DIR, "gedaechtnis"), exist_ok=True)
        with open(os.path.join(a.HOME_DIR, "gedaechtnis", "MEMORY.md"), "w") as f:
            f.write("- [Hund](hund.md) heißt Bello\n")
        antwort, sid, fehler, kontext = a.ki_lauf("[Telegram, jetzt] Testperson: Hallo")
        self.assertIsNone(fehler)
        self.assertIn("Hallo", antwort)
        self.assertNotIn("Ich schaue", antwort)   # nur die letzte Nachricht des Laufs geht an die Person
        self.assertTrue(sid)
        self.assertGreater(kontext, 0)
        aufruf = self.inst.ki_log()[-1]
        self.assertIn("heißt Bello", aufruf["prompt"])   # Gedächtnis-Index vorweg, das CLI lädt ihn nicht selbst
        self.assertTrue(aufruf["google"])
        self.assertTrue(aufruf["cwd"].endswith("/assistent/home"))
        self.assertTrue(aufruf["home"].startswith(self.inst.env["ASSISTENT_DATEN"]))
        antwort, sid2, fehler, _ = a.ki_lauf("[Telegram, jetzt] Testperson: Und weiter?", sitzung=sid)
        self.assertIsNone(fehler)
        self.assertEqual(sid2, sid)
        self.assertNotIn("Bello", self.inst.ki_log()[-1]["prompt"])
        return a, self.inst.ki_log()

    def test_chatgpt_lauf(self):
        a, aufrufe = self.lauf_und_fortsetzung("chatgpt")
        self.assertEqual(a.KI_NAME, "ChatGPT")
        self.assertIn("exec", aufrufe[0]["args"])
        self.assertNotIn("-m", aufrufe[0]["args"])   # Modell „standard“ heißt: Standard des Abos
        self.assertIn('model_reasoning_effort="medium"', aufrufe[0]["args"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", aufrufe[0]["args"])
        self.assertEqual(aufrufe[1]["args"][-3:-1], ["resume", aufrufe[1]["args"][-2]])
        self.assertTrue(os.path.isfile(os.path.join(self.inst.env["ASSISTENT_DATEN"], "codex", "config.toml")))

    def test_gemini_lauf(self):
        a, aufrufe = self.lauf_und_fortsetzung("gemini")
        self.assertIn("--session-id", aufrufe[0]["args"])
        self.assertIn("--resume", aufrufe[1]["args"])
        self.assertIn("yolo", aufrufe[0]["args"])
        settings = json.load(open(os.path.join(self.inst.env["ASSISTENT_DATEN"], "gemini", ".gemini", "settings.json")))
        self.assertEqual(settings["context"]["fileName"], ["CLAUDE.md"])
        self.assertIn("google", settings["mcpServers"])

    def test_chatgpt_limit(self):
        a = self.mit("chatgpt")
        os.environ["FAKE_MODUS"] = "limit"
        _, _, fehler, _ = a.lauf("x")
        self.assertTrue(a.gesperrt(fehler))
        bis = a.load()["gesperrt"]["standard"] - time.time()
        self.assertTrue(2 * 3600 < bis < 2.5 * 3600, bis)
        self.assertTrue(any("ChatGPT-Kontingent" in t for t in self.gesendet()))

    def test_gemini_limit_weicht_aus(self):
        a = self.mit("gemini")
        os.environ["FAKE_MODUS"] = "limit"
        _, _, fehler, _ = a.lauf("x")
        self.assertTrue(a.gesperrt(fehler))
        self.assertEqual(set(a.load()["gesperrt"]), {"standard", "flash"})
        self.assertIn("-m", self.inst.ki_log()[-1]["args"])   # zweiter Versuch mit flash

    def test_anmeldung_kaputt(self):
        for name in ("chatgpt", "gemini"):
            a = self.mit(name)
            os.environ["FAKE_MODUS"] = "auth"
            _, _, fehler, _ = a.lauf("x")
            self.assertTrue(a.anmeldung_kaputt(fehler), fehler)
            self.assertIn("anmeldung", {b["schluessel"] for b in a.pulsbefund()})

    def test_codex_gespraech_weg(self):
        a = self.mit("chatgpt")
        a.update(sitzung="alt", tag=a.gespraechstag())
        os.environ["FAKE_MODUS"] = "weg"
        antwort, fehler, _ = a.im_gespraech("[Telegram, jetzt] Testperson: Hallo", tippen=False)
        self.assertIsNone(fehler)
        self.assertNotEqual(a.load()["sitzung"], "alt")

    def test_limit_formate(self):
        a = self.mit("chatgpt")
        t = time.time()
        self.assertAlmostEqual(a.limit_bis("Try again in 2 hours 13 minutes.") - t, 2 * 3600 + 13 * 60 + 60, delta=5)
        self.assertAlmostEqual(a.limit_bis("Your quota will reset after 13h21m5s.") - t, 13 * 3600 + 21 * 60 + 65, delta=5)
        um = datetime.datetime.fromtimestamp(a.limit_bis("try again at 3:04 PM"), a.TZ)
        self.assertEqual((um.hour, um.minute), (15, 5))


class Anmeldeseite(Chat, unittest.TestCase):
    """Die Person meldet ihre KI selbst an: Link aus dem Chat, Seite, Login-Programm im Pseudo-Terminal."""

    def setUp(self):
        self.port, self.web = freier_port(), freier_port()
        srv = server_starten(self.port)
        self.inst = Instanz(google=True, port=self.port, anbieter="", web_port=self.web)
        self.a = self.inst.laden(f"http://127.0.0.1:{self.port}")
        self.pf_srv, self.pf = self.a.pforte_starten()
        os.environ["FAKE_MODUS"] = "ok"
        FalscherServer.anfragen.clear()
        self.addCleanup(self.inst.weg)
        self.addCleanup(srv.shutdown)
        self.addCleanup(self.pf_srv.shutdown)
        self.addCleanup(lambda: self.pf.anmeldung and self.pf.anmeldung.beenden())

    def holen(self, pfad):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.web}{pfad}", timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def senden(self, pfad, daten):
        req = urllib.request.Request(f"http://127.0.0.1:{self.web}{pfad}", data=json.dumps(daten).encode(),
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def token(self):
        return self.a.anmeldelink().split("t=", 1)[1]

    def warte(self, t, ziel, sek=15):
        ende = time.time() + sek
        while time.time() < ende:
            s = json.loads(self.holen(f"/einrichten/stand?t={t}")[1])
            if (s.get("login") or {}).get("stand") == ziel:
                return s["login"]
            time.sleep(0.2)
        self.fail(f"Stand {ziel} nicht erreicht: {s}")

    def angemeldet(self, name, sek=5):
        ende = time.time() + sek
        while time.time() < ende and self.a.KI_ANBIETER != name:
            time.sleep(0.1)
        self.assertEqual(self.a.KI_ANBIETER, name)
        self.assertIn(f'anbieter = "{name}"', open(os.path.join(self.inst.env["ASSISTENT_DIR"], "config.toml")).read())

    def test_ohne_ki_kommt_der_link(self):
        self.nachricht("Hallo, bist du da?")
        k = self.knoepfe()
        self.assertEqual(len(k), 1)
        self.assertTrue(k[0][2].startswith("https://box.example.de/test/einrichten?t="), k)
        self.assertTrue(glob.glob(os.path.join(self.a.SCHLANGE, "*-msg.json")))   # die Nachricht wartet
        FalscherServer.anfragen.clear()
        self.nachricht("Hallo?")
        self.assertEqual(self.gesendet(warten=0.5), [])   # höchstens alle zehn Minuten
        self.nachricht("/anmelden")
        self.assertTrue(self.gesendet())

    def test_start_ohne_ki_begruesst_einmal(self):
        self.nachricht("/start")
        texte = self.gesendet()
        self.assertTrue(any("Hallo Testperson" in x for x in texte), texte)
        self.assertTrue(any("/einrichten?t=" in u for _, _, u in self.knoepfe()))
        FalscherServer.anfragen.clear()
        self.a.anmeldung_fertig("claude")
        texte = self.gesendet()
        self.assertTrue(any("mit Claude verbunden" in x for x in texte), texte)
        self.assertFalse(any("Hallo Testperson" in x for x in texte), texte)

    def test_link_wird_geprueft(self):
        self.assertEqual(self.holen("/einrichten?t=falsch")[0], 403)
        self.assertEqual(self.senden("/anmelden/start", {"t": "falsch", "anbieter": "claude"}).get("ok"), False)
        code, seite = self.holen(f"/einrichten?t={self.token()}")
        self.assertEqual(code, 200)
        self.assertEqual(self.holen(f"/anmelden?t={self.token()}")[0], 200)   # alter Pfad bleibt
        for name in ("Claude", "ChatGPT", "Gemini"):
            self.assertIn(name, seite)
        self.assertEqual(self.holen("/gibtsnicht")[0], 404)

    def test_claude_mit_adresse_statt_code(self):
        t = self.token()
        s = self.senden("/anmelden/start", {"t": t, "anbieter": "claude"})
        self.assertEqual(s["stand"], "bereit", s)
        self.assertTrue(s["url"].startswith("https://claude.com/cai/oauth/authorize?"))
        s = self.senden("/anmelden/code", {"t": t, "code": "http://localhost:4567/callback?code=gut&state=zustand"})
        self.assertNotEqual(s.get("ok"), False, s)
        self.warte(t, "fertig")
        self.angemeldet("claude")
        self.assertTrue(self.a.secret("CLAUDE_CODE_OAUTH_TOKEN").startswith("sk-ant-oat01-"))
        texte = self.gesendet()
        self.assertTrue(any("mit Claude verbunden" in x and "Hallo Testperson" in x for x in texte), texte)
        self.assertEqual(self.holen(f"/einrichten?t={t}")[0], 403)   # Link ist danach verbraucht

    def test_claude_falscher_code(self):
        t = self.token()
        self.senden("/anmelden/start", {"t": t, "anbieter": "claude"})
        self.senden("/anmelden/code", {"t": t, "code": "falsch"})
        s = self.warte(t, "fehler")
        self.assertIn("noch einmal", s["fehler"])
        self.assertEqual(self.a.KI_ANBIETER, "")

    def test_chatgpt_mit_geraetecode(self):
        t = self.token()
        s = self.senden("/anmelden/start", {"t": t, "anbieter": "chatgpt"})
        self.assertEqual((s["stand"], s["geraetecode"]), ("prueft", "T3ST-AB12C"), s)
        self.assertEqual(s["url"], "https://auth.openai.com/codex/device")
        self.warte(t, "fertig")
        self.angemeldet("chatgpt")
        self.assertTrue(os.path.isfile(os.path.join(self.inst.env["ASSISTENT_DATEN"], "codex", "auth.json")))

    def test_gemini_mit_code(self):
        t = self.token()
        s = self.senden("/anmelden/start", {"t": t, "anbieter": "gemini"})
        self.assertEqual(s["stand"], "bereit", s)
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", s["url"])
        self.senden("/anmelden/code", {"t": t, "code": " gut "})
        self.warte(t, "fertig")
        self.angemeldet("gemini")
        ende = time.time() + 8
        while time.time() < ende and self.pf.anmeldung.p.poll() is None:
            time.sleep(0.2)
        self.assertIsNotNone(self.pf.anmeldung.p.poll(), "gemini läuft nach der Anmeldung weiter")

    def test_wechsel_setzt_modelle_zurueck(self):
        self.a.anmeldung_fertig("claude")
        self.a.kit.setze_config("ki", "modell", "opus")
        self.a.CFG["ki"]["modell"] = "opus"
        self.a.ki_setzen("claude")
        self.a.update(sitzung="s-claude", tag=self.a.gespraechstag())
        self.a.anmeldung_fertig("gemini")
        self.assertEqual((self.a.CHAT_MODELL, self.a.KLEIN_MODELL), ("standard", "flash"))
        self.assertIsNone(self.a.load()["sitzung"])
        self.assertIn('modell = ""', open(os.path.join(self.inst.env["ASSISTENT_DIR"], "config.toml")).read())

    def test_google_rueckweg_ueber_die_seite(self):
        env = open(os.path.join(self.inst.env["ASSISTENT_DIR"], "google.env")).read()
        self.assertIn("https://box.example.de/test/oauth2callback", env)
        code, seite = self.holen("/oauth2callback?state=abc&code=4/xyz&scope=email")
        self.assertEqual(code, 200)
        self.assertIn("Verbunden", seite)
        self.assertIn("https://t.me/test_bot", seite)
        rueck = [x for x in FalscherServer.anfragen if x["pfad"].startswith("/oauth2callback")]
        self.assertEqual(len(rueck), 1)
        self.assertEqual(rueck[0]["auth"], "Bearer " + "g" * 40)
        self.assertTrue(any("Google ist verbunden" in x for x in self.gesendet()))

    def test_google_fehler_von_google(self):
        """Google schickt ?error=… zurück: die Seite sagt warum, der Google-Dienst bekommt nichts."""
        code, seite = self.holen("/oauth2callback?error=access_denied&state=abc")
        self.assertEqual(code, 200)
        self.assertIn("nicht veröffentlicht", seite)
        self.assertFalse([x for x in FalscherServer.anfragen if x["pfad"].startswith("/oauth2callback")])
        g = self.a.einrichtung_stand()["google"]
        self.assertIn("nicht veröffentlicht", g["fehler"])
        self.assertEqual(g["domain"], "example.de")
        self.assertFalse(g["verbunden"])
        import pforte
        self.assertIn("(unbekannt_xy)", pforte.google_fehler("error=unbekannt_xy"))
        self.assertEqual(pforte.google_fehler("code=4/abc&state=x"), "")
        self.holen("/oauth2callback?state=abc&code=4/xyz&scope=email")   # klappt danach: Fehler weg
        g = self.a.einrichtung_stand()["google"]
        self.assertEqual((g["verbunden"], g["fehler"]), (True, ""))

    def test_google_domain(self):
        for web, domain in (("https://box.example.de/test", "example.de"), ("https://1-2-3-4.sslip.io/mara", "sslip.io"),
                            ("https://assistent.mara.co.uk/x", "mara.co.uk"), ("https://1.2.3.4/x", ""), ("", "")):
            self.a.WEB = web
            self.assertEqual(self.a.google_domain(), domain, web)

    def test_google_abgelaufen(self):
        self.a.update(google_verbunden=time.time())
        self.a.eingerichtet = lambda: True
        gilt = []
        self.a.ablage.google_anmeldung_gilt = lambda warten=10: (gilt.append(1), False)[1]
        g = self.a.einrichtung_stand()["google"]
        self.assertEqual((g["verbunden"], g["abgelaufen"]), (False, True))
        self.a.einrichtung_stand()
        self.assertEqual(len(gilt), 1)   # höchstens alle zehn Minuten

    def test_server_ip(self):
        """Erst die Adresse der Standardroute, dann Metadaten (Hetzner, dann DigitalOcean/IONOS), private nie."""
        from unittest import mock
        a = self.a

        class Sock:
            def __init__(self, ip):
                self.ip = ip

            def __enter__(self):
                return self

            def __exit__(self, *x):
                return False

            def connect(self, ziel):
                pass

            def getsockname(self):
                return (self.ip, 4711)

        def antwort(text, ok=True):
            return types.SimpleNamespace(ok=ok, text=text)

        with mock.patch.object(a.socket, "socket", lambda *x: Sock("95.217.1.2")), \
                mock.patch.object(a.requests, "get", side_effect=AssertionError("keine Metadaten nötig")):
            self.assertEqual(a.server_ip(), "95.217.1.2")
        privat = lambda *x: Sock("10.0.0.5")   # noqa: E731  hinter NAT: Metadaten fragen
        with mock.patch.object(a.socket, "socket", privat), \
                mock.patch.object(a.requests, "get", side_effect=[antwort("", ok=False), antwort("49.12.3.4\n")]) as g:
            self.assertEqual(a.server_ip(), "49.12.3.4")
            self.assertIn("/metadata/v1/interfaces/public/0/ipv4/address", g.call_args_list[1][0][0])
        with mock.patch.object(a.socket, "socket", privat), \
                mock.patch.object(a.requests, "get", side_effect=a.requests.ConnectTimeout()) as g:
            self.assertEqual(a.server_ip(), "")
            self.assertEqual(g.call_count, 1)   # kein Metadaten-Dienst: nicht weiter warten
        with mock.patch.object(a.socket, "socket", privat), \
                mock.patch.object(a.requests, "get", return_value=antwort("192.168.1.9")):
            self.assertEqual(a.server_ip(), "")

    def test_leise_nachricht(self):
        self.a.sende_text("Nachts schiefgegangen", leise=True)
        self.a.sende_knopf("Auch nachts", "Ablage", "https://box.example.de/test/einstellungen", leise=True)
        self.a.sende_text("Tagsüber")
        n = [x["daten"] for x in FalscherServer.anfragen if x.get("methode") == "sendMessage"]
        self.assertEqual([x.get("disable_notification") for x in n], ["true", "true", None])

    def test_google_anleitung_ohne_localhost(self):
        self.nachricht("/google")
        k = self.knoepfe()
        self.assertTrue(any(knopf == "Kalender und Mail" and "/einrichten?t=" in u and u.endswith("&wo=google")
                            for _, knopf, u in k), k)

    def test_code_aus(self):
        import pforte
        self.assertEqual(pforte.code_aus("claude", "http://localhost:1/callback?code=a&state=b"), "a#b")
        self.assertEqual(pforte.code_aus("gemini", " 4/0Ab cd\n"), "4/0Abcd")
        self.assertEqual(pforte.code_aus("gemini", "https://codeassist.google.com/authcode?code=4/0x&scope=y"), "4/0x")

    def test_pruefen_ohne_ki(self):
        ergebnis = self.a.pruefen()
        self.assertTrue(all(ok for ok, _ in ergebnis), ergebnis)
        self.assertTrue(any("Einrichtungsseite" in x for _, x in ergebnis))


    # --- Einstellungen über den Menüknopf, Ablage, Meine Daten -------------------------------------------------

    def init_data(self, konto=4242, token="123:abc", alter=0):
        """initData wie Telegram sie signiert (core.telegram.org/bots/webapps, Validating data)."""
        import hashlib, hmac, urllib.parse
        felder = {"auth_date": str(int(time.time() - alter)), "query_id": "AAH",
                  "user": json.dumps({"id": konto, "first_name": "Mara"}, separators=(",", ":"))}
        text = "\n".join(f"{k}={v}" for k, v in sorted(felder.items()))
        geheim = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        felder["hash"] = hmac.new(geheim, text.encode(), hashlib.sha256).hexdigest()
        return urllib.parse.urlencode(felder)

    def einrichten_fertig(self):
        self.a.name_setzen("Mara", "Lotte")
        self.a.anmeldung_fertig("claude")
        self.assertEqual(self.a.abschliessen(), "")

    def test_einstellungen_ueber_menueknopf(self):
        self.einrichten_fertig()
        status, seite = self.holen("/einstellungen")
        self.assertEqual(status, 200)
        self.assertIn("<h1>Einstellungen</h1>", seite)
        r = self.senden("/einstellungen/sitzung", {"initData": self.init_data()})
        self.assertTrue(r["ok"])
        stand = json.loads(self.holen(f"/einrichten/stand?t={r['t']}")[1])
        self.assertTrue(stand["eingerichtet"])
        self.assertEqual(stand["ablage"]["ziel"], "")
        # fremdes Konto, falscher Bot, veraltet, verändert: kein Zugang
        for daten in (self.init_data(konto=999), self.init_data(token="9:zzz"), self.init_data(alter=2 * 3600),
                      self.init_data().replace("Mara", "Eva"), ""):
            self.assertEqual(self.senden("/einstellungen/sitzung", {"initData": daten}), {"ok": False, "t": ""})

    def test_stand_nur_mit_link(self):
        """Ohne gültigen Link nichts über die Person, nach einer neuen Anmeldung nur „fertig“."""
        self.einrichten_fertig()
        self.assertEqual(self.holen("/einrichten/stand")[0], 403)
        self.assertEqual(self.holen("/einrichten/stand?t=falsch")[0], 403)

        class Fertig:
            def json(self):
                return {"stand": "fertig", "url": "", "geraetecode": "ABCD-1234", "fehler": "", "anbieter": "claude"}

            def beenden(self):
                pass
        self.pf.anmeldung = Fertig()
        code, roh = self.holen("/einrichten/stand")
        self.assertEqual((code, json.loads(roh)), (200, {"login": {"stand": "fertig", "anbieter": "claude"}}))
        self.assertNotIn("Mara", roh)

    def test_whatsapp_link_ueber_telegram(self):
        """Der Link zur WhatsApp-Seite gilt eine Stunde, ein älterer Knopf holt über initData einen neuen."""
        self.einrichten_fertig()
        self.a.ADMIN = ""
        self.a.wa_moeglich = lambda: True
        url = self.a.wa_url()
        t = url.split("t=", 1)[1]
        self.assertTrue(self.a.wa_link_gueltig(t))
        self.assertLessEqual(self.a.load()["wa_link"]["bis"], time.time() + 3600)
        self.a.wa_link_beenden()
        self.assertLessEqual(self.a.load()["wa_link"]["bis"], time.time() + 120)
        self.assertEqual(self.a.wa_sitzung(self.init_data(alter=2 * 3600)), "")
        self.assertEqual(self.a.wa_sitzung(self.init_data(konto=999)), "")
        neu = self.a.wa_sitzung(self.init_data())
        self.assertTrue(neu and neu != t and self.a.wa_link_gueltig(neu))

    def test_kein_token_im_log(self):
        self.a.TELEGRAM_API = f"http://127.0.0.1:{freier_port()}"   # niemand da
        with self.assertRaises(self.a.requests.RequestException) as e:
            self.a.telegram("getMe", timeout=1)
        self.assertNotIn("123:abc", str(e.exception))
        self.assertIn("<token>", str(e.exception))

    def test_menueknopf_beim_start(self):
        FalscherServer.anfragen.clear()
        self.a.menue_setzen()
        knopf = [x["daten"] for x in FalscherServer.anfragen if x.get("methode") == "setChatMenuButton"]
        self.assertEqual(len(knopf), 1)
        self.assertEqual(json.loads(knopf[0]["menu_button"])["web_app"]["url"], "https://box.example.de/test/einstellungen")

    def test_einstellungen_knopf_in_antwort(self):
        FalscherServer.anfragen.clear()
        self.a.antworte("Das stellst du unter Einstellungen um.\n[einstellungen: kalender]")
        n = [x["daten"] for x in FalscherServer.anfragen if x.get("methode") == "sendMessage"]
        self.assertEqual(len(n), 1)
        self.assertNotIn("[einstellungen", n[0]["text"])
        knopf = json.loads(n[0]["reply_markup"])["inline_keyboard"][0][0]
        self.assertEqual(knopf["text"], "Kalender und Mail")
        self.assertEqual(knopf["web_app"]["url"], "https://box.example.de/test/einstellungen?wo=google")

    def test_daten_als_zip(self):
        import zipfile
        self.einrichten_fertig()
        os.makedirs(os.path.join(self.a.HOME_DIR, "eingang"), exist_ok=True)
        open(os.path.join(self.a.HOME_DIR, "eingang", "foto.jpg"), "w").write("x")
        pfad = self.a.ablage.zip_bauen(self.a.HOME_DIR, "Lotte")
        namen = zipfile.ZipFile(pfad).namelist()
        os.unlink(pfad)
        self.assertIn("Lotte/CLAUDE.md", namen)
        self.assertFalse([n for n in namen if "/eingang/" in n])
        FalscherServer.anfragen.clear()
        t = self.token()
        self.assertEqual(self.senden("/daten/senden", {"t": t})["ok"], True)
        self.assertTrue([x for x in FalscherServer.anfragen if x.get("methode") == "sendDocument"])
        self.assertEqual(self.senden("/daten/senden", {"t": "falsch"})["ok"], False)

    def test_ablage_spiegel_mit_rclone(self):
        self.einrichten_fertig()
        ab = self.a.ablage
        bin_ = os.path.join(self.inst.tmp.name, "bin")
        os.makedirs(bin_)
        protokoll = os.path.join(self.inst.tmp.name, "rclone.log")
        with open(os.path.join(bin_, "rclone"), "w") as f:
            f.write(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {protokoll}\ncat \"$2\" >> {protokoll}\n")
        os.chmod(os.path.join(bin_, "rclone"), 0o755)
        alt_path = os.environ["PATH"]
        os.environ["PATH"] = bin_ + os.pathsep + alt_path
        self.addCleanup(lambda: os.environ.__setitem__("PATH", alt_path))
        ab.DROPBOX_APP = "app123"
        url = self.senden("/ablage/verbinden", {"t": self.token(), "ziel": "dropbox"})["url"]
        self.assertIn("client_id=app123", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertNotIn("redirect_uri", url)
        ab.zugang = lambda ziel: ("zugang-xyz", 14400)
        ab.setze(ziel="dropbox")
        ab.spiegeln(self.a.HOME_DIR, "Lotte")
        log = open(protokoll).read()
        self.assertIn("sync", log)
        self.assertIn("ziel:Lotte", log)
        self.assertIn("/eingang/**", log)
        self.assertIn("type = dropbox", log)
        self.assertIn("zugang-xyz", log)
        self.assertTrue(ab.lies()["zuletzt"])
        self.assertFalse(glob.glob(os.path.join(self.a.STATE_DIR, "tmp*", "rclone.conf")))   # Zugang liegt nicht herum
        stand = json.loads(self.holen(f"/einrichten/stand?t={self.token()}")[1])["ablage"]
        self.assertEqual((stand["ziel"], stand["angebot"]), ("dropbox", ["dropbox"]))
        self.assertTrue(self.senden("/ablage/aus", {"t": self.token()})["ok"])
        self.assertEqual(ab.lies().get("ziel"), None)


class Einladung(Chat, unittest.TestCase):
    """Ein-Klick-Einrichtung: Einladungslink, /start bindet das Konto, die Seite fragt Namen, KI und Google ab."""

    def setUp(self):
        self.port, self.web = freier_port(), freier_port()
        srv = server_starten(self.port)
        self.inst = Instanz(google=True, port=self.port, anbieter="", web_port=self.web, einladung=True)
        self.a = self.inst.laden(f"http://127.0.0.1:{self.port}")
        self.pf_srv, self.pf = self.a.pforte_starten()
        os.environ["FAKE_MODUS"] = "ok"
        FalscherServer.anfragen.clear()
        self.addCleanup(self.inst.weg)
        self.addCleanup(srv.shutdown)
        self.addCleanup(self.pf_srv.shutdown)
        self.addCleanup(lambda: self.pf.anmeldung and self.pf.anmeldung.beenden())

    holen, senden, warte, angemeldet = Anmeldeseite.holen, Anmeldeseite.senden, Anmeldeseite.warte, Anmeldeseite.angemeldet
    token = Anmeldeseite.token

    def config(self):
        return open(os.path.join(self.inst.env["ASSISTENT_DIR"], "config.toml"), encoding="utf-8").read()

    def binden(self):
        link = self.a.einladungslink()
        self.assertTrue(link.startswith("https://t.me/test_bot?start="), link)
        self.nachricht("/start " + link.split("start=")[1], von=777, vorname="Mara")
        k = self.knoepfe()
        self.assertTrue(k and k[-1][1] == "Einrichten", k)
        return k[-1][2].split("t=", 1)[1]

    def test_falscher_code_und_fremde_konten(self):
        self.a.einladungslink()
        self.nachricht("/start falsch", von=777, vorname="Mara")
        self.nachricht("Hallo", von=777)
        self.nachricht("/start", von=777)
        g = self.gesendet(warten=0.5)
        self.assertEqual(len(g), 1, g)                     # ein Hinweis auf den Link, nicht mehr
        self.assertIn("QR-Code", str(g[0]))
        self.assertEqual(self.a.PERSON_ID, 0)
        t = self.binden()
        self.assertEqual((self.a.PERSON_ID, self.a.PERSON), (777, "Mara"))
        self.assertIn("telegram_id = 777", self.config())
        self.assertTrue(any("Hallo Mara" in x for x in self.gesendet()))
        self.assertEqual(self.a.einladungslink(), "")   # Code ist verbraucht
        FalscherServer.anfragen.clear()
        self.nachricht("/start " + "x" * 16, von=555)
        self.assertEqual(self.gesendet(warten=0.5), [])
        self.assertEqual(self.holen(f"/einrichten?t={t}")[0], 200)

    def test_ganze_einrichtung(self):
        t = self.binden()
        s = json.loads(self.holen(f"/einrichten/stand?t={t}")[1])
        self.assertEqual((s["person"], s["assistent"], s["eingerichtet"], s["google"]["moeglich"]), ("Mara", "", False, True))
        self.assertEqual(self.senden("/einrichten/namen", {"t": t, "person": "Mara", "assistent": "12"})["ok"], False)
        self.assertEqual(self.senden("/einrichten/namen", {"t": t, "person": "Mara", "assistent": "Lotte"})["ok"], True)
        self.assertIn('name = "Lotte"', self.config())
        self.assertTrue(any(x.get("methode") == "setMyName" for x in FalscherServer.anfragen))

        # Vor dem Abschluss wartet jede Nachricht, die Person bekommt den Knopf
        FalscherServer.anfragen.clear()
        self.nachricht("Hallo?", von=777)
        self.assertEqual(self.gesendet(warten=0.5), [])   # Knopf kam gerade erst, höchstens alle zehn Minuten
        self.assertTrue(glob.glob(os.path.join(self.a.SCHLANGE, "*-msg.json")))
        self.assertEqual(self.senden("/einrichten/fertig", {"t": t})["ok"], False)   # KI fehlt noch

        self.senden("/anmelden/start", {"t": t, "anbieter": "chatgpt"})
        self.warte(t, "fertig")
        self.angemeldet("chatgpt")
        self.assertFalse(self.a.eingerichtet())

        FalscherServer.anfragen.clear()
        # Eigener Server: erst die eigene Google-App der Person, dann das Konto
        s = json.loads(self.holen(f"/einrichten/stand?t={t}")[1])
        self.assertFalse(s["google"]["app"])
        self.assertTrue(s["google"]["rueckruf"].endswith("/oauth2callback"))
        self.assertIn("Google-App", self.senden("/einrichten/google", {"t": t, "konto": "mara@example.org",
                                                                         "dienste": ["kalender"]})["fehler"])
        self.assertEqual(self.senden("/einrichten/google-app", {"t": t, "id": "123", "schluessel": "kurz"})["ok"], False)
        # Testwerte zusammengesetzt, damit kein Secret-Scanner sie für echte Zugangsdaten hält
        cid = "1" * 12 + "-" + "test" * 4 + ".apps.googleusercontent.com"
        self.a.GOOGLE_APP_PAUSE = 0
        r = self.senden("/einrichten/google-app", {"t": t, "id": cid, "schluessel": "falsch" + "x" * 26})
        self.assertEqual(r["ok"], False)
        self.assertIn("kennt diese Client-ID", r["fehler"])
        self.assertFalse(json.loads(self.holen(f"/einrichten/stand?t={t}")[1])["google"]["app"])
        self.assertEqual(self.senden("/einrichten/google-app", {"t": t, "id": cid,
                                                                  "schluessel": "x" * 32})["ok"], True)
        client = os.path.join(self.inst.env["ASSISTENT_DATEN"], "google", "client.json")
        self.assertEqual(oct(os.stat(client).st_mode & 0o777), "0o600")
        web = json.load(open(client))["web"]
        self.assertEqual((web["client_id"], web["redirect_uris"]), (cid, [s["google"]["rueckruf"]]))
        self.assertTrue(json.loads(self.holen(f"/einrichten/stand?t={t}")[1])["google"]["app"])
        self.assertEqual(self.senden("/einrichten/google", {"t": t, "konto": "kein-konto", "dienste": ["kalender"]})["ok"], False)
        r = self.senden("/einrichten/google", {"t": t, "konto": "Mara@Example.org", "dienste": ["kalender"]})
        self.assertTrue(r["ok"] and r["url"].startswith("https://accounts.google.com/"), r)
        genv = open(os.path.join(self.inst.env["ASSISTENT_DIR"], "google.env")).read()
        self.assertIn('GOOGLE_RECHTE="calendar:full tasks:manage"', genv)
        self.assertIn(f"GOOGLE_CLIENT_SECRET_PATH={client}", genv)
        self.assertEqual(self.holen("/oauth2callback?state=abc&code=4/xyz")[0], 200)
        self.assertTrue(json.loads(self.holen(f"/einrichten/stand?t={t}")[1])["google"]["verbunden"])
        self.assertEqual(self.gesendet(warten=0.5), [])   # während der Einrichtung schweigt der Chat

        self.assertEqual(self.senden("/einrichten/fertig", {"t": t})["ok"], True)
        home = open(os.path.join(self.a.HOME_DIR, "CLAUDE.md"), encoding="utf-8").read()
        self.assertIn("Lotte", home)
        self.assertIn("Mara", home)
        texte = self.gesendet()
        self.assertEqual(sum("Fertig, Mara. Ich bin Lotte." in x for x in texte), 1, texte)
        self.assertEqual(self.holen(f"/einrichten?t={t}")[0], 403)   # Link ist verbraucht
        FalscherServer.anfragen.clear()
        self.assertEqual(self.a.abschliessen(), "")
        self.assertEqual(self.gesendet(warten=0.5), [])   # begrüßt nur einmal

    def test_namen_spaeter_aendern(self):
        t = self.binden()
        self.senden("/einrichten/namen", {"t": t, "person": "Mara", "assistent": "Lotte"})
        self.a.anmeldung_fertig("claude")
        self.assertEqual(self.a.abschliessen(), "")
        pfad = os.path.join(self.a.HOME_DIR, "CLAUDE.md")
        self.assertEqual(self.a.name_setzen("Mara", "Ottilie"), "")
        self.assertIn("Ottilie", open(pfad, encoding="utf-8").read())

    def test_zwischen_ki_wechseln(self):
        t = self.binden()
        self.senden("/einrichten/namen", {"t": t, "person": "Mara", "assistent": "Lotte"})
        self.a.anmeldung_fertig("claude")
        self.assertEqual(self.a.abschliessen(), "")
        self.assertEqual(self.a.verbundene(), ["claude"])
        FalscherServer.anfragen.clear()
        self.nachricht("/ki", von=777)
        k = self.knoepfe()
        self.assertIn("Du sprichst gerade mit Claude", k[-1][0])
        self.assertIn("ChatGPT verbinden", k[-1][0])
        self.assertTrue(k[-1][2].endswith("&wo=ki"))
        self.assertEqual(self.senden("/anmelden/wechseln", {"t": t, "anbieter": "chatgpt"}).get("ok"), False)
        os.makedirs(self.a.anbieter.CODEX_HOME, exist_ok=True)   # ChatGPT ist jetzt auch verbunden
        open(os.path.join(self.a.anbieter.CODEX_HOME, "auth.json"), "w").write("{}")
        self.assertEqual(json.loads(self.holen(f"/einrichten/stand?t={self.token()}")[1])["verbunden"], ["claude", "chatgpt"])
        FalscherServer.anfragen.clear()
        self.nachricht("/ki", von=777)
        self.assertTrue(any("/ki_chatgpt" in x for x in self.gesendet()))
        self.nachricht("/ki_chatgpt", von=777)   # kein offenes Gespräch: sofort
        self.assertEqual(self.a.KI_ANBIETER, "chatgpt")
        self.assertEqual(self.senden("/anmelden/wechseln", {"t": self.token(), "anbieter": "claude"}).get("ok"), True)
        self.assertEqual(self.a.KI_ANBIETER, "claude")
        self.a.update(sitzung="s-claude", tag=self.a.gespraechstag())   # offenes Gespräch: über den Arbeiter
        self.nachricht("/ki_chatgpt", von=777)
        self.assertTrue(glob.glob(os.path.join(self.a.SCHLANGE, "*-ki.json")))

    def test_fragebogen(self):
        t = self.binden()
        self.senden("/einrichten/namen", {"t": t, "person": "Mara", "assistent": "Lotte"})
        self.a.anmeldung_fertig("claude")
        self.assertEqual(self.senden("/einrichten/fragebogen", {"t": t, "antworten": {"wohnort": "  "}}).get("ok"), False)
        r = self.senden("/einrichten/fragebogen", {"t": t, "antworten": {
            "wohnort": "Köln", "menschen": "Partner Tim,  Tochter Mia", "hilfe": ["Termine", "Unsinn"], "ton": "Locker"}})
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(any("Antworten bekommen" in x for x in self.gesendet(warten=1)))
        stand = json.loads(self.holen(f"/einrichten/stand?t={t}")[1])["fragebogen"]
        self.assertTrue(stand["erledigt"])
        self.assertEqual(stand["antworten"]["hilfe"], ["Termine"])
        auftrag = json.load(open(glob.glob(os.path.join(self.a.SCHLANGE, "*-job.json"))[0]))["inhalt"]
        self.assertEqual(auftrag[0], "kennenlernen")
        self.assertIn("Partner Tim, Tochter Mia", auftrag[1]["prompt"])
        self.assertIn("Kennenlernwoche bis", auftrag[1]["prompt"])
        FalscherServer.anfragen.clear()
        self.assertEqual(self.a.abschliessen(), "")
        self.assertFalse(any("Fertig, Mara" in x for x in self.gesendet(warten=1)))   # die KI meldet sich selbst

    def test_antwort_mit_knoepfen(self):
        self.binden()
        FalscherServer.anfragen.clear()
        self.a.antworte("Morgen früh kann ich dir deine Termine zeigen.\n\n- Soll ich?\n[knopf: Ja, gern]\n[knopf: Nein]")
        self.gesendet(warten=1)
        n = [x["daten"] for x in FalscherServer.anfragen if x.get("methode") == "sendMessage"][-1]
        self.assertNotIn("knopf", n["text"])
        self.assertEqual(json.loads(n["reply_markup"])["keyboard"], [[{"text": "Ja, gern"}], [{"text": "Nein"}]])

    def test_fragebogen_wird_angeboten(self):
        self.binden()
        self.a.name_setzen("Mara", "Lotte")
        self.a.anmeldung_fertig("claude")
        self.a.abschliessen()
        FalscherServer.anfragen.clear()
        self.a.fragebogen_anbieten()
        k = self.knoepfe()
        self.assertTrue(k and k[-1][1] == "Fragen beantworten" and k[-1][2].endswith("&wo=profil"), k)
        FalscherServer.anfragen.clear()
        self.a.fragebogen_anbieten()   # nur einmal
        self.assertEqual(self.gesendet(warten=0.5), [])

    def test_vorlage_nachziehen(self):
        self.binden()
        self.a.name_setzen("Mara", "Lotte")
        self.a.anmeldung_fertig("claude")
        self.a.abschliessen()
        e = self.a.einrichten
        home = self.a.HOME_DIR
        person, claude = os.path.join(home, "profil", "person.md"), os.path.join(home, "CLAUDE.md")
        open(person, "a", encoding="utf-8").write("- wohnt in Köln\n")
        st = e.stand_lesen()   # so tun, als hätte sich die Vorlage seitdem geändert
        for rel in ("CLAUDE.md", os.path.join("profil", "person.md"), os.path.join("jobs", "morgen.md")):
            st[rel]["vorlage"] = "alt"
        e.schreibe(e.STAND, json.dumps(st))
        open(os.path.join(home, "jobs", "morgen.md"), "w").write("alter Morgen-Check, unverändert seit dem Anlegen")
        st = e.stand_lesen()
        st[os.path.join("jobs", "morgen.md")]["datei"] = e._hash("alter Morgen-Check, unverändert seit dem Anlegen")
        e.schreibe(e.STAND, json.dumps(st))
        os.remove(os.path.join(home, "profil", "fragen.md"))
        cfg = self.a.kit.lies_config()
        neu = e.home_nachziehen(e.werte(cfg), False)
        self.assertIn(os.path.join("jobs", "morgen.md"), neu)
        self.assertIn(os.path.join("profil", "fragen.md"), neu)
        self.assertIn("wohnt in Köln", open(person, encoding="utf-8").read())   # geändert, bleibt
        self.assertIn("Kennenlernwoche", open(os.path.join(home, "jobs", "morgen.md"), encoding="utf-8").read())
        os.remove(e.STAND)   # ältere Instanz ohne Stand: nur CLAUDE.md wird erneuert
        open(claude, "w").write("alte Anweisungen")
        neu = e.home_nachziehen(e.werte(cfg), False)
        self.assertEqual(neu, ["CLAUDE.md"])
        self.assertIn("## Verträge", open(claude, encoding="utf-8").read())
        self.assertIn("wohnt in Köln", open(person, encoding="utf-8").read())

    def test_seite_liegen_geblieben(self):
        self.binden()
        self.a.name_setzen("Mara", "Lotte")
        self.a.anmeldung_fertig("claude")
        self.assertFalse(self.a.liegen_geblieben())
        self.a.update(ki_seit=time.time() - self.a.EINRICHTEN_SPAETESTENS - 5)
        FalscherServer.anfragen.clear()
        self.assertTrue(self.a.liegen_geblieben())
        self.assertTrue(any("Fertig, Mara" in x for x in self.gesendet()))

    def test_einladung_auf_der_kommandozeile(self):
        env = {**os.environ, **self.inst.env, "ASSISTENT_TELEGRAM_API": f"http://127.0.0.1:{self.port}"}
        r = subprocess.run([sys.executable, os.path.join(KIT, "assistent.py"), "einladung"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertTrue(r.stdout.strip().startswith("https://t.me/test_bot?start="), r.stdout + r.stderr)

    def test_seite_hat_alle_schritte(self):
        t = self.binden()
        code, seite = self.holen(f"/einrichten?t={t}")
        self.assertEqual(code, 200)
        for teil in ("s-namen", "s-ki", "s-google", "telegram-web-app.js", "Ohne Google weiter", '"test_bot"',
                     "s-lou", "Lou verbinden", "Ohne Lou weiter",
                     "s-brain", "In Google Drive teilen", "louVorab()", "domain-schritt", "google-hinweis"):
            self.assertIn(teil, seite)
        self.assertNotIn("Ich habe eine Einladung", seite)   # Lou gibt jedem einen Zugang
        self.assertIn('data-anbieter="claude"', seite)
        self.assertNotIn('data-anbieter="gemini"', seite)   # Gemini CLI nimmt seit 18.06.2026 keine privaten Konten


class FalscheLou(http.server.BaseHTTPRequestHandler):
    """Spielt Lou: POST /api/zugang gibt einen Zugang aus, /mcp/<code> antwortet auf initialize, fremde Codes 401."""
    GUT = "testeinladung-0000"
    zugang = True       # False: Lou gibt gerade keinen Zugang aus
    voll = False        # True: zu viele Zugänge von dieser IP (429)
    anfragen = []

    def log_message(self, *a):
        pass

    def antworte(self, code, body):
        daten = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def do_POST(self):
        roh = self.rfile.read(int(self.headers.get("content-length") or 0))
        FalscheLou.anfragen.append(self.path)
        if self.path == "/api/zugang":
            if FalscheLou.voll:
                return self.antworte(429, {"fehler": "Zu viele Anfragen"})
            if not FalscheLou.zugang:
                return self.antworte(503, {"fehler": "gerade nicht"})
            basis = "http://" + self.headers.get("host", "")
            return self.antworte(200, {"code": self.GUT, "mcp": f"{basis}/mcp/{self.GUT}",
                                       "einrichten": f"{basis}/einrichten/{self.GUT}"})
        if self.path == f"/mcp/{self.GUT}":
            n = json.loads(roh)
            return self.antworte(200, {"jsonrpc": "2.0", "id": n.get("id"),
                                       "result": {"protocolVersion": "2025-03-26", "serverInfo": {"name": "lou"}}})
        self.antworte(401 if self.path.startswith("/mcp/") else 404, {"fehler": "Kein Zugang"})


def falsches_brain(ablage):
    """Kleines Brain-Modul nach dem Vertrag (anlegen, zustand, teilen, spiegeln), solange brain.py fehlt."""
    m = types.ModuleType("brain")
    m.BrainFehler = type("BrainFehler", (ablage.AblageFehler,), {})
    m.aufrufe, m.geteilt, m.spiegel_fehler = [], False, ""

    def anlegen(home_dir, werte):
        m.aufrufe.append(("anlegen", werte["PERSON"], werte["BRAIN"]))
        os.makedirs(os.path.join(home_dir, "brain"), exist_ok=True)
        with open(os.path.join(home_dir, "brain", "REGELN.md"), "w") as f:
            f.write("Regeln\n")
        return [os.path.join("brain", "REGELN.md")]

    def zustand(drive_moeglich):
        return {"teilen": m.geteilt, "moeglich": drive_moeglich, "ordner": "Brain", "zuletzt": 0.0, "fehler": ""}

    def teilen(an, drive_moeglich):
        m.aufrufe.append(("teilen", an))
        if an and not drive_moeglich:
            raise m.BrainFehler("Zum Teilen verbinde zuerst Google mit Mail, darin steckt Drive.")
        m.geteilt = an

    def spiegeln(home_dir):
        m.aufrufe.append(("spiegeln",))
        if m.spiegel_fehler:
            raise m.BrainFehler(m.spiegel_fehler)

    m.anlegen, m.zustand, m.teilen, m.spiegeln = anlegen, zustand, teilen, spiegeln
    return m


class LouBasis(Chat):
    """Instanz mit Einrichtungsseite, falschem Lou-Server (ASSISTENT_LOU_URL) und auf Wunsch falschem Brain."""

    kits, einladung = "", False

    def setUp(self):
        self.port, self.web, self.lou_port = freier_port(), freier_port(), freier_port()
        srv = server_starten(self.port)
        lou = http.server.ThreadingHTTPServer(("127.0.0.1", self.lou_port), FalscheLou)
        threading.Thread(target=lou.serve_forever, daemon=True).start()
        FalscheLou.zugang, FalscheLou.voll, FalscheLou.anfragen[:] = True, False, []
        self.inst = Instanz(google=True, port=self.port, anbieter="", web_port=self.web, kits=self.kits,
                            einladung=self.einladung)
        self.inst.env["ASSISTENT_LOU_URL"] = self.lou = f"http://127.0.0.1:{self.lou_port}"
        self.a = self.inst.laden(f"http://127.0.0.1:{self.port}")
        self.pf_srv, self.pf = self.a.pforte_starten()
        os.environ["FAKE_MODUS"] = "ok"
        FalscherServer.anfragen.clear()
        self.addCleanup(self.inst.weg)
        self.addCleanup(srv.shutdown)
        self.addCleanup(lou.shutdown)
        self.addCleanup(self.pf_srv.shutdown)
        self.addCleanup(os.environ.pop, "ASSISTENT_LOU_URL", None)
        self.addCleanup(sys.modules.pop, "brain", None)

    holen, senden, token = Anmeldeseite.holen, Anmeldeseite.senden, Anmeldeseite.token

    def pfad(self, *teile):
        return os.path.join(self.inst.env["ASSISTENT_DIR"], *teile)

    def lies(self, pfad):
        try:
            return open(pfad, encoding="utf-8").read()
        except OSError:
            return ""

    def einrichten_fertig(self):
        self.a.name_setzen("Mara", "Lotte")
        self.a.anmeldung_fertig("claude")
        self.assertEqual(self.a.abschliessen(), "")
        FalscherServer.anfragen.clear()

    def stand(self):
        roh = self.holen(f"/einrichten/stand?t={self.token()}")[1]
        self.assertNotIn(FalscheLou.GUT, roh)   # der Zugang geht nie an die Seite
        return json.loads(roh)

    def brain(self):
        m = falsches_brain(self.a.ablage)
        sys.modules["brain"] = m
        return m

    def pruefe_lou(self, an):
        secrets_ = self.lies(self.inst.env["ASSISTENT_SECRETS"])
        mcp = json.load(open(self.pfad("mcp.json")))["mcpServers"]
        settings = json.load(open(self.pfad("settings.json")))["permissions"]
        codex = self.lies(os.path.join(self.inst.env["ASSISTENT_DATEN"], "codex", "config.toml"))
        gemini = json.load(open(os.path.join(self.inst.env["ASSISTENT_DATEN"], "gemini", ".gemini", "settings.json")))
        gemini = gemini.get("mcpServers") or {}
        home = self.lies(os.path.join(self.a.HOME_DIR, "CLAUDE.md"))
        adresse = f"{self.lou}/mcp/{FalscheLou.GUT}"
        self.assertEqual(f"LOU_CODE={FalscheLou.GUT}" in secrets_, an)
        self.assertEqual(mcp.get("lou"), {"type": "http", "url": adresse} if an else None)
        self.assertEqual("google" in mcp, bool(self.a.kit.lies_config()["google"]["konto"]))   # Google bleibt, wie es war
        self.assertEqual("mcp__lou" in settings["allow"], an)
        self.assertIn(f"Read({self.a.INSTANZ}/mcp.json)", settings["deny"])
        self.assertEqual("[mcp_servers.lou]" in codex and adresse in codex, an)
        self.assertEqual((gemini.get("lou") or {}).get("httpUrl"), adresse if an else None)
        self.assertEqual(self.a.kit_status("lou"), "an" if an else "aus")
        if home:
            self.assertEqual("## Lou" in home and "lou_beginnen" in home, an)
            self.assertIn("[knopf: Lou einschalten]", home)
            self.assertNotIn(FalscheLou.GUT, home)
            self.assertNotIn("<!--", home)



class LouUndBrain(LouBasis, unittest.TestCase):
    """Kits Lou und Brain in den Einstellungen: Lou gegen einen falschen Lou-Server, Brain gegen ein falsches Modul."""

    def test_lou_mit_einem_tipp(self):
        self.einrichten_fertig()
        self.pruefe_lou(False)
        self.assertEqual(self.stand()["lou"], {"an": False, "wahl": "", "fehler": ""})
        self.a.update(sitzung="s-1", tag=self.a.gespraechstag())   # offenes Gespräch: liest CLAUDE.md nicht neu
        r = self.senden("/einrichten/lou", {"t": self.token()})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["lou"], {"an": True, "wahl": "ja", "fehler": ""})
        self.assertEqual(FalscheLou.anfragen, ["/api/zugang", f"/mcp/{FalscheLou.GUT}"])
        self.pruefe_lou(True)
        self.assertIn("mcp.json", self.lies(self.pfad(".gitignore")))
        self.assertTrue(glob.glob(os.path.join(self.a.SCHLANGE, "*-wechsel.json")))
        self.assertTrue(any("Hallo Lou" in x for x in self.gesendet()))
        self.assertEqual(self.stand()["lou"]["an"], True)
        self.assertTrue(self.a.sichern(melden=False))   # der Zugang landet nie im Git-Stand
        r = subprocess.run(["git", "-C", self.a.INSTANZ, "ls-files"], capture_output=True, text=True)
        self.assertNotIn("mcp.json", r.stdout)
        # [datei: …] auf mcp.json geht nicht raus
        self.assertEqual(self.a.dateien_aus(f"[datei: {self.pfad('mcp.json')}]")[1], [])
        # Schalter aus: Zugang und Server weg
        r = self.senden("/einrichten/lou-aus", {"t": self.token()})
        self.assertEqual(r["lou"], {"an": False, "wahl": "ohne", "fehler": ""})
        self.pruefe_lou(False)
        self.assertNotIn("LOU_CODE", self.lies(self.inst.env["ASSISTENT_SECRETS"]))

    def test_lou_mit_einladung(self):
        self.einrichten_fertig()
        t = self.token()
        r = self.senden("/einrichten/lou", {"t": t, "einladung": "irgendwas"})
        self.assertIn("nicht nach einer Einladung", r["fehler"])
        r = self.senden("/einrichten/lou", {"t": t, "einladung": "testeinladung-9999"})
        self.assertEqual((r["ok"], r["lou"]["an"]), (False, False))
        self.assertIn("kennt Lou nicht", r["fehler"])
        self.pruefe_lou(False)
        r = self.senden("/einrichten/lou", {"t": t, "einladung": f" https://hellolou.app/einrichten/{FalscheLou.GUT} "})
        self.assertTrue(r["ok"], r)
        self.assertNotIn("/api/zugang", FalscheLou.anfragen)   # mit Einladung holt der Server keinen neuen Zugang
        self.pruefe_lou(True)
        self.assertEqual(self.senden("/einrichten/lou", {"t": "falsch"})["ok"], False)

    def test_lou_nicht_erreichbar(self):
        self.einrichten_fertig()
        FalscheLou.zugang = False
        r = self.senden("/einrichten/lou", {"t": self.token()})
        self.assertEqual((r["ok"], r["fehler"]), (False, self.a.LOU_WEG))
        self.assertEqual(self.stand()["lou"]["fehler"], self.a.LOU_WEG)
        self.pruefe_lou(False)
        os.environ["ASSISTENT_LOU_URL"] = f"http://127.0.0.1:{freier_port()}"   # niemand da
        self.assertEqual(self.a.lou_verbinden(), self.a.LOU_WEG)
        os.environ["ASSISTENT_LOU_URL"] = self.lou
        FalscheLou.zugang = True
        self.assertEqual(self.a.lou_verbinden(), "")   # noch einmal: klappt
        self.assertEqual(self.stand()["lou"]["fehler"], "")

    def test_lou_knopf_im_chat(self):
        self.einrichten_fertig()
        self.a.antworte("Lou ist noch aus.\n[knopf: Lou einschalten]")
        n = [x["daten"] for x in FalscherServer.anfragen if x.get("methode") == "sendMessage"][-1]
        self.assertEqual(json.loads(n["reply_markup"])["keyboard"], [[{"text": "Lou einschalten"}]])
        FalscherServer.anfragen.clear()
        self.nachricht("Lou einschalten")
        ende = time.time() + 10
        while time.time() < ende and self.a.kit_status("lou") != "an":
            time.sleep(0.1)
        self.pruefe_lou(True)
        self.assertTrue(any("Lou ist verbunden" in x for x in self.gesendet()))
        self.assertFalse(glob.glob(os.path.join(self.a.SCHLANGE, "*-msg.json")))   # geht nicht an die KI
        FalscherServer.anfragen.clear()
        self.nachricht("Lou einschalten")
        self.assertTrue(any("schon verbunden" in x for x in self.gesendet()))

    def test_lou_neuer_code(self):
        """Lou ist an, kennt den Code aber nicht mehr (401): Knopf und Nacht holen still einen neuen."""
        self.einrichten_fertig()
        self.assertTrue(self.senden("/einrichten/lou", {"t": self.token()})["ok"])
        self.a.kit.setze_secret("LOU_CODE", "testeinladung-1111")   # Lou kennt ihn nicht
        FalscherServer.anfragen.clear()
        FalscheLou.anfragen[:] = []
        self.nachricht("Lou einschalten")
        ende = time.time() + 10
        while time.time() < ende and not any("wieder verbunden" in x for x in self.gesendet(0)):
            time.sleep(0.1)
        self.assertTrue(any("wieder verbunden" in x for x in self.gesendet()))
        self.assertEqual(FalscheLou.anfragen[:2], ["/mcp/testeinladung-1111", "/api/zugang"])
        self.pruefe_lou(True)
        # nachts: still, ohne Nachricht
        self.a.kit.setze_secret("LOU_CODE", "testeinladung-2222")
        FalscherServer.anfragen.clear()
        self.a.lou_nachts()
        self.pruefe_lou(True)
        self.assertEqual(self.gesendet(warten=0.3), [])
        FalscheLou.anfragen[:] = []
        self.a.lou_nachts()   # gilt: kein neuer Zugang
        self.assertEqual(FalscheLou.anfragen, [f"/mcp/{FalscheLou.GUT}"])

    def test_lou_zu_viele_anfragen(self):
        self.einrichten_fertig()
        FalscheLou.voll = True
        r = self.senden("/einrichten/lou", {"t": self.token()})
        self.assertEqual((r["ok"], r["fehler"]), (False, self.a.LOU_VOLL))
        self.pruefe_lou(False)

    def test_einstellungen_knoepfe(self):
        for bereich, text in (("lou", "Lou"), ("brain", "Brain")):
            FalscherServer.anfragen.clear()
            self.a.antworte(f"Das geht unter Einstellungen.\n[einstellungen: {bereich}]")
            k = self.knoepfe(1)[-1]
            self.assertEqual((k[1], k[2]), (text, f"https://box.example.de/test/einstellungen?wo={bereich}"))

    def test_brain_schalten_und_teilen(self):
        self.einrichten_fertig()
        b = self.brain()
        s = self.stand()["brain"]
        self.assertEqual((s["an"], s["teilen"], s["moeglich"]), (False, False, False))
        self.assertEqual(self.senden("/brain/teilen", {"t": self.token(), "an": True})["fehler"], "Schalte zuerst Brain ein.")
        self.a.update(sitzung="s-1", tag=self.a.gespraechstag())
        r = self.senden("/brain/schalten", {"t": self.token(), "an": True})
        self.assertTrue(r["ok"] and r["brain"]["an"], r)
        self.assertEqual(b.aufrufe, [("anlegen", "Mara", "an")])
        home = self.lies(os.path.join(self.a.HOME_DIR, "CLAUDE.md"))
        self.assertIn("`brain/REGELN.md`", home)
        self.assertEqual(self.a.kit_status("brain"), "an")
        self.assertTrue(any("welches Thema" in x for x in self.gesendet()))
        self.assertTrue(glob.glob(os.path.join(self.a.SCHLANGE, "*-wechsel.json")))
        # Teilen braucht Drive
        r = self.senden("/brain/teilen", {"t": self.token(), "an": True})
        self.assertEqual(r["ok"], False)
        self.assertIn("Drive", r["fehler"])
        self.a.update(google_verbunden=time.time())
        FalscherServer.anfragen.clear()
        r = self.senden("/brain/teilen", {"t": self.token(), "an": True})
        self.assertTrue(r["ok"] and r["brain"]["teilen"] and r["brain"]["moeglich"], r)
        self.assertTrue(any("Google Drive" in x for x in self.gesendet()))
        # Nachts: spiegeln, ein Fehler kommt einmal mit Knopf zur Karte
        t1 = datetime.datetime(2026, 11, 25, 5, 0, tzinfo=self.a.TZ)
        self.assertTrue(self.a.ablage_faellig({}, t1))
        b.spiegel_fehler = "Google Drive ist voll."
        FalscherServer.anfragen.clear()
        self.a.ablage_nachts(t1)
        self.a.ablage_nachts(t1)   # derselbe Tag: nicht noch einmal
        self.a.ablage_nachts(t1 + datetime.timedelta(days=1))   # derselbe Fehler: keine zweite Nachricht
        self.assertEqual(b.aufrufe.count(("spiegeln",)), 2)
        k = self.knoepfe(1)
        self.assertEqual([(x[0], x[1]) for x in k], [("Google Drive ist voll.", "Brain")])
        self.assertTrue(k[0][2].endswith("?wo=brain"))
        self.assertFalse(self.a.ablage_faellig(self.a.load(), t1 + datetime.timedelta(days=1)))
        b.spiegel_fehler = ""
        self.a.ablage_nachts(t1 + datetime.timedelta(days=2))
        self.assertIsNone(self.a.load().get("brain_fehler"))
        # Aus: Teilen endet, der Block verschwindet, home/brain/ bleibt
        r = self.senden("/brain/schalten", {"t": self.token(), "an": False})
        self.assertTrue(r["ok"] and not r["brain"]["an"], r)
        self.assertIn(("teilen", False), b.aufrufe)
        self.assertNotIn("brain/REGELN.md", self.lies(os.path.join(self.a.HOME_DIR, "CLAUDE.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.a.HOME_DIR, "brain", "REGELN.md")))

    def test_brain_fehlt_in_dieser_fassung(self):
        if importlib.util.find_spec("brain"):
            self.skipTest("brain.py ist da")
        self.einrichten_fertig()
        self.assertIsNone(self.stand()["brain"])
        self.assertIn("nächsten Stand", self.a.brain_schalten(True))
        r = subprocess.run([sys.executable, os.path.join(KIT, "einrichten.py"), "--brain", "an"],
                           env={**os.environ, **self.inst.env}, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Brain fehlt", r.stderr)

    @unittest.skipUnless(importlib.util.find_spec("brain"), "brain.py kommt mit dem Modul-Zweig")
    def test_brain_echt(self):
        self.einrichten_fertig()
        self.assertEqual(self.a.brain_schalten(True), "")
        self.assertTrue(os.path.isdir(os.path.join(self.a.HOME_DIR, "brain")))
        s = self.stand()["brain"]
        self.assertTrue({"an", "teilen", "moeglich", "ordner"} <= set(s), s)
        self.assertTrue(s["an"])
        self.assertIn("brain/REGELN.md", self.lies(os.path.join(self.a.HOME_DIR, "CLAUDE.md")))

    def test_kommandozeile(self):
        env = {**os.environ, **self.inst.env}
        einrichten = [sys.executable, os.path.join(KIT, "einrichten.py")]
        r = subprocess.run(einrichten + ["--lou", "an"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Zugang", r.stderr)
        self.einrichten_fertig()
        self.assertEqual(self.a.lou_verbinden(), "")
        r = subprocess.run(einrichten + ["--lou", "aus"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("lou", json.load(open(self.pfad("mcp.json")))["mcpServers"])
        # Update mit WhatsApp an: ein Werkzeug ohne „nachziehen“ hält das Update nicht auf
        self.a.kit.setze_config("whatsapp", "status", "an")
        r = subprocess.run(einrichten + ["--nachziehen"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)


class LouVorabAngehakt(LouBasis, unittest.TestCase):
    """Lou und Brain auf der Website angehakt: Lou verbindet sich von selbst, Brain ist nach der Einrichtung an."""

    kits, einladung = "lou,brain", True
    binden = Einladung.binden

    def setUp(self):
        super().setUp()
        self.binden()
        FalscherServer.anfragen.clear()

    def test_von_selbst(self):
        b = self.brain()
        s = self.stand()
        self.assertEqual((s["kits"], s["lou"]), (["brain", "lou"], {"an": False, "wahl": "", "fehler": ""}))
        self.a.name_setzen("Mara", "Lotte")
        self.a.anmeldung_fertig("claude")
        self.assertEqual(self.a.abschliessen(), "")
        self.pruefe_lou(True)
        self.assertEqual(self.a.kit_status("brain"), "an")
        self.assertEqual(b.aufrufe, [("anlegen", "Mara", "an")])
        self.assertIn("`brain/REGELN.md`", self.lies(os.path.join(self.a.HOME_DIR, "CLAUDE.md")))
        gruss = [x for x in self.gesendet() if "Fertig, Mara" in x]
        self.assertEqual(len(gruss), 1, gruss)
        self.assertIn("Hallo Lou", gruss[0])
        self.assertIn("welches Thema", gruss[0])

    def test_lou_klappt_nicht(self):
        FalscheLou.zugang = False
        r = self.senden("/einrichten/lou", {"t": self.token()})   # so verbindet die Seite im Hintergrund
        self.assertEqual(r["lou"]["fehler"], self.a.LOU_WEG)
        self.a.name_setzen("Mara", "Lotte")
        self.a.anmeldung_fertig("claude")
        self.assertEqual(self.a.abschliessen(), "")   # die Einrichtung geht trotzdem zu Ende
        self.pruefe_lou(False)
        k = self.knoepfe()
        self.assertTrue(any("Lou ließ sich gerade nicht verbinden" in x[0] and x[1] == "Lou" and x[2].endswith("?wo=lou")
                            for x in k), k)

    def test_ohne_lou_weiter(self):
        FalscheLou.zugang = False
        self.senden("/einrichten/lou", {"t": self.token()})
        self.assertEqual(self.senden("/einrichten/lou-aus", {"t": self.token()})["lou"]["wahl"], "ohne")
        FalscheLou.zugang = True
        self.a.name_setzen("Mara", "Lotte")
        self.a.anmeldung_fertig("claude")
        self.assertEqual(self.a.abschliessen(), "")
        self.pruefe_lou(False)   # „Ohne Lou weiter“ gilt
        self.assertNotIn("/api/zugang", FalscheLou.anfragen[1:])


class EigenerServer(unittest.TestCase):
    """Eigener Server der Person (box/selbst.sh): kein Betreuer, die Vorlage spricht nur von ihr."""

    def test_vorlage_ohne_betreuer(self):
        sys.path.insert(0, KIT)
        import einrichten
        text = open(os.path.join(KIT, "vorlage", "home", "CLAUDE.md"), encoding="utf-8").read()
        w = {k: "x" for k in einrichten.PLATZHALTER.findall(text)}
        selbst = einrichten.rendern(text, {**w, "ADMIN": "", "PERSON": "Mara"}, False)
        betreut = einrichten.rendern(text, {**w, "ADMIN": "Kim", "PERSON": "Mara"}, False)
        self.assertNotIn("<!--", selbst + betreut)
        self.assertIn("eigenen Server von Mara", selbst)
        self.assertNotIn("betreut", selbst)
        self.assertIn("betreut wird die Box von Kim", betreut)
        self.assertNotIn("eigenen Server", betreut)

    def test_wache_meldet_ueber_eigenen_bot(self):
        import wache
        gesendet = []
        alt = wache.eigener_bot, wache.urllib.request.urlopen
        wache.eigener_bot = lambda u: ("1:tok", "0")
        try:
            self.assertTrue(wache.senden({"WACHE_SELBST": "a-ich"}, "x"))   # noch niemand verbunden: still
            wache.eigener_bot = lambda u: ("1:tok", "777")

            class Antwort:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False
            wache.urllib.request.urlopen = lambda url, data, timeout: gesendet.append((url, data)) or Antwort()
            self.assertTrue(wache.senden({"WACHE_SELBST": "a-ich"}, "Dienst steht"))
        finally:
            wache.eigener_bot, wache.urllib.request.urlopen = alt
        self.assertEqual(len(gesendet), 1)
        self.assertIn("/bot1:tok/sendMessage", gesendet[0][0])
        self.assertIn(b"chat_id=777", gesendet[0][1])

    def test_nochmal(self):
        netz = os.path.join(KIT, "box", "netz.sh")
        skript = ('. "$1"; z=$(mktemp); trap \'rm -f "$z"\' EXIT; '
                  'zwei() { echo x >> "$z"; [ "$(wc -l < "$z")" -ge 2 ]; }; '
                  'nochmal zwei || exit 7; nochmal false || echo "zweimal gescheitert"; nochmal true')
        r = subprocess.run(["bash", "-euc", skript, "-", netz], capture_output=True, text=True,
                           env={**os.environ, "NOCHMAL_PAUSE": "0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("zweimal gescheitert", r.stdout)
        self.assertEqual(r.stderr.count("zweiter Versuch"), 2)

    def test_installation_bricht_nicht_spaet_ab(self):
        selbst = open(os.path.join(KIT, "box", "selbst.sh"), encoding="utf-8").read()
        # Updates und /whatsapp stehen, bevor person-anlegen.sh den Selbsttest macht
        self.assertLess(selbst.index("enable --now assistent-aktualisieren.timer"), selbst.index("person-anlegen.sh \"$KURZ\""))
        self.assertLess(selbst.index("enable --now assistent-whatsapp.path"), selbst.index("person-anlegen.sh \"$KURZ\""))
        # ⚠️ nur, wenn der Assistent nicht läuft
        schief = selbst[selbst.index("schief() {"):selbst.index("trap schief ERR")]
        self.assertLess(schief.index("is-active"), schief.index('stand "⚠️'))
        anlegen = open(os.path.join(KIT, "box", "person-anlegen.sh"), encoding="utf-8").read()
        self.assertIn("for i in 1 2 3; do", anlegen)
        self.assertIn('[ -n "$SELBST" ] && systemctl --user -M "$U@" is-active -q assistent.service', anlegen)
        akt = open(os.path.join(KIT, "box", "aktualisieren.sh"), encoding="utf-8").read()
        # neue Fassung erst daneben, Tausch erst nach den Downloads, bei Fehlern danach zurück
        self.assertLess(akt.index('ki-programme.sh"'), akt.index('mv "$NEU" "$KIT"'))
        self.assertLess(akt.index("trap zurueck ERR"), akt.index("\neinspielen\n"))

    def test_anbieter_neutral_und_swap(self):
        selbst = open(os.path.join(KIT, "box", "selbst.sh"), encoding="utf-8").read()
        self.assertIn("/hetzner/v1/metadata/public-ipv4", selbst)
        self.assertIn("/metadata/v1/interfaces/public/0/ipv4/address", selbst)   # DigitalOcean
        self.assertNotIn("Server bei Hetzner", selbst)
        # Swap vor den Downloads, ein Fehler dort hält die Einrichtung nicht auf
        self.assertIn("auslagerung || echo", selbst)
        self.assertLess(selbst.index("auslagerung || echo"), selbst.index('bash "$KIT_QUELLE/box/einrichten.sh"'))
        funktion = re.search(r"^oeffentlich\(\) \{.*?^\}\n", selbst, re.S | re.M).group(0)
        r = subprocess.run(["bash", "-c", funktion + 'for a in "$@"; do oeffentlich "$a" && echo "$a"; done', "-",
                            "49.12.3.4", "10.1.2.3", "172.16.0.1", "172.32.0.1", "192.168.0.9", "100.64.1.1", "127.0.0.1",
                            "169.254.169.254", "1.2.3", "164.90.1.2"], capture_output=True, text=True)
        self.assertEqual(r.stdout.split(), ["49.12.3.4", "172.32.0.1", "164.90.1.2"])

    def test_release_signatur(self):
        """Updates nur mit SSH-Signatur des Release-Schlüssels, geprüft gegen die installierte Kopie."""
        signer = open(os.path.join(KIT, "box", "release-signers"), encoding="utf-8").read().split()
        self.assertEqual(signer[:2], ["telegram-assistent", 'namespaces="git"'])
        self.assertEqual(signer[3], "AAAAC3NzaC1lZDI1NTE5AAAAIJcwWQg20yCZc6uivFel+gkBF6AtF8JedBesAS5gNBZX")
        sa = open(os.path.join(KIT, "box", "selbst-aktualisieren.sh"), encoding="utf-8").read()
        self.assertIn("SIGNIERER=/etc/pearl-kit/release-signers", sa)
        self.assertLess(sa.index('! signiert "$neu"'), sa.index('checkout -q "$neu"'))
        # die Datei aus dem geholten Stand zählt erst nach dem Einspielen (Schlüsselwechsel)
        self.assertGreater(sa.index('"$QUELLE/box/release-signers"'), sa.index('if bash "$QUELLE/box/aktualisieren.sh"'))
        selbst = open(os.path.join(KIT, "box", "selbst.sh"), encoding="utf-8").read()
        self.assertIn('"$KIT_QUELLE/box/release-signers" /etc/pearl-kit/release-signers', selbst)
        if not shutil.which("ssh-keygen"):
            self.skipTest("ssh-keygen fehlt")
        with tempfile.TemporaryDirectory() as t:
            def git(*a, schluessel=None):
                cfg = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "gpg.format=ssh", "-c", "commit.gpgsign=false"]
                if schluessel:
                    cfg += ["-c", f"user.signingkey={schluessel}"]
                return subprocess.run(["git", *cfg, "-C", f"{t}/repo", *a], capture_output=True, text=True)
            for k in ("release", "fremd"):
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", f"{t}/{k}"], check=True)
            pub = open(f"{t}/release.pub").read().split()
            with open(f"{t}/signers", "w") as f:   # Form wie box/release-signers, nur mit dem Testschlüssel
                f.write(" ".join(signer[:2] + pub[:2]) + "\n")
            subprocess.run(["git", "init", "-q", f"{t}/repo"], check=True)
            pruefen = lambda: git("-c", f"gpg.ssh.allowedSignersFile={t}/signers", "verify-commit", "HEAD").returncode
            self.assertEqual(git("commit", "-q", "--allow-empty", "-S", "-m", "a", schluessel=f"{t}/release").returncode, 0)
            self.assertEqual(pruefen(), 0)
            git("commit", "-q", "--allow-empty", "-S", "-m", "b", schluessel=f"{t}/fremd")
            self.assertNotEqual(pruefen(), 0)
            git("commit", "-q", "--allow-empty", "-m", "c")
            self.assertNotEqual(pruefen(), 0)

    def test_metadaten_sperre(self):
        skript = open(os.path.join(KIT, "box", "metadaten-sperre.sh"), encoding="utf-8").read()
        self.assertIn("! --uid-owner 0 -j REJECT", skript)
        self.assertIn("-d 169.254.169.254/32", skript)
        funktion = re.search(r"^einfuegen\(\) \{.*?^\}\n", skript, re.S | re.M).group(0)
        regeln = "*filter\n:ufw-before-output - [0:0]\n-A ufw-before-output -o lo -j ACCEPT\n# ende\nCOMMIT\n"
        with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False) as f:
            f.write(regeln)
        self.addCleanup(os.unlink, f.name)
        r = subprocess.run(["bash", "-c", funktion + 'einfuegen "$1" "# marke" "-A ufw-before-output REGEL"', "-", f.name],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.endswith("# ende\n# marke\n-A ufw-before-output REGEL\nCOMMIT\n"), r.stdout)
        self.assertIn('bash "$KIT_QUELLE/box/metadaten-sperre.sh"',
                      open(os.path.join(KIT, "box", "selbst.sh"), encoding="utf-8").read())

    def test_tests_ohne_root_und_feste_pakete(self):
        akt = open(os.path.join(KIT, "box", "aktualisieren.sh"), encoding="utf-8").read()
        self.assertIn("runuser -u nobody", akt)
        self.assertNotIn('unittest discover -s "$QUELLE/tests"', akt)
        anf = open(os.path.join(KIT, "box", "workspace-mcp.txt"), encoding="utf-8").read()
        self.assertEqual(len(re.findall(r"--hash=sha256:[0-9a-f]{64}", anf)), 2)
        netz = open(os.path.join(KIT, "box", "netz.sh"), encoding="utf-8").read()
        self.assertIn("--require-hashes --no-deps", netz)
        for name in ("einrichten.sh", "aktualisieren.sh"):
            text = open(os.path.join(KIT, "box", name), encoding="utf-8").read()
            self.assertIn('workspace_mcp "$KIT/.venv-google" "$KIT/box/workspace-mcp.txt"', text, name)
            self.assertNotIn('install -q --timeout 30 "workspace-mcp', text, name)

    def test_skripte_ohne_fragen(self):
        r = subprocess.run(["bash", "-n", os.path.join(KIT, "box", "selbst.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        anlegen = open(os.path.join(KIT, "box", "person-anlegen.sh"), encoding="utf-8").read()
        self.assertIn('[ -n "$SELBST" ] || ARGS+=(--admin', anlegen)
        self.assertIn("NEU_EINLADUNG", anlegen)
        self.assertIn('--kits "$NEU_KITS"', anlegen)
        self.assertIn('NEU_KITS="$AUSSTATTUNG"', open(os.path.join(KIT, "box", "selbst.sh"), encoding="utf-8").read())


class Abschottung(unittest.TestCase):
    """Was eine eingeschleuste Anweisung nach draußen tragen könnte: WebFetch, /proc, Netz in Codex, Gäste."""

    def test_settings_und_codex(self):
        inst = Instanz(google=True)
        self.addCleanup(inst.weg)
        settings = json.load(open(os.path.join(inst.env["ASSISTENT_DIR"], "settings.json")))["permissions"]
        self.assertNotIn("WebFetch", settings["allow"])
        self.assertIn("WebFetch", settings["deny"])
        self.assertIn("WebSearch", settings["allow"])
        self.assertIn("Read(/proc/**)", settings["deny"])
        import anbieter
        self.assertIn("network_access = false", anbieter.codex_config("/h", 0))
        self.assertIn("network_access = true", anbieter.codex_config("/h", 0, netz=True))

    def test_keine_gaeste(self):
        import asyncio
        import importlib.util
        spec = importlib.util.spec_from_file_location("google_mcp_auth", os.path.join(KIT, "google", "google-mcp-auth.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        weiter = []

        async def app(scope, receive, send):
            weiter.append((await receive())["body"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        def aufruf(nachricht):
            gesendet, teile = [], [json.dumps(nachricht).encode()[:20], json.dumps(nachricht).encode()[20:]]

            async def receive():
                return {"type": "http.request", "body": teile.pop(0), "more_body": bool(teile)}

            async def send(m_):
                gesendet.append(m_)
            asyncio.run(m.OhneGaeste(app)({"type": "http", "method": "POST"}, receive, send))
            return json.loads(gesendet[-1]["body"]) if gesendet[-1]["body"] != b"ok" else "weiter"

        mit = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "manage_event", "arguments": {
            "action": "create", "summary": "x", "attendees": ["fremd@example.org"]}}}
        antwort = aufruf(mit)
        self.assertEqual(antwort["id"], 7)
        self.assertIn("Gäste", antwort["error"]["message"])
        self.assertEqual(weiter, [])
        ohne = {**mit, "params": {"name": "manage_event", "arguments": {"action": "create", "summary": "x"}}}
        self.assertEqual(aufruf(ohne), "weiter")
        self.assertEqual(json.loads(weiter[0]), ohne)   # unverändert weitergereicht


class Wache(unittest.TestCase):
    def test_gescheitertes_update(self):
        import wache
        with tempfile.TemporaryDirectory() as t:
            alt = wache.GESCHEITERT
            wache.GESCHEITERT = os.path.join(t, "aktualisieren-gescheitert")
            try:
                self.assertEqual(wache.aktualisierung(), [])                # nichts gescheitert
                for zeile in ("abcdef1234", "abcdef1234 1", "abcdef1234 2"):  # alte Zeile ohne Zähler, Versuch 1 und 2
                    open(wache.GESCHEITERT, "w").write(zeile + "\n")
                    self.assertEqual(wache.aktualisierung(), [], zeile)
                open(wache.GESCHEITERT, "w").write("abcdef1234 3\n")
                b = wache.aktualisierung()
                self.assertEqual([x["schluessel"] for x in b], ["aktualisierung"])
                self.assertIn("abcdef1", b[0]["text"])
                self.assertIn({"schluessel": "aktualisierung", "stufe": 1, "text": b[0]["text"]}, wache.box_befunde())
                open(wache.GESCHEITERT, "w").write("abcdef1234 3 signatur\n")
                b = wache.aktualisierung()
                self.assertEqual(b[0]["stufe"], 2)
                self.assertIn("nicht mit dem Release-Schlüssel signiert", b[0]["text"])
            finally:
                wache.GESCHEITERT = alt

    def test_entdoppeln_erinnern_entwarnen(self):
        import wache
        t = 1_000_000.0
        n, offen = wache.entscheiden({}, {"a-x/telegram": "erreicht Telegram nicht"}, t)
        self.assertEqual(len(n), 1)
        n, offen = wache.entscheiden(offen, {"a-x/telegram": "erreicht Telegram nicht"}, t + 180)
        self.assertEqual(n, [])
        n, offen = wache.entscheiden(offen, {"a-x/telegram": "erreicht Telegram nicht"}, t + wache.ERINNERN + 1)
        self.assertEqual(len(n), 1)
        self.assertIn("seit", n[0])
        n, offen = wache.entscheiden(offen, {}, t + wache.ERINNERN + 200)
        self.assertEqual(len(n), 1)
        self.assertIn("wieder in Ordnung", n[0])
        self.assertEqual(offen, {})


if __name__ == "__main__":
    unittest.main()


class FalschesWhatsApp(http.server.BaseHTTPRequestHandler):
    """Spielt Bridge (/api/status, /api/auth/qr) und Connector (/mcp/, tools/call) der WhatsApp-Erweiterung."""
    jetzt = 0
    chats = []
    nachrichten = {}
    gepaart = True
    zustand = ""        # gesetzt: auth_state, den /api/status meldet
    aufrufe = []

    def log_message(self, *a):
        pass

    def senden(self, body):
        daten = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def do_GET(self):
        if self.path.startswith("/api/auth/qr"):
            return self.senden({"qr_code": f"2@code-{int(time.time() // 2)}"})
        self.senden({"auth_state": self.zustand or ("paired" if self.gepaart else "unpaired")})

    def do_POST(self):
        n = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        if self.path == "/api/auth/pair-phone":
            self.aufrufe.append(("pair-phone", n, None))
            FalschesWhatsApp.zustand = "pairing_pending"
            return self.senden({"pairing_code": "ABCD1234", "formatted": "ABCD-1234", "state": "pairing_pending"})
        name, args = n["params"]["name"], n["params"]["arguments"]
        self.aufrufe.append((name, args, self.headers.get("Authorization")))
        if name == "healthcheck":
            r = {"timestamp": self.jetzt, "status_detail": {"connected": self.gepaart,
                                                             "auth_state": "paired" if self.gepaart else "unpaired",
                                                             "device_jid": "999:1@s.whatsapp.net"}}
        elif name == "list_chats":
            r = {"chats": self.chats[args.get("offset", 0):args.get("offset", 0) + args["limit"]]}
        else:
            r = {"messages": sorted(self.nachrichten.get(args["chat_jid"], []), key=lambda m: -m["timestamp"])}
        self.senden({"jsonrpc": "2.0", "id": n["id"], "result": {"structuredContent": {"result": r}}})


class WhatsApp(Chat, unittest.TestCase):
    """Erweiterung WhatsApp: Werkzeug gegen einen falschen Connector, Einrichtung im Chat gegen eine falsche Bridge."""

    @classmethod
    def setUpClass(cls):
        cls.port, cls.wa_port = freier_port(), freier_port()
        cls.srv = server_starten(cls.port)
        cls.wa = http.server.ThreadingHTTPServer(("127.0.0.1", cls.wa_port), FalschesWhatsApp)
        threading.Thread(target=cls.wa.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.wa.shutdown()

    def setUp(self):
        FalscherServer.anfragen.clear()
        FalschesWhatsApp.aufrufe.clear()
        FalschesWhatsApp.gepaart, FalschesWhatsApp.zustand = True, ""
        self.inst = Instanz(google=False, port=self.port)
        t = self.inst.tmp.name
        os.makedirs(f"{t}/wa")
        with open(f"{t}/wa/token", "w") as f:
            f.write("ab" * 32)
        with open(f"{t}/unit.path", "w") as f:
            f.write("")
        self.inst.env.update({"WHATSAPP_TOKEN_DATEI": f"{t}/wa/token", "ASSISTENT_WA_DIR": f"{t}/wa",
                              "WHATSAPP_MCP_URL": f"http://127.0.0.1:{self.wa_port}/mcp/",
                              "ASSISTENT_WA_API": f"http://127.0.0.1:{self.wa_port}/api",
                              "ASSISTENT_WA_UNIT": f"{t}/unit.path", "ASSISTENT_WA_TAKT": "0.2"})
        self.home = os.path.join(self.inst.env["ASSISTENT_DIR"], "home")

    def tearDown(self):
        for k in ("WHATSAPP_TOKEN_DATEI", "ASSISTENT_WA_DIR", "WHATSAPP_MCP_URL", "ASSISTENT_WA_API", "ASSISTENT_WA_UNIT",
                  "ASSISTENT_WA_TAKT"):
            os.environ.pop(k, None)
        self.inst.weg()

    def werkzeug(self, *args):
        r = subprocess.run([sys.executable, os.path.join(KIT, "werkzeuge", "whatsapp.py"), *args],
                           env={**os.environ, **self.inst.env}, capture_output=True, text=True, cwd=self.home)
        return r.returncode, r.stdout + r.stderr

    def lesestand(self, wert):
        pfad = os.path.join(self.home, "whatsapp", "Sweep-State.md")
        text = re.sub(r"^\| Chatliste \| \d+ \|", f"| Chatliste | {wert} |", open(pfad).read(), flags=re.M)
        with open(pfad, "w") as f:
            f.write(text)

    def bericht(self, status="ok"):
        heute = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        with open(os.path.join(self.home, "whatsapp", "Sweep-Report.md"), "w") as f:
            f.write(f"---\ntype: sweep-report\nrun_at: {heute}\nrun_status: {status}\n---\n")

    def test_einrichten_und_abschalten(self):
        code, aus = self.werkzeug("einrichten")
        self.assertEqual(code, 0, aus)
        claude = open(os.path.join(self.home, "CLAUDE.md")).read()
        self.assertIn("whatsapp.py lies", claude)
        self.assertIn("whatsapp/heute.md", open(os.path.join(self.home, "jobs", "morgen.md")).read())
        jobs = sorted(os.listdir(os.path.join(self.home, "jobs")))
        self.assertIn("whatsapp-nacht.md", jobs)
        self.assertIn("whatsapp-kreis.md", jobs)
        self.assertNotIn("whatsapp-morgen.md", jobs)   # der Morgen-Check kennt die Vorschläge schon
        for wurzel, _, dateien in os.walk(self.home):
            for d in dateien:
                text = open(os.path.join(wurzel, d), encoding="utf-8").read()
                self.assertNotIn("{{", text, d)
                self.assertNotIn("<!--", text, d)
        self.assertIn("WHATSAPP_MCP_TOKEN=" + "ab" * 32, open(self.inst.env["ASSISTENT_SECRETS"]).read())
        self.assertIn('status = "an"', open(os.path.join(self.inst.env["ASSISTENT_DIR"], "config.toml")).read())
        self.assertIn("home/whatsapp/.sweep/", open(os.path.join(self.inst.env["ASSISTENT_DIR"], ".gitignore")).read())
        a = self.inst.laden(f"http://127.0.0.1:{self.port}")
        j = a.jobs()
        self.assertEqual(j["whatsapp-nacht"]["vorher"], "werkzeuge/whatsapp.py hole")
        self.assertTrue(j["whatsapp-kreis"]["datum"])
        with open(os.path.join(self.home, "whatsapp", "personen", "Anna Test.md"), "w") as f:
            f.write("# Anna Test\n")
        code, aus = self.werkzeug("abschalten")
        self.assertEqual(code, 0, aus)
        self.assertFalse(glob.glob(os.path.join(self.home, "jobs", "whatsapp-*")))
        self.assertNotIn("whatsapp.py lies", open(os.path.join(self.home, "CLAUDE.md")).read())
        self.assertNotIn("WHATSAPP_MCP_TOKEN", open(self.inst.env["ASSISTENT_SECRETS"]).read())
        self.assertTrue(os.path.exists(os.path.join(self.home, "whatsapp", "personen", "Anna Test.md")))

    def test_delta_warteschlange_und_pruefung(self):
        self.assertEqual(self.werkzeug("einrichten")[0], 0)
        jetzt = int(time.time())
        wm = jetzt - 86400
        self.lesestand(wm)
        FalschesWhatsApp.jetzt = jetzt
        FalschesWhatsApp.chats = [
            {"jid": "111@s.whatsapp.net", "name": "Anna Test", "last_message_time": jetzt - 100},
            {"jid": "222@g.us", "name": "Gruppe", "chat_type": "group", "last_message_time": jetzt},
            {"jid": "333@s.whatsapp.net", "name": "Ben Test", "last_message_time": wm - 500},
            {"jid": "444@s.whatsapp.net", "name": "Carla Test", "last_message_time": jetzt - 300},
            {"jid": "999@s.whatsapp.net", "name": "Ich selbst", "last_message_time": jetzt - 10}]
        FalschesWhatsApp.nachrichten = {
            "111@s.whatsapp.net": [
                {"id": "a1", "timestamp": jetzt - 200, "is_from_me": False, "sender_display": "Anna Test",
                 "content_text": "Sehen wir uns Freitag?", "type": "text"},
                {"id": "a2", "timestamp": jetzt - 50000, "is_from_me": True, "content_text": "Bin zurück", "type": "text"},
                {"id": "a3", "timestamp": wm - 10, "is_from_me": False, "content_text": "alt", "type": "text"}],
            "444@s.whatsapp.net": [{"id": "c1", "timestamp": jetzt - 300, "is_from_me": False,
                                    "sender_display": "Carla Test", "type": "voice", "content_text": ""}]}
        code, aus = self.werkzeug("hole", "--max-chats", "1")
        self.assertEqual(code, 0, aus)
        delta = json.load(open(os.path.join(self.home, "whatsapp", ".sweep", "delta.json")))
        self.assertEqual(delta["status"], "partial")
        self.assertEqual([c["chat"] for c in delta["delta"]], ["Anna Test"])
        self.assertEqual(delta["delta"][0]["anzahl"], 2)
        self.assertIn("Testperson: Bin zurück", delta["delta"][0]["nachrichten"][1])
        self.assertEqual(delta["zurueckgestellt"], ["Carla Test"])
        self.assertEqual(delta["watermark_neu"], jetzt)
        self.assertTrue(all(a[2] == "Bearer " + "ab" * 32 for a in FalschesWhatsApp.aufrufe))
        self.assertNotIn("111@", json.dumps(delta))   # keine Kennungen in home/

        # Lauf ohne neuen Lesestand: die Person bekommt Bescheid
        self.bericht("partial")
        code, aus = self.werkzeug("pruefe")
        self.assertEqual(code, 1, aus)
        self.assertIn("nicht weitergerückt", aus)

        # Lauf wie vorgesehen: Plan übernommen, Carla kommt in der nächsten Nacht zuerst
        self.lesestand(jetzt)
        code, aus = self.werkzeug("pruefe")
        self.assertEqual(code, 0, aus)
        FalschesWhatsApp.jetzt = jetzt + 60
        code, aus = self.werkzeug("hole")
        self.assertEqual(code, 0, aus)
        delta = json.load(open(os.path.join(self.home, "whatsapp", ".sweep", "delta.json")))
        self.assertEqual(delta["delta"][0]["chat"], "Carla Test")
        self.assertTrue(delta["delta"][0]["aus_warteschlange"])
        self.assertIn("[Sprachnachricht ohne Transkript]", delta["delta"][0]["nachrichten"][0])
        self.assertEqual(delta["status"], "ok")

        code, aus = self.werkzeug("lies", "anna")
        self.assertEqual(code, 0, aus)
        self.assertIn("Sehen wir uns Freitag?", aus)
        self.assertEqual(self.werkzeug("lies", "Test")[0], 1)   # mehrdeutig

    def test_nicht_verbunden(self):
        self.assertEqual(self.werkzeug("einrichten")[0], 0)
        FalschesWhatsApp.gepaart = False
        code, aus = self.werkzeug("hole")
        self.assertEqual(code, 2)
        self.assertIn("FAILED", aus)
        self.bericht("failed")
        code, aus = self.werkzeug("pruefe")
        self.assertEqual(code, 1)
        self.assertIn("/whatsapp", aus)

    def test_einrichtung_ueber_die_seite(self):
        web = freier_port()
        self.inst.weg()
        self.inst = Instanz(google=False, port=self.port, web_port=web)
        t = self.inst.tmp.name
        os.makedirs(f"{t}/wa")
        with open(f"{t}/wa/token", "w") as f:
            f.write("ab" * 32)
        with open(f"{t}/unit.path", "w") as f:
            f.write("")
        self.inst.env.update({"WHATSAPP_TOKEN_DATEI": f"{t}/wa/token", "ASSISTENT_WA_DIR": f"{t}/wa",
                              "WHATSAPP_MCP_URL": f"http://127.0.0.1:{self.wa_port}/mcp/",
                              "ASSISTENT_WA_API": f"http://127.0.0.1:{self.wa_port}/api",
                              "ASSISTENT_WA_UNIT": f"{t}/unit.path", "ASSISTENT_WA_TAKT": "0.2"})
        self.home = os.path.join(self.inst.env["ASSISTENT_DIR"], "home")
        os.environ.update(self.inst.env)
        self.a = self.inst.laden(f"http://127.0.0.1:{self.port}")
        self.nachricht("/whatsapp")
        self.assertTrue(any("eigenen Server" in x for x in self.gesendet()))   # Box mit Betreuer: geht nicht
        self.a.ADMIN = ""
        pf_srv, _ = self.a.pforte_starten()
        self.addCleanup(pf_srv.shutdown)

        def holen(pfad):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{web}{pfad}", timeout=10) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        def stand(tok):
            return json.loads(holen(f"/whatsapp/stand?t={tok}")[1])

        def bis(bedingung, sekunden=15):
            ende = time.time() + sekunden
            while time.time() < ende and not bedingung():
                time.sleep(0.1)
            return bedingung()

        # Von selbst: nicht in den ersten Tagen nach der Begrüßung, später tagsüber einmal
        self.a.update(begruesst=time.time())
        self.assertFalse(self.a.wa_anbieten())
        self.a.update(begruesst=time.time() - 4 * 86400)
        self.a.jetzt = lambda: datetime.datetime(2026, 11, 24, 7, 0).astimezone()
        self.assertFalse(self.a.wa_anbieten())
        self.a.jetzt = lambda: datetime.datetime(2026, 11, 24, 11, 0).astimezone()
        FalscherServer.anfragen.clear()
        self.assertTrue(self.a.wa_anbieten())
        self.assertFalse(self.a.wa_anbieten())   # nicht doppelt
        angebot = [x["daten"] for x in FalscherServer.anfragen if x.get("methode") == "sendMessage"][-1]
        self.assertIn("in WhatsApp schreibe ich nie", angebot["text"])
        tastatur = json.loads(angebot["reply_markup"])["keyboard"]
        url = tastatur[0][0]["web_app"]["url"]
        self.assertTrue(url.startswith("https://box.example.de/test/whatsapp?t="))
        tok = url.split("t=", 1)[1]
        self.nachricht("Später")
        self.assertTrue(any("in einer Woche" in x for x in self.gesendet()))
        self.assertFalse(self.a.wa_anbieten())

        # Seite: falscher Link nein, richtiger ja
        self.assertEqual(holen("/whatsapp?t=falsch")[0], 403)
        code, seite = holen(f"/whatsapp?t={tok}")
        self.assertEqual(code, 200)
        self.assertIn("Einverstanden, verbinden", seite)
        self.assertEqual(stand(tok)["schritt"], "frage")

        bilder = []
        self.a.wa_qr_bild = lambda c: (bilder.append(c), "data:image/svg+xml;base64,AAAA")[1]   # ohne qrencode
        FalschesWhatsApp.gepaart = False
        FalscherServer.anfragen.clear()
        req = urllib.request.Request(f"http://127.0.0.1:{web}/whatsapp/start", data=json.dumps({"t": tok}).encode(),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        self.assertEqual(open(self.a.WA_AUFTRAG).read().strip(), "an")
        os.remove(self.a.WA_AUFTRAG)   # hier holt sonst root den Auftrag ab
        self.assertTrue(any("zehn bis fünfzehn Minuten" in x for x in self.gesendet()))
        self.assertEqual(stand(tok)["schritt"], "baut")

        datei = os.path.join(t, "wa", "stand")
        with open(datei, "w") as f:
            f.write("bereit")
        self.assertTrue(bis(lambda: any(k[1] == "QR-Code zeigen" for k in self.knoepfe(0))), "kein Knopf zum QR-Code")
        s = stand(tok)
        self.assertEqual(s["schritt"], "qr")
        self.assertTrue(s["qr"].startswith("data:image/svg+xml") and bilder)
        FalschesWhatsApp.gepaart = True
        with open(datei, "w") as f:
            f.write("verknuepft")
        self.assertTrue(bis(lambda: stand(tok)["schritt"] == "verknuepft"))
        with open(datei, "w") as f:
            f.write("fertig")
        self.assertTrue(bis(lambda: not self.a._wa_faden.is_alive(), 20))
        self.assertTrue(any("eingerichtet" in x for x in self.gesendet()))
        self.assertEqual(self.a.wa_status(), "an")
        self.assertEqual(stand(tok)["schritt"], "fertig")
        self.assertTrue(os.path.exists(os.path.join(self.home, "jobs", "whatsapp-nacht.md")))
        self.assertIsNone(self.a.load().get("whatsapp"))
        self.assertFalse(self.a.wa_anbieten())   # verbunden: kein Angebot mehr

    def test_telefonnummer_und_status(self):
        """Koppeln mit Telefonnummer statt QR-Code, /whatsapp fragt die Bridge live."""
        self.a = self.inst.laden(f"http://127.0.0.1:{self.port}")
        self.a.WEB, self.a.ADMIN = "https://box.example.de/test", ""   # eigener Server
        self.assertIn("Ländervorwahl", self.a.wa_nummer("0170 1234567")[1])
        self.assertIn("Ländervorwahl", self.a.wa_nummer("abc")[1])
        self.assertEqual(self.a.wa_nummer("0049 (170) 123-4567"), ("ABCD-1234", ""))
        self.assertEqual(FalschesWhatsApp.aufrufe[-1][1], {"phone_number": "+491701234567"})
        self.assertEqual(self.a.wa_bridge_zustand(), "pairing_pending")
        # /whatsapp: eingerichtet, die Bridge ist aber nicht mehr gekoppelt
        self.a.kit.setze_config("whatsapp", "status", "an")
        FalschesWhatsApp.zustand = "logged_out"
        FalscherServer.anfragen.clear()
        self.nachricht("/whatsapp")
        n = [x["daten"] for x in FalscherServer.anfragen if x.get("methode") == "sendMessage"][-1]
        self.assertIn("nicht mehr", n["text"])
        self.assertEqual(json.loads(n["reply_markup"])["keyboard"], [[{"text": self.a.WA_NEU}]])
        # Fingerabdruck: neue Fassung der Bridge bestellt einmal einen Auftrag
        self.assertTrue(self.a.wa_nachziehen())
        self.assertEqual(open(self.a.WA_AUFTRAG).read().strip(), "an")
        os.remove(self.a.WA_AUFTRAG)
        self.assertFalse(self.a.wa_nachziehen())   # gleiche Fassung: nichts zu tun

    def test_haekchen_im_onboarding(self):
        web = freier_port()
        self.inst.weg()
        self.inst = Instanz(google=False, port=self.port, web_port=web)
        t = self.inst.tmp.name
        os.makedirs(f"{t}/wa")
        with open(f"{t}/unit.path", "w") as f:
            f.write("")
        self.inst.env.update({"ASSISTENT_WA_DIR": f"{t}/wa", "ASSISTENT_WA_API": f"http://127.0.0.1:{self.wa_port}/api",
                              "ASSISTENT_WA_UNIT": f"{t}/unit.path", "ASSISTENT_WA_TAKT": "0.2"})
        os.environ.update(self.inst.env)
        self.a = self.inst.laden(f"http://127.0.0.1:{self.port}")
        self.a.ADMIN = ""
        pf_srv, _ = self.a.pforte_starten()
        self.addCleanup(pf_srv.shutdown)
        claude_md = os.path.join(self.inst.env["ASSISTENT_DIR"], "home", "CLAUDE.md")
        os.rename(claude_md, claude_md + ".weg")   # mitten in der Einrichtung
        tok = self.a.anmeldelink().split("t=", 1)[1]

        def post(pfad, daten):
            req = urllib.request.Request(f"http://127.0.0.1:{web}{pfad}", data=json.dumps({"t": tok, **daten}).encode(),
                                         headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())

        with urllib.request.urlopen(f"http://127.0.0.1:{web}/einrichten/stand?t={tok}", timeout=10) as r:
            self.assertEqual(json.loads(r.read())["whatsapp"], {"moeglich": True, "wahl": "", "an": False})
        with urllib.request.urlopen(f"http://127.0.0.1:{web}/einrichten?t={tok}", timeout=10) as r:
            self.assertIn("Ja, WhatsApp dazu", r.read().decode())
        FalscherServer.anfragen.clear()
        self.assertTrue(post("/einrichten/whatsapp", {"an": True})["ok"])
        self.assertEqual(open(self.a.WA_AUFTRAG).read().strip(), "an")   # der Server baut sofort
        os.remove(self.a.WA_AUFTRAG)
        self.assertEqual(self.a.load()["wa_onboarding"], "ja")
        with open(f"{t}/wa/stand", "w") as f:
            f.write("bereit")
        time.sleep(1.5)
        self.assertEqual(self.knoepfe(0), [])   # kein QR-Code, solange die Einrichtung läuft
        os.rename(claude_md + ".weg", claude_md)
        ende = time.time() + 15
        while time.time() < ende and not any(k[1] == "QR-Code zeigen" for k in self.knoepfe(0)):
            time.sleep(0.2)
        self.assertTrue(any(k[1] == "QR-Code zeigen" for k in self.knoepfe(0)))
        self.assertFalse(self.a.wa_anbieten())   # eingewilligt: kein Angebot mehr
        with open(f"{t}/wa/stand", "w") as f:
            f.write("fehler: weg")   # Faden beenden
        ende = time.time() + 15
        while time.time() < ende and self.a._wa_faden.is_alive():
            time.sleep(0.1)

    def test_vorab_gewaehlte_kits(self):
        web = freier_port()
        self.inst.weg()
        self.inst = Instanz(google=False, port=self.port, web_port=web, kits="ablage")
        t = self.inst.tmp.name
        with open(f"{t}/unit.path", "w") as f:
            f.write("")
        self.inst.env.update({"ASSISTENT_WA_DIR": f"{t}/wa", "ASSISTENT_WA_UNIT": f"{t}/unit.path"})
        os.environ.update(self.inst.env)
        self.a = self.inst.laden(f"http://127.0.0.1:{self.port}")
        self.a.ADMIN = ""
        pf_srv, _ = self.a.pforte_starten()
        self.addCleanup(pf_srv.shutdown)
        tok = self.a.anmeldelink().split("t=", 1)[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{web}/einrichten/stand?t={tok}", timeout=10) as r:
            stand = json.loads(r.read())
        self.assertEqual(stand["kits"], ["ablage"])
        self.assertTrue(stand["whatsapp"]["moeglich"])   # möglich bleibt es, die Seite blendet den Schritt nur aus
        with urllib.request.urlopen(f"http://127.0.0.1:{web}/einrichten?t={tok}", timeout=10) as r:
            self.assertIn("hat('whatsapp')", r.read().decode())
        self.a.jetzt = lambda: datetime.datetime(2026, 11, 25, 12, 0, tzinfo=self.a.TZ)
        self.assertFalse(self.a.wa_anbieten())   # nicht gewählt: kein Angebot von selbst
        self.a.KITS = None
        self.assertTrue(self.a.wa_anbieten())    # ohne Vorauswahl wie bisher

    def test_fehler_auf_dem_server(self):
        web = freier_port()
        self.inst.weg()
        self.inst = Instanz(google=False, port=self.port, web_port=web)
        t = self.inst.tmp.name
        os.makedirs(f"{t}/wa")
        with open(f"{t}/unit.path", "w") as f:
            f.write("")
        self.inst.env.update({"ASSISTENT_WA_DIR": f"{t}/wa", "ASSISTENT_WA_API": f"http://127.0.0.1:{self.wa_port}/api",
                              "ASSISTENT_WA_UNIT": f"{t}/unit.path", "ASSISTENT_WA_TAKT": "0.2"})
        os.environ.update(self.inst.env)
        self.a = self.inst.laden(f"http://127.0.0.1:{self.port}")
        self.a.ADMIN = ""
        FalscherServer.anfragen.clear()
        self.assertEqual(self.a.wa_seite_start(), "")
        os.remove(self.a.WA_AUFTRAG)
        with open(f"{t}/wa/stand", "w") as f:
            f.write("fehler: Der Server hat zu wenig Speicher.")
        ende = time.time() + 15
        while time.time() < ende and self.a._wa_faden.is_alive():
            time.sleep(0.1)
        self.assertIn(("Der Server hat zu wenig Speicher.", "Noch einmal versuchen"),
                      [(k[0], k[1]) for k in self.knoepfe()])
        self.assertEqual(self.a.wa_seitenstand(), {"schritt": "fehler", "fehler": "Der Server hat zu wenig Speicher."})
        time.sleep(1.2)                   # der alte Stand ist älter als der neue Auftrag
        self.nachricht("/whatsapp_neu")   # eingewilligt: startet direkt neu, der alte Fehler ist weg
        self.assertEqual(self.a.wa_seitenstand()["schritt"], "baut")
        self.nachricht("Nein danke")      # ohne offenes Angebot geht das an die KI, nicht an WhatsApp
        self.assertFalse((self.a.load().get("wa_angebot") or {}).get("nein"))


def _png(ordner):
    pfad = os.path.join(ordner, "qr.png")
    with open(pfad, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
    return pfad
