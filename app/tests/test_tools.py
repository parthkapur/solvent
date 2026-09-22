import json
import logging

import pytest

from app import azure_clients, policy, server

SECRET = "s3cret"
APPROVER = "ops@example.com"


@pytest.fixture(scope="module")
def http():
    """One client for the whole module.

    `streamable_http_app`'s session manager refuses a second `run()`, and TestClient only
    starts the lifespan as a context manager, so entering it per-test breaks the suite.
    """
    from starlette.testclient import TestClient

    with TestClient(server.app) as client:
        yield client


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", SECRET)
    monkeypatch.setattr(azure_clients, "cost_summary", lambda days: {"days": days, "rows": []})
    monkeypatch.setattr(
        azure_clients, "resource_health", lambda rg: {"resource_group": rg, "items": []}
    )
    monkeypatch.setattr(azure_clients, "restart_container_app", lambda name: {"restarted": name})


async def test_lists_three_tools():
    names = sorted(t.name for t in await server.mcp.list_tools())
    assert names == ["get_cost_summary", "get_resource_health", "restart_container_app"]


async def test_read_tool_returns_structured(caplog):
    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        r = await server.mcp.call_tool("get_cost_summary", {"days": 3})
    assert r.structured_content == {"days": 3, "rows": []}
    ev = json.loads(caplog.records[-1].message)
    assert (ev["tool"], ev["decision"]) == ("get_cost_summary", "allowed")


async def test_write_tool_denied_without_token():
    r = await server.mcp.call_tool("restart_container_app", {"name": "x"})
    assert r.is_error is False
    assert r.structured_content == {"denied": True, "reason": "approval_required"}


async def test_write_tool_allowed_with_token():
    tok = policy.mint_token("restart_container_app", {"name": "x"}, SECRET, APPROVER)
    r = await server.mcp.call_tool("restart_container_app", {"name": "x", "approval_token": tok})
    assert r.structured_content == {"restarted": "x"}


async def test_healthz(http):
    assert http.get("/healthz").json() == {"ok": True}
    assert http.get("/").json()["endpoints"]["health"] == "/healthz"


def test_mcp_requires_the_key(monkeypatch, caplog, http):
    monkeypatch.setattr(server, "API_KEY", "k3y")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    hdrs = {"accept": "application/json, text/event-stream"}

    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        r = http.post("/mcp", json=body, headers=hdrs)
    assert r.status_code == 401
    assert r.json() == {"denied": True, "reason": "unauthenticated"}
    ev = json.loads(caplog.records[-1].message)
    assert (ev["tool"], ev["decision"], ev["reason"]) == ("mcp", "denied", "unauthenticated")

    ok = http.post("/mcp", json=body, headers={**hdrs, "authorization": "Bearer k3y"})
    assert ok.status_code == 200


def test_wrong_key_denied(monkeypatch, http):
    monkeypatch.setattr(server, "API_KEY", "k3y")
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"accept": "application/json, text/event-stream", "authorization": "Bearer nope"},
    )
    assert r.status_code == 401


def test_probes_stay_open(monkeypatch, http):
    """The pipeline smoke test and the liveness probe must not need a key."""
    monkeypatch.setattr(server, "API_KEY", "k3y")
    assert http.get("/healthz").status_code == 200
    assert http.get("/").status_code == 200


def test_guard_is_inert_without_a_key(monkeypatch, http):
    """Local development runs unauthenticated, as it does today."""
    monkeypatch.setattr(server, "API_KEY", "")
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200


def test_non_ascii_auth_header_denied_not_crashed(monkeypatch, http):
    """A header that is not valid UTF-8 must be a 401, not a 500."""
    monkeypatch.setattr(server, "API_KEY", "k3y")
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "accept": "application/json, text/event-stream",
            "authorization": b"Bearer \x80\xff",
        },
    )
    assert r.status_code == 401
