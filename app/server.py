"""Solvent: an MCP server whose write tools are gated by human approval."""

import hmac
import logging
import os
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import azure_clients as az
from app import entra, faults, policy
from app.policy import audit_event, governed

if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(logger_name="solvent")
logging.basicConfig(level=logging.INFO)
logging.getLogger("azure").setLevel(logging.WARNING)  # SDK request/response chatter drowns the audit log

mcp = MCPServer(
    "solvent",
    instructions=(
        "Most read tools are free; a gated classification (see get_cost_summary) or any write "
        "needs an approval_token minted by a human; a denial of approval_required or "
        "classification_gated means: ask the operator for one."
    ),
)


@mcp.tool()
@governed
def get_cost_summary(days: int = 7, approval_token: str = "") -> dict[str, Any]:
    """Actual cost by resource group over the last N days. Read-only, but FINANCIAL:
    requires a human-minted approval_token."""
    return az.cost_summary(days)


@mcp.tool()
@governed
def get_resource_health(resource_group: str) -> dict[str, Any]:
    """Azure Resource Health availability state for every resource in a group. Read-only."""
    return az.resource_health(resource_group)


@mcp.tool()
@governed
def restart_container_app(name: str, approval_token: str = "") -> dict[str, Any]:
    """Restart a Container App's latest revision. WRITE: requires a human-minted approval_token."""
    return az.restart_container_app(name)


@mcp.custom_route("/", methods=["GET"])
async def index(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "service": "solvent",
            "description": (
                "Governed MCP server: reads run freely unless gated by classification, "
                "writes always need a human-minted approval token."
            ),
            "endpoints": {"mcp": "/mcp (POST, streamable HTTP)", "health": "/healthz"},
            "source": "https://github.com/parthkapur/solvent",
        }
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


API_KEY = os.environ.get("API_KEY", "")


def _authenticate(offered: bytes) -> tuple[str | None, str]:
    """(caller, reason) for this request. caller is None to deny; reason then says why.

    A verified JWT names a person; the shared key cannot, so it is audited as `shared_key`
    and the weaker path stays visible in the record instead of looking like the strong one.
    Nothing configured leaves the local server open, as it always has.
    """
    if not API_KEY and not entra.configured():
        return "local_no_auth", ""  # nothing configured: the local server stays open
    if API_KEY and hmac.compare_digest(offered, b"Bearer " + API_KEY.encode()):
        return "shared_key", ""
    if entra.configured() and offered.startswith(b"Bearer "):
        claims = entra.verified_claims(offered[7:].decode("ascii", "ignore"))
        if claims:
            return entra.caller_name(claims) or "verified_no_identity", ""
    if not offered:
        return None, "unauthenticated"
    return None, "caller_token_invalid"


def require_key(asgi):
    """Caller guard over /mcp. / and /healthz stay open for probes and the smoke test."""

    async def guard(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            # Stay in bytes: compare_digest refuses non-ASCII str, and .decode() would
            # raise on a header that is not valid UTF-8. The caller picks both.
            offered = dict(scope["headers"]).get(b"authorization", b"")
            # _authenticate can hit the tenant's JWKS endpoint over plain urllib, which blocks
            # the event loop for up to its timeout - at max_replicas = 1 that stalls /healthz
            # too, and Container Apps restarts the app on the failed liveness probe. Off the
            # loop, same as MCP already runs sync tool functions in a worker thread.
            caller, reason = await anyio.to_thread.run_sync(_authenticate, offered)
            if caller is None:
                audit_event(tool="mcp", decision="denied", reason=reason)
                await JSONResponse({"denied": True, "reason": reason}, status_code=401)(
                    scope, receive, send
                )
                return
            policy.CALLER.set(caller)
            faults.FAULT_MS.set(faults.from_header(dict(scope["headers"]).get(faults.HEADER, b"")))
        await asgi(scope, receive, send)

    return guard


# Public service behind Container Apps ingress; DNS-rebinding protection is for localhost servers.
app = require_key(
    mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
)
