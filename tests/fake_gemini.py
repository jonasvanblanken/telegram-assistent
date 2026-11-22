#!/usr/bin/env python3
"""Falsches `gemini` für die Tests, Ereignisse wie `gemini -o stream-json` (Gemini CLI 0.70).
FAKE_MODUS: ok, still, limit, auth. FAKE_LOG wie bei fake_claude.py.
Ohne -o spielt es die interaktive Anmeldung mit NO_BROWSER: Link, Frage nach dem Code, bei „gut“ legt es
oauth_creds.json an und bleibt offen wie das echte Programm."""
import json
import os
import sys
import time
import uuid

home = os.path.join(os.environ.get("GEMINI_CLI_HOME") or "", ".gemini")
args = sys.argv[1:]
if "-o" not in args:
    print("Code Assist login required.\nAttempting to open authentication page in your browser.\n"
          "Otherwise navigate to:\n\nhttps://accounts.google.com/o/oauth2/v2/auth?redirect_uri=https%3A%2F%2F"
          "codeassist.google.com%2Fauthcode&access_type=offline&client_id=test&state=abc\n\n", flush=True)
    code = input("Enter the authorization code: ").strip()
    if code != "gut":
        print("Failed to authenticate with authorization code", flush=True)
        sys.exit(1)
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "oauth_creds.json"), "w") as f:
        json.dump({"refresh_token": "test"}, f)
    print("Authentication succeeded", flush=True)
    while True:
        time.sleep(1)

prompt = sys.stdin.read()
modus = os.environ.get("FAKE_MODUS", "ok")
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd(), "home": home,
                            "google": bool(os.environ.get("GOOGLE_MCP_TOKEN"))}) + "\n")
sid = (args[args.index("--resume") + 1] if "--resume" in args else
       args[args.index("--session-id") + 1] if "--session-id" in args else str(uuid.uuid4()))


def zeile(d):
    print(json.dumps(d), flush=True)


zeile({"type": "init", "session_id": sid, "model": "gemini-test"})
zeile({"type": "message", "role": "user", "content": prompt})
if modus in ("limit", "auth"):
    meldung = ("You have exhausted your capacity on this model. Your quota will reset after 13h21m5s."
               if modus == "limit" else "Request had invalid authentication credentials (401). Please login again.")
    zeile({"type": "error", "severity": "error", "message": meldung})
    zeile({"type": "result", "status": "error", "error": {"type": "Error", "message": meldung},
           "stats": {"input_tokens": 10, "tool_calls": 0}})
    sys.exit(1)
zeile({"type": "message", "role": "assistant", "content": "Ich schaue ", "delta": True})
zeile({"type": "message", "role": "assistant", "content": "nach.", "delta": True})
zeile({"type": "tool_use", "tool_name": "google_web_search", "tool_id": "t1", "parameters": {"query": "x"}})
zeile({"type": "tool_result", "tool_id": "t1", "status": "success", "output": "…"})
text = "STILL" if modus == "still" else "OK, verstanden: " + prompt.strip().splitlines()[-1][-80:]
zeile({"type": "message", "role": "assistant", "content": text, "delta": True})
zeile({"type": "result", "status": "success", "stats": {"input_tokens": 5000, "output_tokens": 50, "tool_calls": 1}})
