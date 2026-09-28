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
    tok = policy.mint_token("get_cost_summary", {"days": 3}, SECRET, APPROVER)
    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        r = await server.mcp.call_tool(
            "get_cost_summary", {"days": 3, "approval_token": tok}
        )
    assert r.structured_content == {"days": 3, "rows": []}
    ev = json.loads(caplog.records[-1].message)
    assert (ev["tool"], ev["decision"]) == ("get_cost_summary", "allowed")


async def test_gated_read_denied_without_token():
    r = await server.mcp.call_tool("get_cost_summary", {"days": 3})
    assert r.is_error is False
    assert r.structured_content == {"denied": True, "reason": "classification_gated"}


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


def test_jwt_caller_is_allowed_without_shared_key(monkeypatch, http):
    """A verified JWT gets in on its own; the shared key does not need to be set."""
    monkeypatch.setattr(server, "API_KEY", "")
    monkeypatch.setattr(server.entra, "configured", lambda: True)
    monkeypatch.setattr(server.entra, "verified_claims", lambda t: {"oid": "caller-oid"})
    monkeypatch.setattr(server.entra, "caller_name", lambda c: c["oid"])
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "accept": "application/json, text/event-stream",
            "authorization": "Bearer a.jwt.here",
        },
    )
    assert r.status_code == 200


def test_an_unverifiable_jwt_is_401(monkeypatch, http):
    """A token that fails verification is a distinct denial from a missing header."""
    monkeypatch.setattr(server, "API_KEY", "")
    monkeypatch.setattr(server.entra, "configured", lambda: True)
    monkeypatch.setattr(server.entra, "verified_claims", lambda t: None)
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "accept": "application/json, text/event-stream",
            "authorization": "Bearer forged",
        },
    )
    assert r.status_code == 401
    assert r.json() == {"denied": True, "reason": "caller_token_invalid"}


def test_wrong_key_is_caller_token_invalid(monkeypatch, http):
    """A header that names neither the shared key nor a verifiable JWT gets its own reason,
    distinct from a request that offered no Authorization header at all."""
    monkeypatch.setattr(server, "API_KEY", "k3y")
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"accept": "application/json, text/event-stream", "authorization": "Bearer nope"},
    )
    assert r.status_code == 401
    assert r.json() == {"denied": True, "reason": "caller_token_invalid"}


def test_entra_configured_but_no_header_is_unauthenticated(monkeypatch, http):
    """Entra alone being configured must still deny a bare request, not open the server."""
    monkeypatch.setattr(server, "API_KEY", "")
    monkeypatch.setattr(server.entra, "configured", lambda: True)
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    assert r.json() == {"denied": True, "reason": "unauthenticated"}


def test_jwt_caller_is_named_in_a_real_audit_line(monkeypatch, http, caplog):
    """tools/list never touches policy._record, so the caller has to be proven through a
    governed tool call: a write with no approval_token is denied but still audited, and that
    audit line has to carry the JWT caller through the guard's contextvar into the tool call."""
    monkeypatch.setattr(server, "API_KEY", "")
    monkeypatch.setattr(server.entra, "configured", lambda: True)
    monkeypatch.setattr(server.entra, "verified_claims", lambda t: {"oid": "caller-oid"})
    monkeypatch.setattr(server.entra, "caller_name", lambda c: c["oid"])
    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        r = http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "restart_container_app", "arguments": {"name": "x"}},
            },
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer a.jwt.here",
            },
        )
    assert r.status_code == 200
    events = [json.loads(rec.message) for rec in caplog.records if rec.name == "solvent.audit"]
    assert any(
        e["tool"] == "restart_container_app" and e["caller"] == "caller-oid" for e in events
    )


def test_shared_key_is_named_in_a_real_audit_line(monkeypatch, http, caplog):
    """The shared key names itself, not a person, in the same real audit line."""
    monkeypatch.setattr(server, "API_KEY", "k3y")
    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        r = http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "restart_container_app", "arguments": {"name": "x"}},
            },
            headers={
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer k3y",
            },
        )
    assert r.status_code == 200
    events = [json.loads(rec.message) for rec in caplog.records if rec.name == "solvent.audit"]
    assert any(
        e["tool"] == "restart_container_app" and e["caller"] == "shared_key" for e in events
    )
