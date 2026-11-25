# telegram-assistent

Ein persönlicher Assistent in Telegram auf einem eigenen Server. Das Gehirn ist das KI-Abo der Person: Claude (Claude Code), ChatGPT (Codex) oder Gemini (Gemini CLI). Die Person bekommt einen Einladungslink, tippt auf Starten und richtet in Telegram selbst ein: Namen, KI-Anmeldung und optional ihr Google-Konto (Kalender, Mail als Entwurf, Drive, Aufgaben). Eine Box trägt mehrere Personen, jede als eigener Linux-User, die sich gegenseitig nicht sehen.

Gefallen, kein Geschäft: kein Preis, keine Marke, kein Vertrag. Vor dem Start eine kurze Absprache mit der Person ([docs/absprache.md](docs/absprache.md)).

## Eigener Server, ohne Betreuer

Der einfachste Weg: Jede Person mietet sich einen eigenen kleinen Server (Hetzner Cloud, CX23 oder CAX11, Ubuntu 24.04) und fügt beim Anlegen eine Cloud-config ein. Die baut eine Anleitungsseite im Browser aus dem Token ihres Bots und einem zufälligen Einladungscode:

```yaml
#cloud-config
write_files:
  - path: /root/assistent.env
    permissions: "0600"
    content: |
      TG_TOKEN=<Token von @BotFather>
      EINLADUNG=<32 Hex-Zeichen>
      AUSSTATTUNG=<vorab gewählte Kits: whatsapp, ablage, kalender mit Komma, oder ohne>
runcmd:
  - [bash, -c, "git clone -q <dieses Repo> /opt/assistent-quelle && git -C /opt/assistent-quelle checkout -q <Commit> && bash /opt/assistent-quelle/box/selbst.sh"]
```

`box/selbst.sh` richtet den Server ohne Rückfragen ein: Pakete, Caddy mit HTTPS über `<ip>.sslip.io` (Ausweich: nip.io), einen Assistenten ohne Betreuer, Wache über den eigenen Bot. Den Fortschritt schreibt es in die Beschreibung des Bots (`⏳ 1/3` bis `3/3`, Fehler mit `⚠️`), fertig ist er, wenn dort „Dein persönlicher Assistent …“ steht. Danach öffnet die Person `https://t.me/<bot>?start=<EINLADUNG>`, tippt auf Starten und richtet Namen und KI ein. Die Einrichtung zeigt nur die vorab gewählten Kits (`[kits] wahl` in der config.toml, fehlt die Zeile: alle), in den Einstellungen lässt sich jedes Kit später dazunehmen. Google bleibt auf dem eigenen Server vorerst aus. Protokoll: `/var/log/assistent-installation.log`.

Der Rest dieser Datei beschreibt den Betrieb einer gemeinsamen Box mit Betreuer.

## Was die Person bekommt

- Einen eigenen Telegram-Bot mit Namen ihrer Wahl, der nur ihr antwortet
- Text, Sprachnachrichten (mit OpenAI-Key), Fotos und Dateien. Briefe und Rechnungen landen mit Fristen in den Akten
- Morgen-Check um 07:30, kurzer Abend-Check um 21:00, stündlicher Herzschlag (höchstens zwei Nachrichten am Tag), Erinnerungen auf Zuruf
- Ein Gedächtnis, das jede Nacht gesichert wird, und ein Logbuch des ganzen Chats
- Mit Google: Termine lesen und eintragen, Mails lesen und als Entwurf beantworten, Drive, Aufgaben. Senden kann der Assistent nicht

## Aufbau

```
/opt/pearl-kit/                 Code, root-eigen, für alle gleich
  assistent.py                  der Dienst (Telegram, Schlange, Zeitpläne, Sicherung, Puls)
  anbieter.py                   Claude, ChatGPT, Gemini: Programme, Standardmodelle, Einstellungen
  pforte.py                     Einrichtungsseite (Telegram Web App): Namen, KI-Login, Kalender und Mail, Rückweg von Google
  kit.py, einrichten.py         Config lesen, Instanz anlegen und nachziehen
  werkzeuge/                    erinnern.py, gedaechtnis-pruefen.py, ablegen.py
  google/                       workspace-mcp mit Bearer-Prüfung
  vorlage/                      CLAUDE.md, Profil, Zeitpläne, settings und mcp
  box/                          Skripte für root und die Wache
/opt/pearl-kit-ki/              Node, Codex und Gemini CLI, feste Fassungen (box/ki-programme.sh)
/etc/pearl-kit/                 box.env (Wache, Domain), google-client.json (OAuth-Client)
/etc/caddy/pearl-kit/           je Person ein Pfad der Einrichtungsseite
/home/a-<kurz>/                 je Person, chmod 700
  assistent/config.toml         Name, Telegram-ID, KI und Modell, Google-Konto und Dienste, Einrichtungsseite
  assistent/home/               was der Assistent weiß: profil/, gedaechtnis/, leben/, jobs/
  .config/assistent/secrets.env TG_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, GOOGLE_MCP_TOKEN, OPENAI_API_KEY (600)
  .local/state/assistent/       Zustand, Schlange, Mitschriften
  .local/share/assistent/       Anmeldungen und Einstellungen von Codex und Gemini, Google-Zugang
```

