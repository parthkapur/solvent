import json
import logging

import pytest

from app import azure_clients, policy, server

SECRET = "s3cret"


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
    tok = policy.mint_token("restart_container_app", {"name": "x"}, SECRET)
    r = await server.mcp.call_tool("restart_container_app", {"name": "x", "approval_token": tok})
    assert r.structured_content == {"restarted": "x"}


async def test_healthz():
    from starlette.testclient import TestClient

    client = TestClient(server.app)
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/").json()["endpoints"]["health"] == "/healthz"
