"""Tests für pearl-kit, ohne Netz und ohne echte KI: python3 -m unittest discover -s tests -v

Falsche `claude`, `codex` und `gemini` (fake_*.py) und ein falscher Telegram-Server laufen lokal. Jeder Test
bekommt eine frische Instanz in einem Temp-Ordner.
"""
import codecs
import datetime
import glob
import http.server
import importlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
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

    def __init__(self, google=True, port=None, anbieter="claude", web_port=0, einladung=False):
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
        r = subprocess.run(args, env={**os.environ, **self.env}, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        with open(self.env["ASSISTENT_SECRETS"], "w") as f:
            f.write("TG_TOKEN=123:abc\nGOOGLE_MCP_TOKEN=" + "g" * 40 + "\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-test\n"
                    "CLAUDE_TOKEN_SEIT=2025-11-01\n")

    def laden(self, telegram_api):
        os.environ.update(self.env)
        os.environ["ASSISTENT_TELEGRAM_API"] = telegram_api
        for m in ("kit", "anbieter", "pforte", "einrichten", "assistent"):
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
                    if not google and d.endswith(".md"):
                        self.assertNotIn("google", text.lower(), d)
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
            if x.get("methode") == "sendMessage" and x["daten"].get("reply_markup"):
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

    def test_google_anleitung_ohne_localhost(self):
        self.nachricht("/google")
        k = self.knoepfe()
        self.assertTrue(any(knopf == "Kalender und Mail" and "/einrichten?t=" in u and u.endswith("#google")
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
        self.assertEqual(self.gesendet(warten=0.5), [])
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
        self.assertEqual(self.senden("/einrichten/google", {"t": t, "konto": "kein-konto", "dienste": ["kalender"]})["ok"], False)
        r = self.senden("/einrichten/google", {"t": t, "konto": "Mara@Example.org", "dienste": ["kalender"]})
        self.assertTrue(r["ok"] and r["url"].startswith("https://accounts.google.com/"), r)
        self.assertIn('GOOGLE_RECHTE="calendar:full tasks:manage"',
                      open(os.path.join(self.inst.env["ASSISTENT_DIR"], "google.env")).read())
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
        for teil in ("s-namen", "s-ki", "s-google", "telegram-web-app.js", "Ohne Google weiter", '"test_bot"'):
            self.assertIn(teil, seite)


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

    def test_skripte_ohne_fragen(self):
        r = subprocess.run(["bash", "-n", os.path.join(KIT, "box", "selbst.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        anlegen = open(os.path.join(KIT, "box", "person-anlegen.sh"), encoding="utf-8").read()
        self.assertIn('[ -n "$SELBST" ] || ARGS+=(--admin', anlegen)
        self.assertIn("NEU_EINLADUNG", anlegen)


class Wache(unittest.TestCase):
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
