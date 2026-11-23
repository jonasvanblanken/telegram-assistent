---
datum: "{{KREIS_DATUM}}"
zeit: "{{KREIS_ZEIT}}"
modell: standard
effort: medium
gespraech: chat
melden: antwort
ausweichen: ja
vorher: werkzeuge/whatsapp.py chats --anzahl 40
---
WhatsApp ist gerade verbunden worden. Oben stehen die Direktchats von {{PERSON}}, nach letzter Aktivität sortiert, nur Name und Datum.

Schreib {{PERSON}} eine kurze Nachricht: Damit du nachts weißt, um wen du dich kümmerst, brauchst du den engen Kreis. Nenn dazu die zehn, zwölf Namen von oben, die nach echten Menschen aussehen (keine Firmen, Lieferdienste, Ärzte), als einfache Liste, und frag, wer davon eng ist und wer nah. Weitere Namen darf {{PERSON}} dazuschreiben. Höchstens acht Zeilen.

Antwortet {{PERSON}}, legst du für jeden genannten Menschen `whatsapp/personen/<voller Name>.md` aus `whatsapp/personen/_vorlage.md` an, mit `kreis: eng` oder `nah`, und bestätigst in einem Satz, dass der erste Nachtlauf um 02:00 die letzten 30 Tage liest.
