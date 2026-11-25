"""Tests für brain.py, ohne Netz: python3 -m unittest tests.test_brain -v

Ein falsches rclone (Python) liegt vorn im PATH. Es protokolliert Aufrufe und Konfiguration und spielt ein Drive aus
einer JSON-Datei: lsf liest daraus, sync schreibt die oberste Ebene der Quelle hinein.
"""
import glob
import importlib
import json
import os
import re
import sys
import tempfile
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
VORLAGE = os.path.join(KIT, "vorlage", "brain")
MODULE = ("kit", "ablage", "brain")

FAKE_RCLONE = r'''
import json, os, sys
args = sys.argv[1:]
conf = ""
if args[:1] == ["--config"]:
    with open(args[1], encoding="utf-8") as f:
        conf = f.read()
    args = args[2:]
pfad = os.environ["FAKE_RCLONE_DRIVE"]
try:
    with open(pfad, encoding="utf-8") as f:
        drive = json.load(f)
except FileNotFoundError:
    drive = {}
eintrag = {"args": args, "conf": conf}

def ende(code, fehler=""):
    with open(os.environ["FAKE_RCLONE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(eintrag) + "\n")
    if fehler:
        sys.stderr.write(fehler + "\n")
    sys.exit(code)

fehler, bei = os.environ.get("FAKE_RCLONE_FEHLER", ""), os.environ.get("FAKE_RCLONE_FEHLER_BEI", "")
if fehler and bei in ("", args[0]):
    ende(1, fehler)
if args[0] == "lsf":
    name = args[1].split(":", 1)[1]
    if name not in drive:
        ende(3, f"ERROR : : error listing: directory not found")
    print("\n".join(drive[name]))
    ende(0)
if args[0] == "sync":
    quelle, ziel = args[1], args[2].split(":", 1)[1]
    raus = [args[i + 1].strip("/").split("/")[0] for i, a in enumerate(args) if a == "--exclude"]
    dateien = sorted(os.path.relpath(os.path.join(w, d), quelle) for w, _, ds in os.walk(quelle) for d in ds)
    dateien = [d for d in dateien if d.split(os.sep)[0] not in raus]
    eintrag["dateien"] = dateien
    drive[ziel] = sorted({d.split(os.sep)[0] + ("/" if os.sep in d else "") for d in dateien})
    with open(pfad, "w", encoding="utf-8") as f:
        json.dump(drive, f)
    ende(0)
ende(2, "unbekannter Befehl")
'''

DATEIEN = ["brain/COCKPIT.md", "brain/INDEX.md", "brain/REGELN.md", "brain/_eingang/_REGELN.md",
           "brain/vorlagen/STAND-Vorlage.md"]
WERTE = {"PERSON": "Testperson", "ASSISTENT": "Testi", "HEUTE": "2026-01-02"}


def lies(pfad):
    with open(pfad, encoding="utf-8") as f:
        return f.read()


class Vorlage(unittest.TestCase):
    def test_vorlage_sauber(self):
        gefunden = []
        for wurzel, _, dateien in os.walk(VORLAGE):
            for d in dateien:
                pfad = os.path.join(wurzel, d)
                gefunden.append(os.path.relpath(pfad, os.path.dirname(VORLAGE)))
                text = lies(pfad)
                self.assertLessEqual(set(re.findall(r"\{\{(\w+)\}\}", text)), {"PERSON", "ASSISTENT", "HEUTE"}, d)
                self.assertNotIn("<!--", text, d)
                self.assertNotIn("google", text.lower(), d)   # Instanzen ohne Google dürfen das Wort nicht sehen
        self.assertEqual(sorted(gefunden), DATEIEN)

    def test_regeln_decken_das_wichtige_ab(self):
        regeln = lies(os.path.join(VORLAGE, "REGELN.md"))
        for stelle in ("Könnte der Chat jetzt gelöscht werden", "Erst fragen", "STAND.md", "_eingang/", "profil/",
                       "whatsapp/", "gedaechtnis/"):
            self.assertIn(stelle, regeln)


