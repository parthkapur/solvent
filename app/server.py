"""Solvent: an MCP server whose write tools are gated by human approval."""

import hmac
import logging
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import azure_clients as az
from app.policy import audit_event, governed

if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(logger_name="solvent")
logging.basicConfig(level=logging.INFO)
logging.getLogger("azure").setLevel(logging.WARNING)  # SDK request/response chatter drowns the audit log

mcp = MCPServer(
    "solvent",
    instructions=(
        "Read tools are free. restart_container_app needs an approval_token minted by a human; "
        "if you get {denied: true, reason: approval_required}, ask the operator for one."
    ),
)


@mcp.tool()
@governed
def get_cost_summary(days: int = 7) -> dict[str, Any]:
    """Actual cost by resource group over the last N days. Read-only."""
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
            "description": "Governed MCP server: read tools run freely, write tools need a human-minted approval token.",
            "endpoints": {"mcp": "/mcp (POST, streamable HTTP)", "health": "/healthz"},
            "source": "https://github.com/parthkapur/solvent",
        }
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


API_KEY = os.environ.get("API_KEY", "")


def require_key(asgi):
    """Bearer-key guard over /mcp. / and /healthz stay open for probes and the smoke test.

    ponytail: one shared key, so the audit log names the approver but not the caller.
    Upgrade path is an Entra-issued JWT verified against the tenant JWKS.
    """

    async def guard(scope, receive, send):
        if scope["type"] == "http" and API_KEY and scope["path"].startswith("/mcp"):
            # Stay in bytes: compare_digest refuses non-ASCII str, and .decode() would
            # raise on a header that is not valid UTF-8. The caller picks both.
            offered = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(offered, b"Bearer " + API_KEY.encode()):
                audit_event(tool="mcp", decision="denied", reason="unauthenticated")
                await JSONResponse({"denied": True, "reason": "unauthenticated"}, status_code=401)(
                    scope, receive, send
                )
                return
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
