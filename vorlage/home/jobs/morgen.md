---
zeit: "07:30"
modell: standard
effort: medium
gespraech: eigenes
melden: antwort
ausweichen: ja
---
Morgen-Check für {{PERSON}}. Eine kurze, freundliche Nachricht zum Tagesbeginn, höchstens sechs Zeilen, die WhatsApp-Entwürfe kommen dazu.

Schau nach:
- `profil/laufend.md` und die Akten mit Fristen: Was ist heute oder in den nächsten Tagen fällig?
<!-- google -->
- Der Kalender von heute (`list_calendars`, dann `get_events` für jeden, den {{PERSON}} nutzt): Was steht an, wann muss {{PERSON}} los?
<!-- /google -->
<!-- whatsapp -->
- `whatsapp/heute.md`: Steht dort das heutige Datum und ein Name, nennst du je Mensch in einer Zeile Name, Bereich und warum sich heute eine Nachricht lohnt, in der Reihenfolge der Datei (Familie, Privat, Business), und darunter den Entwurf aus der Datei, unverändert und ohne „Entwurf:“ davor, damit {{PERSON}} ihn kopieren und in WhatsApp abschicken kann. Steht bei jemandem „Bereich offen“, fragst du ganz am Ende für den ersten davon in einem Satz, wohin er gehört, und hängst die drei Zeilen `[knopf: Familie]`, `[knopf: Privat]` und `[knopf: Business]` an
<!-- /whatsapp -->
<!-- brain -->
- `brain/COCKPIT.md`: Was unter „Fristen“ heute oder in den nächsten Tagen fällig ist und was unter „Wartet auf {{PERSON}}“ steht, gehört mit in den Morgen-Check, je Punkt höchstens eine Zeile
<!-- /brain -->
- `profil/fragen.md`: Steht dort eine offene Frage, stellst du am Ende genau eine davon, beiläufig und einfach zu beantworten (in der Kennenlernwoche bis zu zwei, siehe CLAUDE.md). Ist die Datei leer, keine Frage
- Fehlt dir etwas für einen guten Morgen-Check, bittest du am Ende darum, statt die Lücke zu übergehen (Regel in CLAUDE.md, Abschnitt Kennenlernen, Vermerk unter `## Gebeten` in `leben/kennenlernen.md`). Etwa:
<!-- google -->
  noch kein Kalender („Damit ich dir morgens sagen kann, was ansteht, gib mir Zugriff auf deinen Kalender“ und darunter die Zeile `[einstellungen: kalender]`),
<!-- /google -->
  keine To-dos in `leben/todo.md` („Schick mir, was diese Woche erledigt werden muss, dann behalte ich es im Blick“), keine Verträge in `leben/vertraege.md`. Eine Bitte je Morgen, und sie ersetzt dann die Frage aus `profil/fragen.md`

Schreib nur, was heute zählt. Gibt es nichts, reicht ein Gruß mit einem Satz zum Tag. Kein Wetterbericht, keine Aufzählung von Dingen, die nicht anstehen.
