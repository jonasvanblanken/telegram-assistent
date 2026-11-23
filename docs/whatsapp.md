# WhatsApp-Erweiterung

Für Menschen, die schon einen Assistenten auf ihrem eigenen Server haben. Der Assistent liest nachts die Einzelchats mit, pflegt zu den Menschen im engen Kreis eine kurze Notiz und sagt morgens, bei wem sich eine Nachricht lohnt. Schreiben kann er in WhatsApp nicht.

## Einschalten

Wer den Assistenten neu einrichtet, sieht auf der Einrichtungsseite nach Kalender und Mail den Schritt WhatsApp. Bei „Ja, WhatsApp dazu“ baut der Server sofort, der QR-Code kommt im Chat, sobald die Einrichtung durch ist. Bei „Ohne WhatsApp weiter“ oder bei älteren Einrichtungen bietet der Assistent WhatsApp einmal von selbst an, tagsüber und frühestens drei Tage nach der Begrüßung, mit den Knöpfen „WhatsApp verbinden“, „Später“ und „Nein danke“. Ohne Antwort fragt er nach einer Woche ein zweites Mal, danach nur noch auf `/whatsapp`. „WhatsApp verbinden“ öffnet die Seite `/whatsapp` der Einrichtungsseite in Telegram (eigener Link, gilt sieben Tage). Dort stehen Funktionen und Risiken, eingewilligt wird mit „Einverstanden, verbinden“. Dann baut der Server die Verbindung (beim ersten Mal 10 bis 15 Minuten). Sobald der QR-Code da ist, meldet sich der Assistent im Chat mit dem Knopf „QR-Code zeigen“. Die Seite zeigt den Code, er erneuert sich von selbst und verlässt den Server nie. Gescannt wird mit dem Handy unter Verknüpfte Geräte, die Seite muss deshalb auf einem zweiten Bildschirm offen sein, etwa Telegram am Laptop. Nach dem Scan fragt der Assistent, wer eng und wer nah ist, und legt dazu Notizen an. Der erste Nachtlauf um 02:00 liest 30 Tage zurück.

- `/whatsapp` zeigt den Stand
- `/whatsapp_neu` richtet neu ein, etwa wenn das Handy die Verknüpfung gelöst hat
- `/whatsapp_aus` trennt alles und räumt den Server auf, die Notizen bleiben

Nur auf dem eigenen Server (`box/selbst.sh`). Auf einer Box mit mehreren Personen könnte jeder Linux-User die Bridge auf 127.0.0.1 lesen, dort lehnt der Assistent ab.

## Was die Person wissen muss

- Die Verbindung läuft über ein inoffizielles Programm, das sich wie WhatsApp Web anmeldet. WhatsApp kann die Verknüpfung sperren, im schlimmsten Fall die Nummer
- Die Kontakte haben nicht zugestimmt. Der Assistent hat eine kurze Nachricht für sie parat, wer nicht will, bekommt `sweep: false`
- Die Nachrichten liegen verschlüsselt auf dem Server. Gelesen werden sie nur nachts von der KI der Person und auf ihre ausdrückliche Frage im Chat

## Aufbau

| Teil | Wo | Was |
|---|---|---|
| Bridge | `whatsapp/`, gebaut von `box/whatsapp.sh` | adelaidasofia/whatsapp-mcp v0.4.1 (MIT) mit den Patches aus `whatsapp/patches/`, verknüpftes Gerät, SQLCipher, Port 8080 nur lokal |
| Connector | `whatsapp/server_http.py` | fünf lesende Werkzeuge mit Bearer-Token, Port 8081 nur lokal |
| Auftrag | `systemd/assistent-whatsapp.path` | der Bot schreibt `~/.local/state/assistent/whatsapp-auftrag`, root baut, Stand in `/var/lib/pearl-kit/whatsapp/stand` |
| Werkzeug | `werkzeuge/whatsapp.py` | `hole` (Delta je Chat bis zum Lesestand, Warteschlange ab 25 Chats), `pruefe`, `status`, `chats`, `lies`, `einrichten`, `abschalten` |
| Notizen | `home/whatsapp/` aus `vorlage/whatsapp/` | Regeln, Verfahren des Nachtlaufs, Lesestand, Bericht, eine Notiz je Mensch |
| Läufe | `whatsapp-nacht` 02:00, `whatsapp-kreis` einmal nach dem Scan | die Vorschläge stehen im Morgen-Check, sonst in `whatsapp-morgen` 07:45 |

Die KI bekommt keinen WhatsApp-Connector. Sie sieht Nachrichtentext nur im Delta des Nachtlaufs und über `whatsapp.py lies`, wenn die Person danach fragt. Kennungen und Nummern bleiben außerhalb von `home/`.

## Stolperfallen

- Kontaktnamen kommen erst nach einem Neustart der Bridge rund zwei Minuten nach dem Scan an, das Skript macht das selbst
- Koppeln per Telefonnummer funktioniert mit dieser Bridge nicht, nur der QR-Code
- Löst das Handy die Verknüpfung, meldet die Prüfung nach dem Nachtlauf das der Person. `/whatsapp_neu` zeigt einen neuen Code
- Protokoll auf dem Server: `/var/log/assistent-whatsapp.log`
