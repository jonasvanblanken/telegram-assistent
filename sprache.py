#!/usr/bin/env python3
"""Sprachnachricht in Text, auf dem Server selbst (faster-whisper, Modell small, int8). Nichts verlässt den Server.

  /opt/pearl-kit-ki/sprache/bin/python3 sprache.py <datei> [--sprache de] [--modell <ordner>] [--kerne N]

Läuft mit der Python-Umgebung aus box/sprache.sh, nicht mit dem System-Python. Der Dienst ruft es je Nachricht als
eigenen Prozess auf, damit der Speicher (rund 700 MB) danach wieder frei ist. Ausgabe: der Text auf stdout.
Gemessen am 27.11.2026 auf AMD EPYC: 45 Sekunden Sprache in 5,6 s mit zwei Kernen, 9,8 s mit einem.
"""
import argparse
import os
import sys

MODELL = os.environ.get("ASSISTENT_SPRACHE_MODELL") or "/opt/pearl-kit-ki/sprache/modell"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("datei")
    ap.add_argument("--sprache", default="de")
    ap.add_argument("--modell", default=MODELL)
    ap.add_argument("--kerne", type=int, default=min(os.cpu_count() or 1, 4))
    a = ap.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"   # das Modell liegt fertig auf dem Server, nie nachladen
    from faster_whisper import WhisperModel
    m = WhisperModel(a.modell, device="cpu", compute_type="int8", cpu_threads=max(a.kerne, 1))
    teile, _ = m.transcribe(a.datei, language=a.sprache or None, beam_size=1, vad_filter=False,
                            condition_on_previous_text=False)
    text = " ".join(t.text.strip() for t in teile).strip()
    sys.stdout.write(text)
    return 0 if text else 3


if __name__ == "__main__":
    sys.exit(main())