Je Person laufen zwei systemd-User-Units: `assistent.service` und, mit Google, `google-mcp.service` auf einem eigenen Port ab 8101, nur auf 127.0.0.1. Der Dienst trägt die Einrichtungsseite auf einem Port ab 8201, ebenfalls nur auf 127.0.0.1. Caddy holt das Zertifikat und reicht `https://<domain>/<kurz>/` dorthin durch. Die Wache (`pearl-kit-wache.timer`, root) schaut alle drei Minuten auf alle und meldet Störungen per Telegram an dich, nie Inhalte.

## Kosten

| Posten | Wer | Monatlich |
|---|---|---|
| KI-Abo: Claude Pro oder Max, ChatGPT Plus oder Pro, oder Gemini | die Person, eigenes Konto | Claude Pro und ChatGPT Plus um 20 bis 25 €, Gemini mit privatem Google-Konto ohne Abo mit Tageskontingent, mehr mit Google AI Pro |
| Box Hetzner CPX32 (4 vCPU, 8 GB), bis etwa fünf Personen | du | um 36 € |
| Box Hetzner CPX42 (8 vCPU, 16 GB), acht bis zehn Personen | du | um 70 € |
| Hetzner-Backup der Box (+20 %) | du | 7–14 € |
| OpenAI für Sprachnachrichten (Whisper, 0,6 Cent je Minute) | wer den Key stellt | meist unter 1 € |

Telegram und Google kosten nichts, die Domain ist eine Subdomain einer vorhandenen. Die kleinen Abos reichen für Chat und Check-ins. Wer viel schreibt oder lange Dokumente schickt, stößt dort ans Limit: Der Assistent sagt dann Bescheid und holt nach.

## Einmal: Box

1. Bei Hetzner eine CPX32 mit Ubuntu 24.04 und deinem SSH-Key bestellen, Backup an
2. Kit hochladen und einrichten:
   ```
   rsync -a --exclude .git ~/Projects/pearl-kit/ root@<box>:/root/pearl-kit/
   ssh root@<box> bash /root/pearl-kit/box/einrichten.sh
   ```
3. Wache: in `/etc/pearl-kit/box.env` `WACHE_TG_TOKEN` (Token eines Bots, über den du Meldungen willst) und `WACHE_CHAT_ID` (deine Telegram-ID) eintragen. Test: `python3 /opt/pearl-kit/box/wache.py --zeigen`
4. Domain für die Einrichtungsseite: einen A-Record (und AAAA) einer Subdomain, etwa `box.example.de`, auf die Box zeigen lassen, in `/etc/pearl-kit/box.env` `PFORTE_DOMAIN=box.example.de` eintragen und `einrichten.sh` noch einmal starten. Es schreibt das Caddyfile und öffnet 80 und 443. Test: `https://box.example.de` zeigt eine leere Seite mit Status 404 und gültigem Zertifikat

Ohne Domain läuft alles wie vorher: `person-anlegen.sh` meldet die KI im Terminal an, und Google endet bei einer Seite mit „localhost“, deren Adresse die Person in den Chat schickt.

## Einmal: Google-Cloud-Projekt (nur für Google)

Ein Projekt für alle Personen, unter deinem Google-Konto.

