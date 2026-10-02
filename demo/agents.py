"""The simulated callers. Each call returns what the caller saw, so lane 1 needs no server help."""

import time
from dataclasses import dataclass

import httpx

ACCEPT = "application/json, text/event-stream"


@dataclass(frozen=True)
class Seen:
    outcome: str  # allowed | denied | error
    reason: str
    status: int
    rtt_ms: float


def classify(status: int, body: object) -> tuple[str, str]:
    """(outcome, reason) from an HTTP status and a decoded JSON-RPC or guard body."""
    if not isinstance(body, dict):
        return "error", f"http_{status}"
    if body.get("denied"):  # the 401 body the guard writes
        return "denied", str(body.get("reason", "unknown"))
    if "error" in body:
        return "error", "rpc_error"
    result = body.get("result")
    if not isinstance(result, dict):
        return "error", "no_result"
    data = result.get("structuredContent")
    if isinstance(data, dict):
        if data.get("denied"):
            return "denied", str(data.get("reason", "unknown"))
        if "error" in data:
            return "error", "tool_error"
    if result.get("isError"):
        return "error", "tool_error"
    return "allowed", "ok"


class Caller:
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        self.client = client
        self.api_key = api_key

    async def call(
        self, tool: str, arguments: dict, *, auth: bool = True, fault_ms: int = 0
    ) -> Seen:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        headers = {"accept": ACCEPT}
        if auth:
            headers["authorization"] = f"Bearer {self.api_key}"
        if fault_ms:
            headers["x-solvent-fault"] = f"latency_ms={fault_ms}"
        start = time.perf_counter()
        try:
            r = await self.client.post("/mcp", json=body, headers=headers)
        except httpx.HTTPError as e:
            return Seen("error", type(e).__name__, 0, (time.perf_counter() - start) * 1000)
        rtt = (time.perf_counter() - start) * 1000
        try:
            data = r.json()
        except ValueError:
            data = None
        outcome, reason = classify(r.status_code, data)
        return Seen(outcome, reason, r.status_code, rtt)
