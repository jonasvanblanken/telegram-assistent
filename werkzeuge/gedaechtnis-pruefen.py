#!/usr/bin/env python3
"""Prüft das Gedächtnis des Assistenten ohne Modell.

Laeuft als `pruefen:` am Wochenrueckblick: Exit 0 heisst alles in Ordnung, Exit 1 schickt die Ausgabe an die Person.

- MEMORY.md gegen die Ladegrenze von Claude Code (200 Zeilen oder 25 KB, was zuerst kommt; Warnung ab 70 %)
- jede Datei im Gedaechtnis steht im Index, kein Link zeigt ins Leere
- Profil vorhanden und in seinen Grenzen, laufend.md frisch
- Akten, die ein Zeitplan liest, nicht aelter als 30 Tage
- an jedem Tag mit Gespraech gibt es ein Logbuch

  python3 /opt/pearl-kit/werkzeuge/gedaechtnis-pruefen.py [--alles]   --alles zeigt auch, was in Ordnung ist
"""
import datetime
import glob
import os
import re
import sys

INSTANZ = os.environ.get("ASSISTENT_DIR") or os.path.expanduser("~/assistent")
HOME_DIR = os.path.join(INSTANZ, "home")
GED = os.path.join(HOME_DIR, "gedaechtnis")
PROFIL = os.path.join(HOME_DIR, "profil")
LOGBUCH = os.path.join(HOME_DIR, "leben", "logbuch")
VERLAUF = os.path.join(os.environ.get("ASSISTENT_STATE") or os.path.expanduser("~/.local/state/assistent"), "verlauf")
GRENZE_ZEILEN, GRENZE_BYTES = 200, 25_000
PROFIL_GRENZEN = {"assistent.md": 25, "person.md": 70, "laufend.md": 45}
HEUTE = datetime.date.today()


def stand(pfad):
    """Datum aus `stand:` im Kopf, sonst Aenderungsdatum. `stand: dauerhaft` (Nachschlagewerk) zaehlt nie als alt."""
    try:
        kopf = open(pfad, encoding="utf-8", errors="replace").read(2000)
    except OSError:
        return None
    if re.search(r"^stand:\s*dauerhaft", kopf, flags=re.M):
        return None
    m = re.search(r"^stand:\s*\"?(\d{4}-\d\d-\d\d)", kopf, flags=re.M)
    if m:
        return datetime.date.fromisoformat(m.group(1))
    return datetime.date.fromtimestamp(os.path.getmtime(pfad))


def main():
    warn, ok = [], []
    index = os.path.join(GED, "MEMORY.md")
    text = open(index, encoding="utf-8").read() if os.path.exists(index) else ""
    zeilen, nbytes = text.count("\n") + 1, len(text.encode())
    anteil = max(zeilen / GRENZE_ZEILEN, nbytes / GRENZE_BYTES)
    if anteil >= 1:
        warn.append(f"Der Gedächtnis-Index ist über der Ladegrenze ({zeilen} Zeilen, {nbytes // 1000} KB): die letzten Zeilen lädt "
                    "Claude nicht mehr, ich vergesse gerade das Neueste. Index kürzen, Details in die Dateien")
    elif anteil >= 0.7:
        warn.append(f"Der Gedächtnis-Index ist bei {anteil:.0%} der Ladegrenze ({zeilen} Zeilen, {nbytes // 1000} KB), "
                    "Zeit zum Zusammenlegen")
    else:
        ok.append(f"Index {zeilen} Zeilen, {nbytes // 1000} KB ({anteil:.0%} der Grenze)")
    lang = sum(1 for z in text.splitlines() if len(z) > 150)
    if lang > 5:
        warn.append(f"{lang} Zeilen im Gedächtnis-Index sind länger als 150 Zeichen")
    verlinkt = set(re.findall(r"\]\(([^)#\s]+\.md)\)", text))
    dateien = {os.path.relpath(f, GED) for f in glob.glob(os.path.join(GED, "**", "*.md"), recursive=True)}
    dateien = {f for f in dateien if f != "MEMORY.md" and not f.startswith(("_archiv/", "stil/"))}
    fehlen = sorted(dateien - verlinkt)
    tot = sorted(v for v in verlinkt if not os.path.exists(os.path.join(GED, v)))
    if fehlen:
        warn.append(f"{len(fehlen)} Gedächtniseinträge stehen nicht im Index und werden nie gelesen: {', '.join(fehlen[:6])}")
    if tot:
        warn.append(f"{len(tot)} Links im Gedächtnis-Index zeigen ins Leere: {', '.join(tot[:6])}")
    if not fehlen and not tot:
        ok.append(f"{len(dateien)} Einträge, alle im Index")
    projekte = []
    for f in dateien:
        kopf = open(os.path.join(GED, f), encoding="utf-8", errors="replace").read(800)
        if re.search(r"^\s*type:\s*project", kopf, flags=re.M):
            projekte.append(f)
    if len(projekte) > 10:
        warn.append(f"{len(projekte)} Einträge im Gedächtnis sind Zustände (type: project), die gehören in die Akten")
    for name, grenze in PROFIL_GRENZEN.items():
        p = os.path.join(PROFIL, name)
        if not os.path.exists(p):
            warn.append(f"profil/{name} fehlt, ohne die Datei kennt mich ein neues Gespräch nicht richtig")
            continue
        n = sum(1 for _ in open(p, encoding="utf-8"))
        s = stand(p)
        if n > grenze:
            warn.append(f"profil/{name} hat {n} Zeilen, Grenze {grenze}")
        if name == "laufend.md" and s and (HEUTE - s).days > 7:
            warn.append(f"profil/laufend.md ist seit {s:%d.%m.} nicht neu geschrieben, das macht der Tagesabschluss")
        if name == "person.md" and s and (HEUTE - s).days > 60:
            warn.append(f"profil/person.md hat Stand {s:%d.%m.}, bitte gemeinsam durchgehen")
    gelesen = set()
    for job in glob.glob(os.path.join(HOME_DIR, "jobs", "*.md")):
        gelesen.update(re.findall(r"`(leben/[\w./-]+\.md)`", open(job, encoding="utf-8", errors="replace").read()))
    alt = []
    for rel in sorted(gelesen):
        p = os.path.join(HOME_DIR, rel)
        s = stand(p) if os.path.exists(p) else None
        if s and (HEUTE - s).days > 30:
            alt.append(f"{rel} ({s:%d.%m.})")
    if alt:
        warn.append(f"Akten, die meine Zeitpläne lesen, sind über 30 Tage alt: {', '.join(alt)}")
    ohne = []
    for v in glob.glob(os.path.join(VERLAUF, "*.md")):
        tag = os.path.basename(v)[:-3]
        if tag < HEUTE.isoformat() and not os.path.exists(os.path.join(LOGBUCH, f"{tag}.md")):
            ohne.append(tag)
    if ohne:
        warn.append(f"Kein Logbuch für Tage mit Gespräch: {', '.join(sorted(ohne))}")
    if warn:
        print("Gedächtnis-Prüfung:\n" + "\n".join(f"- {w}" for w in warn))
    if "--alles" in sys.argv or not warn:
        print("In Ordnung:\n" + "\n".join(f"- {o}" for o in ok) if ok else "")
    sys.exit(1 if warn else 0)


if __name__ == "__main__":
    main()