1. [console.cloud.google.com](https://console.cloud.google.com) → neues Projekt „Assistent“
2. APIs aktivieren: Gmail, Google Calendar, Google Drive, Google Tasks
3. OAuth-Zustimmungsbildschirm: Typ „Extern“, App-Name frei, deine Mail als Kontakt. Unter „Autorisierte Domains“ die Hauptdomain der Box eintragen (`example.de`). Bereiche nicht von Hand eintragen, workspace-mcp fragt sie an
4. Veröffentlichungsstatus auf **„In Produktion“** setzen. Im Testmodus laufen Anmeldungen nach sieben Tagen ab. Ungeprüft geht das bis 100 Konten, die Person sieht bei der Anmeldung eine Warnung „Google hat diese App nicht überprüft“ und tippt auf „Erweitert“ und „Weiter“
5. Anmeldedaten → OAuth-Client-ID → Typ **„Webanwendung“**, autorisierte Weiterleitungs-URIs: je Person `https://box.example.de/<kurz>/oauth2callback`. `person-anlegen.sh` nennt die genaue Adresse, trag sie ein, bevor die Person `/google` schickt. Ohne Domain: `http://localhost:8101/oauth2callback` bis `http://localhost:8110/oauth2callback`
6. JSON herunterladen und auf die Box:
   ```
   scp client_secret_*.json root@<box>:/etc/pearl-kit/google-client.json
   ssh root@<box> 'chown root:assistenten /etc/pearl-kit/google-client.json; chmod 640 /etc/pearl-kit/google-client.json'
   ```

## Je Person

**Bot anlegen:** Bei `@BotFather` `/newbot`, Name und Benutzername frei (endet auf `bot`), den Token kopieren. Der Bot gehört damit deinem Telegram-Konto, den Namen stellt der Assistent später selbst auf den, den die Person wählt.

**Anlegen**, auf der Box als root:

```
bash /opt/pearl-kit/box/person-anlegen.sh anna
```

Das Skript legt den User `a-anna` an, installiert Claude Code, fragt nach dem Bot-Token (und einmalig nach deinem Namen als Betreuer), legt den Pfad `/anna/` der Einrichtungsseite an und startet den Dienst. Liegt `/etc/pearl-kit/google-client.json` schon da, bekommt die Person einen Google-Port und nennt dir das Skript die Redirect-URI für das Cloud-Projekt. Zum Schluss läuft der Selbsttest, dann stehen Einladungslink und QR-Code im Terminal.

**Die Person** tippt den Link an (oder scannt den Code) und dann auf „Starten“. Der Bot verbindet sich mit ihrem Telegram-Konto, übernimmt ihren Vornamen und zeigt den Knopf „Einrichten“. Die Seite öffnet sich in Telegram und fragt nacheinander:

| Schritt | Was die Person tut |
|---|---|
| Namen | Ihr Vorname steht schon da, sie gibt dem Assistenten einen Namen |
| KI | Claude oder Gemini: anmelden, den Code zurück auf die Seite einfügen. ChatGPT: den angezeigten Code bei OpenAI eintippen, die Seite merkt es von selbst. Der Claude-Token hält ein Jahr, die Wache erinnert ab Tag 330 |
| Kalender und Mail | Zwei Schalter, Google-Adresse, eine Anmeldung bei Google. Oder „Ohne Google weiter“ |

Danach legt der Dienst ihren Bereich an und der Assistent stellt sich vor. Bleibt die Seite nach der KI-Anmeldung liegen, schließt er nach 30 Minuten ohne Google ab. Der Einladungslink gilt, bis sich das erste Telegram-Konto damit meldet, danach antwortet der Bot nur noch ihr. Neu ausgeben: `runuser -u a-anna -- env HOME=/home/a-anna python3 /opt/pearl-kit/assistent.py einladung`. Für die Person: [docs/fuer-die-person.md](docs/fuer-die-person.md).

**Ohne Domain** fragt das Skript Vorname, Telegram-ID (die Person schreibt `@userinfobot` an), Namen des Assistenten, Google-Konto und KI ab und meldet die KI im Terminal an: Claude mit `claude setup-token` (Link an die Person, Code zurück, Token einfügen), ChatGPT mit `codex login --device-auth` (Link und Code an die Person), Gemini im Programm selbst (Link an die Person, Code einfügen, dann `/quit`).

## Betrieb

| Was | Wie |
|---|---|
| Läuft alles? | `python3 /opt/pearl-kit/box/wache.py --zeigen` |
| Log einer Person | `journalctl _UID=$(id -u a-anna) --user-unit assistent -n 50` |
| Selbsttest | `runuser -u a-anna -- env HOME=/home/a-anna python3 /opt/pearl-kit/assistent.py pruefen` |
| Neu starten | `systemctl --user -M a-anna@ restart assistent` |
| Einstellungen | Der Menüknopf „Einstellungen“ neben dem Eingabefeld öffnet `<web>/einstellungen` als Web App. Die Seite weist sich mit Telegrams signiertem `initData` aus (nur das Konto der Person), dann gibt es KI, Kalender und Mail, Ablage (Spiegel per rclone nach Google Drive, Dropbox oder OneDrive, `ablage.py`) und Meine Daten (ZIP in den Chat, auf Wunsch am Ersten jedes Monats). Dropbox und OneDrive erscheinen erst, wenn in `ablage.py` die öffentliche App-Kennung steht |
| KI neu anmelden oder wechseln | Die Person schickt `/ki`: Sie wechselt per Antippen zwischen allen verbundenen KIs (`/ki_claude`, `/ki_chatgpt`) oder verbindet eine weitere, `/anmelden` öffnet die Einrichtung, für Kalender und Mail `/google`. Lehnt die KI die Anmeldung ab, bekommt sie den Link von selbst. Beim Wechsel sichert erst die bisherige KI das offene Gespräch, dann beginnt ein neues, das Gedächtnis bleibt |
| Ohne Domain neu anmelden | Claude: `sudo -u a-anna -i claude setup-token`, Token in `secrets.env` als `CLAUDE_CODE_OAUTH_TOKEN` ersetzen, `CLAUDE_TOKEN_SEIT` auf heute. ChatGPT: `auth.json` in `~/.local/share/assistent/codex/` löschen und `person-anlegen.sh anna` noch einmal. Gemini ebenso mit `gemini/.gemini/oauth_creds.json` |
| Modell oder Zeiten ändern | Modell, Effort, Rechte: `[ki]` in `config.toml` der Person (leer heißt Standard des Anbieters), dann Neustart. Zeiten: Die Person sagt es dem Assistenten, er ändert seine `jobs/` selbst |
| Neue Fassung des Kits | rsync nach `/root/pearl-kit/`, dann `bash /root/pearl-kit/box/aktualisieren.sh`. Tests laufen vorher, das Gelernte bleibt |
| Person hört auf | `bash /opt/pearl-kit/box/person-entfernen.sh anna`: Export nach `/root/pearl-kit-export/`, dann löschen |

**Rechte:** Claude läuft mit `--permission-mode auto`. Lehnt das Abo der Person den Modus ab (Selbsttest scheitert am Claude-Lauf), in `config.toml` `rechte = "dontAsk"` setzen: Dann darf der Assistent nur, was in `settings.json` freigegeben ist, und das reicht für alles im Kit. ChatGPT läuft in der Sandbox von Codex, schreiben darf es nur im Arbeitsordner, ins Netz darf es. Gemini läuft ohne Sandbox mit `--approval-mode yolo`, begrenzt nur durch den Linux-User.

## WhatsApp (Erweiterung)

Nur auf dem eigenen Server: Der Assistent bietet es einmal von selbst an (oder die Person schickt `/whatsapp`), eine Seite in Telegram holt die Einwilligung und zeigt den QR-Code, ab dann pflegt der Assistent nachts Notizen zu den Menschen im engen Kreis und schlägt morgens vor, wem sie schreiben könnte, mit einem fertigen Entwurf je Mensch. Aufbau und Hinweise für die Person: `docs/whatsapp.md`.

## Stolperfallen

- **Geheimnisse:** Für Claude sperren deny-Regeln `secrets.env` und `config.toml`. Codex und Gemini kennen solche Regeln nicht, sie könnten die Schlüssel der Person lesen. Die bleiben trotzdem in ihrem User, an fremde Instanzen kommen sie nicht
- **Codex-Sandbox:** Scheitert der ChatGPT-Lauf im Selbsttest an der Sandbox (Fehler mit `landlock`, `bwrap` oder `sandbox`), in `config.toml` `rechte = "voll"` setzen
- **ChatGPT-Anmeldung per Code:** Lehnt OpenAI den Gerätecode ab, muss die Person die Geräte-Anmeldung in ChatGPT unter Einstellungen, Sicherheit einschalten. OpenAI warnt auf der Seite davor, Codes von anderen einzugeben. Das gilt Fremden, hier kommt der Code von der eigenen Einrichtungsseite
- **Gemini mit Workspace-Konto:** Firmen- und Schulkonten brauchen ein eigenes Google-Cloud-Projekt. Für Gemini deshalb ein privates Google-Konto nehmen, Google für Kalender und Mail darf trotzdem das Firmenkonto sein
- **Erster Gemini-Lauf:** Der Kit gibt Gemini den Prompt über stdin. Lokal ist das nur bis zur Anmeldung geprüft, den ersten echten Lauf im Selbsttest anschauen
- **Gemini-Kontingent:** Ohne Abo gilt ein Tageskontingent. Ist es aufgebraucht, weicht der Assistent auf Flash aus und sagt sonst Bescheid

**Sicherung:** Jede Nacht ein Git-Stand der Instanz, lokal auf der Box, dazu das Hetzner-Backup der ganzen Box. Wer einen zweiten Ort will, trägt in der Instanz ein Remote `origin` ein (`git -C ~/assistent remote add origin …`), dann pusht die Nacht dorthin.

## Was du siehst und was nicht

Als root kannst du technisch alles auf der Box lesen. Abgemacht ist: Du liest keine Inhalte. Die Wache meldet nur Zustände („erreicht Telegram nicht“, „Token bald alt“), keine Nachrichten. So steht es auch in der Absprache.

## Entwickeln

```
python3 -m unittest discover -s tests -v       ohne Netz, mit falschem claude, codex, gemini und Telegram
python3 tests/lauf.py                          Testfragen gegen eine echte Instanz (als User der Person)
```

Ein Test stellt sicher, dass nichts Persönliches aus der Vorlage im Kit landet. Neue Zeitpläne für alle kommen nach `vorlage/home/jobs/` und erreichen bestehende Instanzen nicht von selbst (die Vorlage überschreibt nie, was schon da ist).
