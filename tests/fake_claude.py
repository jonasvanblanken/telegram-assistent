#!/usr/bin/env python3
"""Falsches `claude` für die Tests: liest den Prompt, schreibt stream-json wie Claude Code.
FAKE_MODUS: ok (Standard), still, limit, auth. FAKE_LOG: jede Zeile ein JSON mit Argumenten und Umgebung.
`claude setup-token` spielt die Anmeldung: Link, Frage nach dem Code, bei „gut#zustand“ ein Token."""
import json
import os
import sys
import uuid

if sys.argv[1:2] == ["setup-token"]:
    print("Opening browser to sign in…")
    print("Browser didn't open? Use the url below to sign in (c to copy)\n")
    print("https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code&state=zustand\n", flush=True)
    code = input("Paste code here if prompted > ").strip()
    if code in ("gut#zustand", "gut"):
        print("\nLong-lived authentication token created successfully!\n")
        print("Your OAuth token (valid for 1 year):\n\n" + "sk-ant-" + "oat01-" + "t" * 40 + "\n", flush=True)
        sys.exit(0)
    print("OAuth error: Invalid code", flush=True)
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
