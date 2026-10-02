"""The page server: SSE for events, POST for controls. Localhost only."""

import json
import secrets
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

STATIC = Path(__file__).with_name("static") / "index.html"
EVENTS_MARKER = "/*__EVENTS__*/null"
# A DNS-rebinding page reaches 127.0.0.1 under its own hostname; refusing any other Host header
# is what keeps another browser tab from reading the control token.
_HOSTS = [Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])]


def load_html() -> str:
    return STATIC.read_text(encoding="utf-8")


def inline_events(html: str, events: list[dict]) -> str:
    payload = json.dumps(events, separators=(",", ":")).replace("</", "<\\/")
    return html.replace(EVENTS_MARKER, payload)


def make_app(bus, engine, html: str, token: str) -> Starlette:
    async def index(_: Request):
        return HTMLResponse(html.replace("__TOKEN__", token))

    async def events(_: Request):
        q = bus.subscribe()

        async def stream():
            try:
                while (ev := await q.get()) is not None:
                    yield f"data: {json.dumps(ev, separators=(',', ':'))}\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )

    async def control(request: Request):
        offered = request.headers.get("x-demo-token", "")
        if not secrets.compare_digest(offered.encode(), token.encode()):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        try:
            body = await request.json()
            action = str(body["action"])
            args = {k: v for k, v in body.items() if k != "action"}
            return JSONResponse(engine.handle(action, **args))
        except (KeyError, TypeError, ValueError, OverflowError):
            return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/events", events),
            Route("/control", control, methods=["POST"]),
        ],
        middleware=_HOSTS,
    )


async def serve(app: Starlette, port: int) -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    await uvicorn.Server(config).serve()


async def serve_static(html: str, port: int) -> None:
    """Replay mode: the page carries its own events, so there is nothing else to serve."""

    async def index(_: Request):
        return HTMLResponse(html)

    await serve(Starlette(routes=[Route("/", index)], middleware=_HOSTS), port)
