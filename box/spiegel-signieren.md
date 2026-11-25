# Spiegel signieren

Eigene Server spielen nur Commits ein, die der Release-Schlüssel signiert hat (`box/selbst-aktualisieren.sh`). Jeder Commit, der auf `main` des öffentlichen Spiegels landet, braucht deshalb eine SSH-Signatur. Geprüft wird nur der oberste Commit, den der Server holt.

## Schlüssel

- Privater Schlüssel: `~/.ssh/telegram-assistent-signatur`, nur auf dem Rechner, der den Spiegel schreibt. Nie ins Repo, nie in einen Chat
- Öffentlicher Schlüssel: steht in `box/release-signers` (Principal `telegram-assistent`, Namespace `git`)
- Auf dem Server gilt die installierte Kopie `/etc/pearl-kit/release-signers`, nie die Datei im geholten Stand

## Commit signieren

```bash
git -c gpg.format=ssh -c user.signingkey=~/.ssh/telegram-assistent-signatur commit -S -m "release: …"
```

Ein fertiger Commit, der noch keine Signatur hat (zum Beispiel ein Squash für den Spiegel):

```bash
git -c gpg.format=ssh -c user.signingkey=~/.ssh/telegram-assistent-signatur commit --amend --no-edit -S
```

Vor dem Push prüfen, so wie der Server es tut:

```bash
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=box/release-signers verify-commit HEAD
```

Erst wenn dort `Good "git" signature for telegram-assistent` steht, pushen.

## Schlüssel wechseln

1. Neues Paar erzeugen: `ssh-keygen -t ed25519 -f ~/.ssh/telegram-assistent-signatur-neu -C "telegram-assistent releases"`
2. Die neue Zeile in `box/release-signers` eintragen. Die alte Zeile bleibt für diesen einen Commit drin oder fliegt raus, beides geht
3. Diesen Commit mit dem **alten** Schlüssel signieren und pushen. Die Server prüfen ihn gegen den alten Schlüssel, spielen ihn ein und übernehmen danach die neue `box/release-signers` nach `/etc/pearl-kit/release-signers`
4. Warten, bis die Server die Fassung haben (stündlicher Timer). Ab dann mit dem neuen Schlüssel signieren und die alte Zeile entfernen, falls sie noch drin ist

Ist der alte Schlüssel verloren, kommt ein neuer nur von Hand auf die Server (`/etc/pearl-kit/release-signers` als root ersetzen) oder über einen neu angelegten Server.

## Was ein Server tut, wenn die Signatur fehlt

Er spielt nichts ein, schreibt `<sha> 3 signatur` nach `/var/lib/pearl-kit/aktualisieren-gescheitert` und versucht diese Fassung nicht noch einmal. Die Wache meldet es der Person über ihren Bot. Der nächste signierte Commit läuft normal.
