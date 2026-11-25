"""Tests für die Bereiche in Stay Close (WhatsApp) und `whatsapp.py nachziehen`, ohne Netz:
python3 -m unittest tests.test_bereiche -v

Die Instanz entsteht wie in test_kit mit einrichten.py in einem Temp-Ordner, whatsapp.py läuft als eigener Prozess.
Die Umgebung dieses Prozesses bleibt unberührt.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VORLAGE = os.path.join(KIT, "vorlage", "whatsapp")
WERKZEUG = os.path.join(KIT, "werkzeuge", "whatsapp.py")


def lies(pfad):
    with open(pfad, encoding="utf-8") as f:
        return f.read()


def schreib(pfad, text):
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(text)


class Vorlagen(unittest.TestCase):
    def test_bereich_in_den_vorlagen(self):
        self.assertRegex(lies(os.path.join(VORLAGE, "personen", "_vorlage.md")), r"(?m)^bereich:")
        verfahren = lies(os.path.join(VORLAGE, "verfahren.md"))
        for stelle in ("kontakte.md", "Bereich unklar", "`familie`", "`privat`", "`business`", "montags bis freitags",
                       "erst Familie, dann Privat, dann Business"):
            self.assertIn(stelle, verfahren)
        self.assertIn("## Bereich unklar", lies(os.path.join(VORLAGE, "Sweep-Report.md")))
        self.assertIn("| Name | Bereich | Zuletzt |", lies(os.path.join(VORLAGE, "kontakte.md")))
        claude = lies(os.path.join(VORLAGE, "CLAUDE.md"))
        self.assertIn("Stay Close", claude)
        self.assertIn("Alex ist Familie", claude)
        self.assertIn("Familie, Privat oder Business", lies(os.path.join(VORLAGE, "jobs", "whatsapp-kreis.md")))
        morgen = lies(os.path.join(VORLAGE, "jobs", "whatsapp-morgen.md"))
        for knopf in ("Familie", "Privat", "Business"):
            self.assertIn(f"[knopf: {knopf}]", morgen)

    def test_abschnitte_fortlaufend(self):
        nummern = [int(n) for n in re.findall(r"(?m)^## (\d+)\. ", lies(os.path.join(VORLAGE, "verfahren.md")))]
        self.assertEqual(nummern, list(range(1, len(nummern) + 1)))

    def test_name_des_kits(self):
        for wurzel in (VORLAGE, os.path.join(KIT, "docs")):
            for pfad, _, dateien in os.walk(wurzel):
                for d in dateien:
                    self.assertNotIn("Stay in Touch", lies(os.path.join(pfad, d)), d)
        self.assertIn("Stay Close", lies(os.path.join(KIT, "docs", "whatsapp.md")))


class Nachziehen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        fake = os.path.join(KIT, "tests")
        self.env = {**os.environ,
                    "ASSISTENT_DIR": f"{t}/assistent", "ASSISTENT_STATE": f"{t}/state", "ASSISTENT_DATEN": f"{t}/daten",
                    "ASSISTENT_SECRETS": f"{t}/secrets.env", "ASSISTENT_CLAUDE": f"{fake}/fake_claude.py",
                    "ASSISTENT_CODEX": f"{fake}/fake_codex.py", "ASSISTENT_GEMINI": f"{fake}/fake_gemini.py",
                    "ASSISTENT_KI_BIN": f"{t}/ki-bin", "WHATSAPP_TOKEN_DATEI": f"{t}/wa/token"}
        r = subprocess.run([sys.executable, os.path.join(KIT, "einrichten.py"), "--admin", "Betreuer", "--anbieter",
                            "claude", "--person", "Testperson", "--telegram-id", "4242", "--assistent", "Testi"],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        schreib(f"{t}/wa/token", "ab" * 32)
        self.home = os.path.join(t, "assistent", "home")
        self.wa = os.path.join(self.home, "whatsapp")

    def tearDown(self):
        self.tmp.cleanup()

    def werkzeug(self, *args):
        r = subprocess.run([sys.executable, WERKZEUG, *args], env=self.env, capture_output=True, text=True,
                           cwd=self.home)
        return r.returncode, r.stdout + r.stderr

    def stand(self):
        """Inhalt und Änderungszeit aller Dateien in whatsapp/ und jobs/."""
        out = {}
        for wurzel in (self.wa, os.path.join(self.home, "jobs")):
            for pfad, _, dateien in os.walk(wurzel):
                for d in dateien:
                    p = os.path.join(pfad, d)
                    out[p] = (lies(p), os.stat(p).st_mtime_ns)
        return out

    def test_einrichten_legt_kontakte_an(self):
        code, aus = self.werkzeug("einrichten")
        self.assertEqual(code, 0, aus)
        kontakte = lies(os.path.join(self.wa, "kontakte.md"))
        self.assertNotIn("{{", kontakte)
        self.assertIn("Testperson", kontakte)
        self.assertRegex(lies(os.path.join(self.wa, "personen", "_vorlage.md")), r"(?m)^bereich:")

    def test_einrichten_morgen_ersatz_ohne_platzhalter(self):
        morgen = os.path.join(self.home, "jobs", "morgen.md")
        schreib(morgen, '---\nzeit: "07:30"\n---\nEigener Morgen-Check ohne Vorschläge.\n')
        code, aus = self.werkzeug("einrichten")
        self.assertEqual(code, 0, aus)
        ersatz = lies(os.path.join(self.home, "jobs", "whatsapp-morgen.md"))
        self.assertNotIn("{{", ersatz)
        self.assertIn("Testperson", ersatz)
        self.assertIn("[knopf: Familie]", ersatz)

    def test_nachziehen_alte_einrichtung(self):
        self.assertEqual(self.werkzeug("einrichten")[0], 0)
        # Stand einer älteren Fassung herstellen
        claude_md = os.path.join(self.wa, "CLAUDE.md")
        alt = lies(claude_md)
        alt = re.sub(r"(?m)^## Neue Kontakte\n\naus$", "## Neue Kontakte\n\nan", alt)
        alt = re.sub(r"Eingeschaltet am \d{4}-\d{2}-\d{2}", "Eingeschaltet am 2026-01-15", alt)
        alt = alt.replace("# WhatsApp (Stay Close)", "# WhatsApp")
        schreib(claude_md, alt)
        schreib(os.path.join(self.wa, "verfahren.md"), "altes Verfahren\n")
        schreib(os.path.join(self.wa, "personen", "_vorlage.md"), "---\ntype: person\nkreis: weiter\n---\n")
        os.remove(os.path.join(self.wa, "kontakte.md"))
        report = os.path.join(self.wa, "Sweep-Report.md")
        schreib(report, lies(report).replace("## Bereich unklar\n\n", "").replace("Noch kein Lauf.", "Lauf von gestern."))
        notiz = os.path.join(self.wa, "personen", "Alex Test.md")
        schreib(notiz, "---\ntype: person\nkreis: eng\n---\n\n# Alex Test\n\nEigene Zeilen.\n")
        state = os.path.join(self.wa, "Sweep-State.md")
        state_text = lies(state) + "\neigene Zeile\n"
        schreib(state, state_text)
        schreib(os.path.join(self.wa, "heute.md"), "datum: 2026-01-15\nkeine\n")
        nacht = os.path.join(self.home, "jobs", "whatsapp-nacht.md")
        nacht_kopf = lies(nacht).split("---\n", 2)[1].replace('zeit: "02:00"', 'zeit: "03:00"')
        schreib(nacht, "---\n" + nacht_kopf + "---\nalter Text\n")
        ersatz = os.path.join(self.home, "jobs", "whatsapp-morgen.md")
        schreib(ersatz, '---\nzeit: "06:30"\nmodell: klein\n---\nalter Text mit {{PERSON}}\n')

        code, aus = self.werkzeug("nachziehen")
        self.assertEqual(code, 0, aus)
        self.assertIn("nachgezogen", aus)

        claude = lies(claude_md)
        self.assertIn("# WhatsApp (Stay Close)", claude)
        self.assertIn("Eingeschaltet am 2026-01-15", claude)
        self.assertRegex(claude, r"(?m)^## Neue Kontakte\n\nan$")
        self.assertIn("Alex ist Familie", claude)
        verfahren = lies(os.path.join(self.wa, "verfahren.md"))
        self.assertIn("Bereich und Kontakte", verfahren)
        self.assertIn("Testperson", verfahren)
        self.assertRegex(lies(os.path.join(self.wa, "personen", "_vorlage.md")), r"(?m)^bereich:")
        self.assertIn("| Name | Bereich | Zuletzt |", lies(os.path.join(self.wa, "kontakte.md")))
        r = lies(report)
        self.assertEqual(r.count("## Bereich unklar"), 1)
        self.assertLess(r.index("## Ohne Notiz"), r.index("## Bereich unklar"))
        self.assertLess(r.index("## Bereich unklar"), r.index("## Nicht transkribiert"))
        self.assertIn("Lauf von gestern.", r)
        self.assertIn("Eigene Zeilen.", lies(notiz))
        self.assertEqual(lies(state), state_text)
        self.assertEqual(lies(os.path.join(self.wa, "heute.md")), "datum: 2026-01-15\nkeine\n")
        n = lies(nacht)
        self.assertIn('zeit: "03:00"', n)
        self.assertIn("verfahren.md", n)
        self.assertNotIn("alter Text", n)
        m = lies(ersatz)
        self.assertTrue(m.startswith('---\nzeit: "06:30"\nmodell: klein\n---\n'))
        self.assertIn("[knopf: Business]", m)
        self.assertIn("Testperson", m)
        for pfad, (text, _) in self.stand().items():
            self.assertNotIn("{{", text, pfad)

        # zweiter Lauf: nichts ändert sich, auch eine gepflegte kontakte.md bleibt
        kontakte = os.path.join(self.wa, "kontakte.md")
        schreib(kontakte, lies(kontakte) + "| Alex Test | familie | 2026-01-14 |\n")
        vorher = self.stand()
        code, aus = self.werkzeug("nachziehen")
        self.assertEqual(code, 0, aus)
        self.assertIn("auf dem Stand", aus)
        self.assertEqual(self.stand(), vorher)

    def test_nachziehen_ohne_whatsapp(self):
        self.assertFalse(os.path.exists(self.wa))
        code, aus = self.werkzeug("nachziehen")
        self.assertEqual(code, 0, aus)
        self.assertIn("nicht eingerichtet", aus)
        self.assertFalse(os.path.exists(self.wa))

    def test_nachziehen_ohne_morgen_ersatz_legt_keinen_an(self):
        self.assertEqual(self.werkzeug("einrichten")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "whatsapp-morgen.md")))
        self.assertEqual(self.werkzeug("nachziehen")[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "whatsapp-morgen.md")))


if __name__ == "__main__":
    unittest.main()
