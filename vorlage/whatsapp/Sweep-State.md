---
type: sweep-state
updated: {{HEUTE}}
---

# Sweep-State

Der Lesestand: bis wohin der Nachtlauf zuletzt vollständig gelesen hat. Weitergesetzt wird er nur, wenn der Lauf jeden Chat aus dem Delta bearbeitet hat. Der erste Lauf liest 30 Tage zurück.

## Lesestand

| Was | Zeitstempel | Gesetzt am |
|---|---|---|
| Chatliste | {{START}} | {{HEUTE}} |

## Namensbrücke

Abweichungen zwischen Chatname und Notiz, einmal geklärt, damit kein Lauf sie neu rät.

| Im Chat | Notiz | Anmerkung |
|---|---|---|

## Bewusst ausgenommen

Der Schalter ist `sweep: false` in der Notiz. Hier steht nur, warum.

| Chat | Grund |
|---|---|

## Vorschläge

Wer an welchem Morgen vorgeschlagen wurde. Wer hier in den letzten sieben Tagen steht, kommt nicht noch einmal.

| Datum | Person | Anlass |
|---|---|---|
