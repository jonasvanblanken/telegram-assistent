#!/usr/bin/env python3
"""Startet workspace-mcp mit Bearer-Token-Prüfung.

workspace-mcp kennt im HTTP-Modus ohne OAuth 2.1 keine Anmeldung: jeder lokale Prozess auf der Box könnte
Mails, Kalender und Drive der Person lesen. Dieser Starter hängt vor die App des Servers eine kleine
ASGI-Prüfung, im selben Prozess, ohne neue Abhängigkeit und ohne zusätzlichen Port. Jede HTTP-Anfrage
braucht "Authorization: Bearer <GOOGLE_MCP_TOKEN>", sonst 401. Das gilt auch für /oauth2callback: den
Rückruf reicht assistent.py mit Header weiter.

Zweite Schicht (OhneGaeste): Aufrufe mit Gästen (arguments.attendees) lehnt der Dienst ab. Google schickt Gästen
sonst eine Einladung mit Titel und Beschreibung im Namen der Person, und eine eingeschleuste Anweisung könnte so
Inhalte nach draußen tragen. workspace-mcp kennt dafür keinen Schalter, nur ganze Werkzeuge.

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


GAESTE_FEHLER = ("Gäste einladen geht hier nicht, Google würde ihnen im Namen der Person schreiben. Leg den Termin "
                 "ohne Gäste an, die Gäste trägt die Person in Google Kalender selbst ein.")


def mit_gaesten(body):
    """JSON-RPC-Nachricht(en) mit tools/call und nicht leeren attendees: Liste der ids, sonst []."""
    try:
        d = json.loads(body or b"null")
    except ValueError:
        return []
    out = []
    for n in d if isinstance(d, list) else [d]:
        if not isinstance(n, dict) or n.get("method") != "tools/call":
            continue
        args = (n.get("params") or {}).get("arguments") or {}
        if isinstance(args, dict) and args.get("attendees"):
            out.append(n.get("id"))
    return out


class OhneGaeste:
    """ASGI: liest den Körper jeder POST-Anfrage, lehnt tools/call mit Gästen ab und reicht den Rest unverändert weiter."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        teile, mehr = [], True
        while mehr:
            m = await receive()
            if m.get("type") != "http.request":
                return
            teile.append(m.get("body", b""))
            mehr = m.get("more_body", False)
        body = b"".join(teile)
        ids = mit_gaesten(body)
        if ids:
            antwort = [{"jsonrpc": "2.0", "id": i, "error": {"code": -32602, "message": GAESTE_FEHLER}} for i in ids]
            daten = json.dumps(antwort[0] if len(antwort) == 1 else antwort, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(daten)).encode())]})
            await send({"type": "http.response.body", "body": daten})
            return
        geliefert = False

        async def nochmal():
            nonlocal geliefert
            if not geliefert:
                geliefert = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await self.app(scope, nochmal, send)


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
        app.user_middleware.insert(0, Middleware(OhneGaeste))
        app.user_middleware.insert(0, Middleware(BearerAuth, token=tok))  # aeusserste Schicht
        app.middleware_stack = app.build_middleware_stack()
        return app

    cs.SecureFastMCP.http_app = http_app
    from main import main as workspace_main
    sys.argv[0] = "workspace-mcp"
    sys.exit(workspace_main())


if __name__ == "__main__":
    main()
