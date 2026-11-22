#!/usr/bin/env python3
"""Suche über alles, was der Assistent je gewusst hat.

Durchsucht Profil, Gedaechtnis (auch _archiv), Akten in leben/ (ohne Journal), Logbuch, die Mitschriften
der letzten 14 Tage, die Transkripte ihrer Gespraeche und deren Archiv (zstd). Alle Woerter muessen im
selben Absatz bzw. in derselben Nachricht stehen, Gross/klein und Umlaute egal (ä = ae = a).

  python3 /opt/pearl-kit/werkzeuge/erinnern.py "zahnarzt termin"   alles
  python3 /opt/pearl-kit/werkzeuge/erinnern.py "versicherung" --seit 2026-10-01  nur Logbuch, Mitschriften und Gespraeche ab Datum
  python3 /opt/pearl-kit/werkzeuge/erinnern.py "umzug" --nur akten         nur eine Quelle: profil, gedaechtnis, akten, logbuch, gespraeche
  python3 /opt/pearl-kit/werkzeuge/erinnern.py "…" --max 30 --journal   mehr Treffer, Journal einschließen (nur wenn die Person das Thema öffnet)
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys

INSTANZ = os.environ.get("ASSISTENT_DIR") or os.path.expanduser("~/assistent")
HOME_DIR = os.path.join(INSTANZ, "home")
HOME = os.path.expanduser("~")
PROJEKT = re.sub(r"[^A-Za-z0-9]", "-", HOME_DIR)   # so benennt Claude Code den Ordner der Transkripte
TRANSKRIPTE = [os.path.join(HOME, ".claude/projects", PROJEKT), os.path.join(HOME, ".claude/projects-archiv", PROJEKT)]
VERLAUF = os.path.join(os.environ.get("ASSISTENT_STATE") or os.path.join(HOME, ".local/state/assistent"), "verlauf")
UMLAUTE = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss"})


def norm(s):
    return s.casefold().translate(UMLAUTE).replace("ae", "a").replace("oe", "o").replace("ue", "u")


def passt(text, woerter):
    t = norm(text)
    return all(w in t for w in woerter)


def ausschnitt(text, woerter, breite=220):
    t = norm(text)
    i = max(t.find(woerter[0]), 0)
    # norm() kann die Laenge veraendern (ae -> a); Ausschnitt deshalb grob um die Fundstelle im Original
    i = min(i, len(text))
    a, b = max(0, i - breite // 2), min(len(text), i + breite)
    s = re.sub(r"\s+", " ", text[a:b]).strip()
    return ("… " if a else "") + s + (" …" if b < len(text) else "")


def datum_aus(pfad):
    m = re.search(r"(\d{4}-\d\d-\d\d)", os.path.basename(pfad))
    if m:
        return m.group(1)
    return datetime.date.fromtimestamp(os.path.getmtime(pfad)).isoformat()


def markdown_quellen(nur, journal):
    q = []
    if nur in (None, "profil"):
        q += [("Profil", f) for f in glob.glob(os.path.join(HOME_DIR, "profil", "*.md"))]
    if nur in (None, "gedaechtnis"):
        q += [("Gedächtnis", f) for f in glob.glob(os.path.join(HOME_DIR, "gedaechtnis", "**", "*.md"), recursive=True)]
    if nur in (None, "akten"):
        for f in glob.glob(os.path.join(HOME_DIR, "leben", "**", "*.md"), recursive=True):
            rel = os.path.relpath(f, os.path.join(HOME_DIR, "leben"))
            if rel.startswith("logbuch/") or (rel.startswith("Journal/") and not journal):
                continue
            q.append(("Akte", f))
    if nur in (None, "logbuch"):
        q += [("Logbuch", f) for f in glob.glob(os.path.join(HOME_DIR, "leben", "logbuch", "*.md"))]
        q += [("Mitschrift", f) for f in glob.glob(os.path.join(VERLAUF, "*.md"))]
    return q


def suche_markdown(woerter, nur, journal, seit, bis):
    for art, pfad in markdown_quellen(nur, journal):
        datiert = art in ("Logbuch", "Mitschrift")
        d = datum_aus(pfad)
        if datiert and ((seit and d < seit) or (bis and d > bis)):
            continue
        try:
            text = open(pfad, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if not all(w in norm(text) for w in woerter):
            continue
        teile = re.split(r"\n\s*\n", text) if art != "Mitschrift" else text.splitlines()
        for teil in teile:
            if passt(teil, woerter):
                zeit = ""
                m = re.match(r"\*\*(\d\d:\d\d) |\[(\d\d:\d\d)\]", teil.strip())
                if m:
                    zeit = m.group(1) or m.group(2)
                yield {"datum": d, "zeit": zeit, "art": art, "ort": os.path.relpath(pfad, HOME_DIR) if pfad.startswith(HOME_DIR) else pfad,
                       "text": ausschnitt(teil, woerter)}


def texte(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text") or "")
    return "\n".join(out)


def zeilen(pfad):
    if pfad.endswith(".zst"):
        try:
            p = subprocess.run(["zstd", "-dc", pfad], capture_output=True, timeout=120)
            return p.stdout.decode("utf-8", "replace").splitlines()
        except (OSError, subprocess.TimeoutExpired):
            return []
    try:
        return open(pfad, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return []


def suche_gespraeche(woerter, seit, bis):
    roh = [w for w in woerter]
    for ordner in TRANSKRIPTE:
        for pfad in glob.glob(os.path.join(ordner, "*.jsonl")) + glob.glob(os.path.join(ordner, "*.jsonl.zst")):
            d = datetime.date.fromtimestamp(os.path.getmtime(pfad)).isoformat()
            if seit and d < seit:   # zuletzt geaendert vor dem Zeitraum: kann nichts Neueres enthalten
                continue
            for z in zeilen(pfad):
                if '"type":"user"' not in z and '"type":"assistant"' not in z:
                    continue
                if not all(w in norm(z) for w in roh):
                    continue
                try:
                    e = json.loads(z)
                except ValueError:
                    continue
                if e.get("type") not in ("user", "assistant") or e.get("isSidechain"):
                    continue
                text = texte((e.get("message") or {}).get("content"))
                if not text or not passt(text, woerter):
                    continue
                ts = (e.get("timestamp") or "")[:19]
                try:
                    lokal = datetime.datetime.fromisoformat(ts + "+00:00").astimezone()
                except ValueError:
                    continue
                if (seit and lokal.date().isoformat() < seit) or (bis and lokal.date().isoformat() > bis):
                    continue
                wer = "Assistent" if e["type"] == "assistant" else "Eingang"
                if e["type"] == "user" and text.startswith("[Telegram,"):
                    wer = "Person"
                elif e["type"] == "user" and text.startswith("[Zeitplan"):
                    wer = "Zeitplan"
                yield {"datum": lokal.date().isoformat(), "zeit": f"{lokal:%H:%M}", "art": f"Gespräch ({wer})",
                       "ort": os.path.basename(pfad)[:8], "text": ausschnitt(text, woerter)}


def main():
    a = argparse.ArgumentParser(description="Suche über alles, was der Assistent je gewusst hat")
    a.add_argument("stichworte")
    a.add_argument("--seit")
    a.add_argument("--bis")
    a.add_argument("--max", type=int, default=15)
    a.add_argument("--nur", choices=["profil", "gedaechtnis", "akten", "logbuch", "gespraeche"])
    a.add_argument("--journal", action="store_true")
    x = a.parse_args()
    woerter = [norm(w) for w in x.stichworte.split() if len(w) > 1]
    if not woerter:
        sys.exit("Bitte mindestens ein Stichwort mit zwei Buchstaben")
    treffer = []
    if x.nur != "gespraeche":
        treffer += list(suche_markdown(woerter, x.nur, x.journal, x.seit, x.bis))
    if x.nur in (None, "gespraeche"):
        treffer += list(suche_gespraeche(woerter, x.seit, x.bis))
    gesehen, eindeutig = set(), []
    for t in sorted(treffer, key=lambda t: (t["datum"], t["zeit"]), reverse=True):
        schluessel = norm(t["text"])[:160]
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)
        eindeutig.append(t)
    if not eindeutig:
        print(f"Nichts gefunden zu „{x.stichworte}“. Andere Wörter versuchen (Name, Ort, Betrag) oder ein Wort weniger.")
        return
    print(f"{len(eindeutig)} Treffer zu „{x.stichworte}“, neueste zuerst"
          + (f", die ersten {x.max}" if len(eindeutig) > x.max else "") + ":\n")
    for t in eindeutig[:x.max]:
        print(f"{t['datum']} {t['zeit']} · {t['art']} · {t['ort']}\n  {t['text']}\n")


if __name__ == "__main__":
    main()
