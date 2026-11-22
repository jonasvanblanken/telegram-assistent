#!/usr/bin/env python3
"""Startet workspace-mcp mit Bearer-Token-Prüfung.

workspace-mcp kennt im HTTP-Modus ohne OAuth 2.1 keine Anmeldung: jeder lokale Prozess auf der Box könnte
Mails, Kalender und Drive der Person lesen. Dieser Starter hängt vor die App des Servers eine kleine
ASGI-Prüfung, im selben Prozess, ohne neue Abhängigkeit und ohne zusätzlichen Port. Jede HTTP-Anfrage
braucht "Authorization: Bearer <GOOGLE_MCP_TOKEN>", sonst 401. Das gilt auch für /oauth2callback: den
Rückruf reicht assistent.py mit Header weiter.

Aufruf aus google-mcp.sh mit dem Python der venv von workspace-mcp:
  python3 google-mcp-auth.py <argumente für workspace-mcp>
Token: GOOGLE_MCP_TOKEN in ~/.config/assistent/secrets.env (600). Fehlt er, startet der Dienst nicht.
"""
import hmac
import json
import os
import re
import shlex
import sys

TOKENS = os.environ.get("ASSISTENT_SECRETS") or os.path.expanduser("~/.config/assistent/secrets.env")


def token():
    try:
        for line in open(TOKENS, encoding="utf-8"):
            m = re.match(r"^(?:export\s+)?GOOGLE_MCP_TOKEN=(.*)$", line.strip())
            if m:
                v = shlex.split(m.group(1))
                return v[0] if v else ""
    except OSError:
        pass
    return ""


class BearerAuth:
    """Reine ASGI-Middleware: HTTP nur mit passendem Bearer-Token, Lifespan und Rest laufen durch."""

    def __init__(self, app, token):
        self.app = app
        self.expected = ("Bearer " + token).encode()

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            got = b""
            for k, v in scope.get("headers") or []:
                if k == b"authorization":
                    got = v
                    break
            if not hmac.compare_digest(got, self.expected):
                body = json.dumps({"error": "unauthorized"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def main():
    tok = token()
    if len(tok) < 24:
        print(f"google-mcp-auth: kein GOOGLE_MCP_TOKEN in {TOKENS}, starte nicht", file=sys.stderr)
        sys.exit(2)
    from starlette.middleware import Middleware  # kommt mit workspace-mcp (fastmcp), keine neue Abhaengigkeit
    import core.server as cs

    original = cs.SecureFastMCP.http_app

    def http_app(self, *args, **kwargs):
        app = original(self, *args, **kwargs)
        app.user_middleware.insert(0, Middleware(BearerAuth, token=tok))  # aeusserste Schicht
        app.middleware_stack = app.build_middleware_stack()
        return app

    cs.SecureFastMCP.http_app = http_app
    from main import main as workspace_main
    sys.argv[0] = "workspace-mcp"
    sys.exit(workspace_main())


if __name__ == "__main__":
    main()
