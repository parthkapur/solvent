import httpx
import pytest

from demo.agents import Caller, classify


def rpc(data):
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": data,
                                                  "isError": False}}


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {"denied": True, "reason": "unauthenticated"}, ("denied", "unauthenticated")),
        (200, rpc({"denied": True, "reason": "classification_gated"}),
         ("denied", "classification_gated")),
        (200, rpc({"items": []}), ("allowed", "ok")),
        (200, rpc({"error": "boom"}), ("error", "tool_error")),
        (200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}, ("error", "rpc_error")),
        (502, None, ("error", "http_502")),
        (200, {"unexpected": True}, ("error", "no_result")),
    ],
)
def test_classify(status, body, expected):
    assert classify(status, body) == expected


def make(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t")
    return Caller(client, "k3y")


async def test_call_sends_auth_and_fault_headers_only_when_asked():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json=rpc({"items": []}))

    caller = make(handler)
    out = await caller.call("get_resource_health", {"resource_group": "rg"})
    assert (out.outcome, out.status) == ("allowed", 200)
    assert seen["authorization"] == "Bearer k3y"
    assert "x-solvent-fault" not in seen

    await caller.call("get_resource_health", {}, fault_ms=3000)
    assert seen["x-solvent-fault"] == "latency_ms=3000"


async def test_unauthenticated_call_carries_no_key():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(401, json={"denied": True, "reason": "unauthenticated"})

    out = await make(handler).call("get_resource_health", {}, auth=False)
    assert (out.outcome, out.reason, out.status) == ("denied", "unauthenticated", 401)
    assert "authorization" not in seen


async def test_connection_failure_is_an_error_outcome_not_an_exception():
    def handler(request):
        raise httpx.ConnectError("no route")

    out = await make(handler).call("get_resource_health", {})
    assert (out.outcome, out.reason, out.status) == ("error", "ConnectError", 0)


async def test_non_json_body_is_an_error_outcome():
    out = await make(lambda r: httpx.Response(200, text="<html>")).call("t", {})
    assert out.outcome == "error"
