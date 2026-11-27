#!/usr/bin/env python3
"""Falsches `claude` für die Tests: liest den Prompt, schreibt stream-json wie Claude Code.
FAKE_MODUS: ok (Standard), still, limit, auth. FAKE_LOG: jede Zeile ein JSON mit Argumenten und Umgebung.
`claude setup-token` spielt die Anmeldung: Link, Frage nach dem Code, bei „gut#zustand“ ein Token."""
import json
import os
import sys
import uuid

if sys.argv[1:2] == ["setup-token"]:
    # Wie Claude Code (Ink) im Terminal: roh lesen, Enter zählt nur als eigenes Paket. Kommt es im selben Paket
    # wie der Code, ist es Teil des Eingefügten und nichts wird abgeschickt. Bei falschem Code wartet es auf Enter.
    import termios
    import tty

    def zeile(text=""):
        sys.stdout.write(text + "\r\n")
        sys.stdout.flush()

    fd = sys.stdin.fileno()
    tty.setraw(fd)
    zeile("Opening browser to sign in…")
    zeile("Browser didn't open? Use the url below to sign in (c to copy)")
    zeile()
    zeile("https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code&state=zustand")
    zeile()
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    eingabe = ""
    while True:
        paket = os.read(fd, 4096).decode("utf-8", "replace")
        if not paket:
            sys.exit(1)
        if paket in ("\r", "\n"):
            break
        eingabe += paket.replace("\r", "\n")
        sys.stdout.write("*" * len(paket))
        sys.stdout.flush()
    if eingabe.strip() in ("gut#zustand", "gut"):
        zeile()
        zeile("Long-lived authentication token created successfully!")
        zeile()
        zeile("Your OAuth token (valid for 1 year):")
        zeile()
        zeile("sk-ant-" + "oat01-" + "t" * 40)
        sys.exit(0)
    zeile()
    sys.stdout.write("OAuth error: Request failed with status code 400Press Enter to retry. ")
    sys.stdout.flush()
    while os.read(fd, 4096):   # wie das echte Programm: bleibt offen, bis jemand Enter drückt oder es beendet
        pass
    sys.exit(1)

prompt = sys.stdin.read()
modus = os.environ.get("FAKE_MODUS", "ok")
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd(),
                            "oauth": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
                            "google": bool(os.environ.get("GOOGLE_MCP_TOKEN"))}) + "\n")
sid = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else str(uuid.uuid4())
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
print(json.dumps({"type": "assistant", "session_id": sid, "message": {
    "content": [{"type": "text", "text": "…"}], "usage": {"input_tokens": 1200, "output_tokens": 30}}}))
if modus == "limit":
    res = {"type": "result", "subtype": "success", "is_error": True, "session_id": sid,
           "result": "You've hit your limit · resets 11pm (Europe/Berlin)"}
elif modus == "auth":
    res = {"type": "result", "subtype": "success", "is_error": True, "session_id": sid,
           "result": "Failed to authenticate. API Error: 401 OAuth token has expired"}
else:
    text = "STILL" if modus == "still" else "OK, verstanden: " + prompt.strip().splitlines()[-1][-80:]
    res = {"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": text}
print(json.dumps(res), flush=True)