class Brain(unittest.TestCase):
    def setUp(self):
        self.alt_env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        os.environ.update({"ASSISTENT_DIR": f"{t}/assistent", "ASSISTENT_STATE": f"{t}/state",
                           "ASSISTENT_DATEN": f"{t}/daten", "ASSISTENT_SECRETS": f"{t}/secrets.env",
                           "FAKE_RCLONE_LOG": f"{t}/rclone.log", "FAKE_RCLONE_DRIVE": f"{t}/drive.json"})
        for k in ("FAKE_RCLONE_FEHLER", "FAKE_RCLONE_FEHLER_BEI"):
            os.environ.pop(k, None)
        os.makedirs(f"{t}/state")
        os.makedirs(f"{t}/assistent/home")
        self.bin = f"{t}/bin"
        os.makedirs(self.bin)
        with open(f"{self.bin}/rclone", "w", encoding="utf-8") as f:
            f.write(f"#!{sys.executable}\n{FAKE_RCLONE}")
        os.chmod(f"{self.bin}/rclone", 0o755)
        os.environ["PATH"] = self.bin + os.pathsep + self.alt_env.get("PATH", "")
        for m in MODULE:
            sys.modules.pop(m, None)
        self.brain = importlib.import_module("brain")
        self.ablage = self.brain.ablage
        self.ablage.zugang = lambda ziel: ("zugang-xyz", 3600)
        self.home = self.brain.kit.HOME_DIR

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.alt_env)
        for m in MODULE:
            sys.modules.pop(m, None)
        self.tmp.cleanup()

    def aufrufe(self):
        try:
            return [json.loads(z) for z in lies(os.environ["FAKE_RCLONE_LOG"]).splitlines()]
        except FileNotFoundError:
            return []

    def drive(self, inhalt=None):
        pfad = os.environ["FAKE_RCLONE_DRIVE"]
        if inhalt is not None:
            with open(pfad, "w", encoding="utf-8") as f:
                json.dump(inhalt, f)
        try:
            return json.loads(lies(pfad))
        except FileNotFoundError:
            return {}

    def syncs(self):
        return [a for a in self.aufrufe() if a["args"][0] == "sync"]

    # --- anlegen ---

    def test_anlegen_und_vorhandenes_bleibt(self):
        neu = self.brain.anlegen(self.home, WERTE)
        self.assertEqual(neu, DATEIEN)
        for rel in DATEIEN:
            pfad = os.path.join(self.home, rel)
            text = lies(pfad)
            self.assertNotIn("{{", text, rel)
            self.assertEqual(os.stat(pfad).st_mode & 0o777, 0o600, rel)
        self.assertIn("Testperson", lies(os.path.join(self.home, "brain", "REGELN.md")))
        self.assertIn("2026-01-02", lies(os.path.join(self.home, "brain", "COCKPIT.md")))
        regeln = os.path.join(self.home, "brain", "REGELN.md")
        with open(regeln, "w", encoding="utf-8") as f:
            f.write("eigene Regeln\n")
        os.remove(os.path.join(self.home, "brain", "COCKPIT.md"))
        self.assertEqual(self.brain.anlegen(self.home, WERTE), ["brain/COCKPIT.md"])
        self.assertEqual(lies(regeln), "eigene Regeln\n")
        self.assertFalse(glob.glob(os.path.join(self.home, "brain", "**", "*.tmp"), recursive=True))

    def test_anlegen_ohne_werte(self):
        self.brain.anlegen(self.home, {})
        for rel in DATEIEN:
            self.assertNotIn("{{", lies(os.path.join(self.home, rel)), rel)

    # --- teilen und spiegeln ---

    def test_ohne_teilen_kein_lauf(self):
        self.brain.anlegen(self.home, WERTE)
        self.brain.spiegeln(self.home)
        self.assertEqual(self.aufrufe(), [])
        z = self.brain.zustand(True)
        self.assertEqual(z, {"teilen": False, "moeglich": True, "ordner": "Brain", "zuletzt": 0.0, "fehler": ""})

    def test_teilen_spiegelt_nur_brain(self):
        self.brain.anlegen(self.home, WERTE)
        os.makedirs(os.path.join(self.home, "profil"))
        with open(os.path.join(self.home, "profil", "privat.md"), "w") as f:
            f.write("privat\n")
        with open(os.path.join(self.home, "brain", "_eingang", "neu.md"), "w") as f:
            f.write("ungeprüft\n")
        os.makedirs(os.path.join(self.home, "brain", "Umzug"))
        with open(os.path.join(self.home, "brain", "Umzug", "STAND.md"), "w") as f:
            f.write("stand\n")
        self.brain.teilen(True, True)
        s = self.syncs()
        self.assertEqual(len(s), 1)
        args = s[0]["args"]
        self.assertEqual(args[1], os.path.join(self.home, "brain"))
        self.assertEqual(args[2], "ziel:Brain")
        self.assertIn("/_eingang/**", args[args.index("--exclude") + 1])
        self.assertIn("Umzug/STAND.md", s[0]["dateien"])
        self.assertFalse([d for d in s[0]["dateien"] if d.startswith(("_eingang", "profil"))])
        for zeile in ("type = drive", "scope = drive", "zugang-xyz"):
            self.assertIn(zeile, s[0]["conf"])
        z = self.brain.zustand(True)
        self.assertEqual(set(z), {"teilen", "moeglich", "ordner", "zuletzt", "fehler"})
        self.assertEqual((z["teilen"], z["moeglich"], z["ordner"], z["fehler"]), (True, True, "Brain", ""))
        self.assertGreater(z["zuletzt"], 0)
        self.assertIsInstance(z["zuletzt"], float)
        self.assertFalse(glob.glob(os.path.join(self.brain.kit.STATE_DIR, "tmp*", "rclone.conf")))
        self.assertEqual(os.stat(self.brain.STAND).st_mode & 0o777, 0o600)
        self.brain.spiegeln(self.home)   # Nachtlauf: derselbe Ordner, er trägt jetzt REGELN.md
        self.assertEqual([a["args"][2] for a in self.syncs()], ["ziel:Brain", "ziel:Brain"])

    def test_fremder_ordner_bleibt_unberuehrt(self):
        self.brain.anlegen(self.home, WERTE)
        self.drive({"Brain": ["Notizen.md", "Projekte/"]})
        self.brain.teilen(True, True)
        self.assertEqual(self.syncs()[-1]["args"][2], "ziel:Brain 2")
        self.assertEqual(self.drive()["Brain"], ["Notizen.md", "Projekte/"])
        self.assertEqual(self.brain.zustand(True)["ordner"], "Brain 2")
        self.brain.spiegeln(self.home)
        self.assertEqual(self.aufrufe()[-2]["args"][:2], ["lsf", "ziel:Brain 2"])   # gemerkter Ordner zuerst
        self.assertEqual(self.syncs()[-1]["args"][2], "ziel:Brain 2")

    def test_leerer_ordner_gilt_als_unserer(self):
        self.brain.anlegen(self.home, WERTE)
        self.drive({"Brain": []})
        self.brain.teilen(True, True)
        self.assertEqual(self.syncs()[-1]["args"][2], "ziel:Brain")

    def test_zu_viele_fremde_ordner(self):
        self.brain.anlegen(self.home, WERTE)
        self.drive({name: ["fremd.md"] for name in ["Brain"] + [f"Brain {n}" for n in range(2, 21)]})
        with self.assertRaises(self.brain.BrainFehler) as f:
            self.brain.teilen(True, True)
        self.assertIn("Benenne", str(f.exception))
        self.assertEqual(self.syncs(), [])
        self.assertFalse(self.brain.zustand(True)["teilen"])

    def test_teilen_ohne_drive(self):
        self.brain.anlegen(self.home, WERTE)
        with self.assertRaises(self.ablage.AblageFehler) as f:
            self.brain.teilen(True, False)
        self.assertIsInstance(f.exception, self.brain.BrainFehler)
        self.assertIn("Google Drive", str(f.exception))
        self.assertEqual(self.aufrufe(), [])
        z = self.brain.zustand(False)
        self.assertEqual((z["teilen"], z["moeglich"]), (False, False))

    def test_teilen_ohne_brain(self):
        with self.assertRaises(self.brain.BrainFehler):
            self.brain.teilen(True, True)
        self.assertEqual(self.aufrufe(), [])
        self.assertFalse(self.brain.zustand(True)["teilen"])

    def test_zugang_weg(self):
        self.brain.anlegen(self.home, WERTE)
        os.environ["FAKE_RCLONE_FEHLER"] = "Failed to lsf: HTTP error 401 Unauthorized"
        with self.assertRaises(self.brain.BrainFehler) as f:
            self.brain.teilen(True, True)
        self.assertIn("Google", str(f.exception))
        self.assertTrue(f.exception.zugang_weg)
        self.assertIn("401", f.exception.technik)
        z = self.brain.zustand(True)
        self.assertFalse(z["teilen"])
        self.assertTrue(z["fehler"])
        self.assertNotIn("401", z["fehler"])   # Technik nur im Zustand, nicht im Satz für die Person
        self.assertIn("401", self.brain.lies()["fehler_technik"])

    def test_sync_fehler_im_nachtlauf(self):
        self.brain.anlegen(self.home, WERTE)
        self.brain.teilen(True, True)
        os.environ.update({"FAKE_RCLONE_FEHLER": "boom", "FAKE_RCLONE_FEHLER_BEI": "sync"})
        with self.assertRaises(self.brain.BrainFehler) as f:
            self.brain.spiegeln(self.home)
        self.assertIn("nicht geklappt", str(f.exception))
        z = self.brain.zustand(True)
        self.assertTrue(z["teilen"])   # die nächste Nacht versucht es wieder
        self.assertTrue(z["fehler"])
        os.environ.pop("FAKE_RCLONE_FEHLER")
        self.brain.spiegeln(self.home)
        self.assertEqual(self.brain.zustand(True)["fehler"], "")

    def test_ohne_rclone(self):
        self.brain.anlegen(self.home, WERTE)
        leer = os.path.join(self.tmp.name, "leer")
        os.makedirs(leer)
        os.environ["PATH"] = leer
        with self.assertRaises(self.brain.BrainFehler) as f:
            self.brain.teilen(True, True)
        self.assertIn("Programm", str(f.exception))
        self.assertFalse(self.brain.zustand(True)["teilen"])

    def test_teilen_aus(self):
        self.brain.anlegen(self.home, WERTE)
        self.brain.teilen(True, True)
        n = len(self.aufrufe())
        self.brain.teilen(False, True)
        self.assertFalse(self.brain.zustand(True)["teilen"])
        self.brain.spiegeln(self.home)
        self.assertEqual(len(self.aufrufe()), n)


if __name__ == "__main__":
    unittest.main()
