#!/usr/bin/env python3
"""Legt eine Datei aus home/eingang/ dauerhaft ab: home/leben/ablage/<jahr>/<datum>-<name>.<endung>.

Fotos, PDFs und Sprachnachrichten aus Telegram landen zuerst in eingang/ und werden nach 30 Tagen gelöscht.
Was bleiben soll (Vertrag, Rechnung, Bescheid, Rezept), kommt hierher und ist ab dann in der Sicherung.

  python3 /opt/pearl-kit/werkzeuge/ablegen.py eingang/2026-11-20-foto.jpg "rechnung zahnarzt"
"""
import datetime
import os
import re
import shutil
import sys

INSTANZ = os.environ.get("ASSISTENT_DIR") or os.path.expanduser("~/assistent")
HOME_DIR = os.path.join(INSTANZ, "home")


def sauber(name):
    name = name.strip().lower().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:60] or "datei"


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    quelle = os.path.realpath(os.path.join(HOME_DIR, sys.argv[1]))
    eingang = os.path.realpath(os.path.join(HOME_DIR, "eingang"))
    if not quelle.startswith(eingang + os.sep) or not os.path.isfile(quelle):
        sys.exit(f"Nur Dateien aus eingang/ lassen sich ablegen, {sys.argv[1]} ist keine")
    heute = datetime.date.today()
    endung = os.path.splitext(quelle)[1].lower()
    ordner = os.path.join(HOME_DIR, "leben", "ablage", str(heute.year))
    os.makedirs(ordner, exist_ok=True)
    ziel = os.path.join(ordner, f"{heute.isoformat()}-{sauber(sys.argv[2])}{endung}")
    n = 2
    while os.path.exists(ziel):
        ziel = os.path.join(ordner, f"{heute.isoformat()}-{sauber(sys.argv[2])}-{n}{endung}")
        n += 1
    shutil.copy2(quelle, ziel)
    print(f"Abgelegt: {os.path.relpath(ziel, HOME_DIR)}")


if __name__ == "__main__":
    main()
