#!/usr/bin/env python3
"""Falsches `codex` für die Tests, Ereignisse wie `codex exec --json` (Codex 0.162).
FAKE_MODUS: ok, still, limit, auth, weg (Gespräch nicht mehr da). FAKE_LOG wie bei fake_claude.py.
`codex login --device-auth` spielt die Anmeldung per Gerätecode und legt danach auth.json an."""
import json
import os
import sys
import time
import uuid

home = os.environ.get("CODEX_HOME") or ""
if sys.argv[1:3] == ["login", "--device-auth"]:
    print("Welcome to Codex [v0.0.0]\n\nFollow these steps to sign in with ChatGPT using device code authorization:\n")
    print("1. Open this link in your browser and sign in to your account\n   https://auth.openai.com/codex/device\n")
    print("2. Enter this one-time code (expires in 15 minutes)\n   T3ST-AB12C\n", flush=True)
    time.sleep(float(os.environ.get("FAKE_WARTEN", "1")))
    with open(os.path.join(home, "auth.json"), "w") as f:
        json.dump({"tokens": {"access_token": "test"}}, f)
    print("Successfully logged in", flush=True)
    sys.exit(0)

args = sys.argv[1:]
prompt = sys.stdin.read()
modus = os.environ.get("FAKE_MODUS", "ok")
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd(), "home": home,
                            "google": bool(os.environ.get("GOOGLE_MCP_TOKEN"))}) + "\n")
if modus == "weg" and "resume" in args:
    print(json.dumps({"type": "error", "message": "thread/resume failed: no rollout found for thread id x"}))
    sys.exit(1)
tid = args[args.index("resume") + 1] if "resume" in args else str(uuid.uuid4())


def zeile(d):
    print(json.dumps(d), flush=True)


zeile({"type": "thread.started", "thread_id": tid})
zeile({"type": "turn.started"})
if modus == "limit":
    zeile({"type": "error", "message": "You've hit your usage limit. Try again in 2 hours 13 minutes."})
    zeile({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again in 2 hours 13 minutes."}})
    sys.exit(1)
if modus == "auth":
    zeile({"type": "error", "message": "unexpected status 401 Unauthorized: Your authentication token has expired. "
                                       "Please try signing in again."})
    sys.exit(1)
zeile({"type": "item.started", "item": {"id": "item_0", "type": "mcp_tool_call", "server": "google",
                                        "tool": "get_events", "status": "in_progress"}})
zeile({"type": "item.completed", "item": {"id": "item_0", "type": "mcp_tool_call", "server": "google",
                                          "tool": "get_events", "status": "completed"}})
zeile({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "Ich schaue nach."}})
text = "STILL" if modus == "still" else "OK, verstanden: " + prompt.strip().splitlines()[-1][-80:]
zeile({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": text}})
zeile({"type": "turn.completed", "usage": {"input_tokens": 2400, "cached_input_tokens": 0, "output_tokens": 40}})
