#!/usr/bin/env python3
"""Testfragen gegen eine echte Instanz: stellt jede Frage als eigenes Gespräch (ohne Telegram, ohne Zustand)
und schreibt Soll und Ist nebeneinander nach ~/assistent/testergebnisse/<datum>.md. Bewertet wird von Hand
oder in einer Claude-Session: stimmt, teilweise, falsch. Läuft als der User der Person und verbraucht
etwas von ihrem Kontingent (je Frage ein bis drei Minuten).

  runuser -u a-anna -- env HOME=/home/a-anna python3 /opt/pearl-kit/tests/lauf.py [--nur T1,T2]

Fragen: tests/testfragen.md aus dem Kit, dazu home/testfragen.md der Instanz, falls vorhanden.
"""
import argparse
import datetime
import os
import re
import sys
import time

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HIER))
import assistent  # noqa: E402


def fragen(pfad):
    if not os.path.exists(pfad):
        return
    text = open(pfad, encoding="utf-8").read()
    for block in re.split(r"^## ", text, flags=re.M)[1:]:
        kopf, _, rest = block.partition("\n")
        m = re.match(r"(T\d+)\s*(.*)", kopf.strip())
        felder = dict(re.findall(r"^(Frage|Soll):\s*(.+)$", rest, flags=re.M))
        if m and "Frage" in felder:
            yield {"id": m.group(1), "kategorie": m.group(2).strip(), **felder}


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--nur")
    x = a.parse_args()
    alle = list(fragen(os.path.join(HIER, "testfragen.md"))) + list(fragen(os.path.join(assistent.HOME_DIR, "testfragen.md")))
    auswahl = [f for f in alle if not x.nur or f["id"] in x.nur.split(",")]
    ordner = os.path.join(assistent.INSTANZ, "testergebnisse")
    os.makedirs(ordner, exist_ok=True)
    ziel = os.path.join(ordner, f"{datetime.date.today().isoformat()}.md")
    with open(ziel, "a", encoding="utf-8") as out:
        out.write(f"# Testlauf {datetime.datetime.now():%d.%m.%Y %H:%M}, {len(auswahl)} Fragen\n\n")
        for f in auswahl:
            t0 = time.time()
            antwort, sitzung, fehler, kontext = assistent.ki_lauf(
                f"[Telegram, {assistent.stempel()}] {assistent.PERSON}: [Test: bitte nichts senden.] {f['Frage']}",
                effort="medium")
            ist = f"FEHLER: {fehler}" if fehler else antwort.strip()
            out.write(f"## {f['id']} {f['kategorie']}\nFrage: {f['Frage']}\nSoll: {f.get('Soll', '')}\nIst: {ist}\n"
                      f"({time.time() - t0:.0f} s, {kontext // 1000}k Tokens)\nBewertung: \n\n")
            out.flush()
            print(f"{f['id']}: {ist[:100]}")
    print(f"\nErgebnis: {ziel}")


if __name__ == "__main__":
    main()
