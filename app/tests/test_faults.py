"""The dev-only latency knob: inert unless FAULT_INJECTION=1, capped, never silent."""

import json
import logging
import time

import pytest

from app import faults, policy, server


@pytest.fixture(autouse=True)
def _clean():
    faults.FAULT_MS.set(0)
    yield
    faults.FAULT_MS.set(0)


def test_header_ignored_when_off(monkeypatch):
    monkeypatch.delenv("FAULT_INJECTION", raising=False)
    assert faults.from_header(b"latency_ms=3000") == 0


@pytest.mark.parametrize("value", ["true", "yes", "0", "", "on"])
def test_only_exactly_one_turns_it_on(monkeypatch, value):
    monkeypatch.setenv("FAULT_INJECTION", value)
    assert faults.from_header(b"latency_ms=3000") == 0


def test_header_honoured_when_on(monkeypatch):
    monkeypatch.setenv("FAULT_INJECTION", "1")
    assert faults.from_header(b"latency_ms=3000") == 3000
    assert faults.from_header(b"  latency_ms=250 ") == 250


def test_header_capped(monkeypatch):
    monkeypatch.setenv("FAULT_INJECTION", "1")
    assert faults.from_header(b"latency_ms=999999") == faults.MAX_MS


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"latency_ms=",
        b"latency_ms=-5",
        b"latency_ms=abc",
        b"latency=3000",
        b"LATENCY_MS=3000",
        b"latency_ms=3000; drop table",
        b"latency_ms=1234567",
        b"\xff\xfe",
    ],
)
def test_garbage_ignored(monkeypatch, raw):
    monkeypatch.setenv("FAULT_INJECTION", "1")
    assert faults.from_header(raw) == 0


def _read_tool():
    @policy.governed
    def get_resource_health(**kwargs):  # a read in policy.yaml, so it needs no token
        return {"ok": True}

    return get_resource_health


def test_delay_sits_inside_recorded_latency_and_audit_says_so(caplog):
    faults.FAULT_MS.set(120)
    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        assert _read_tool()(resource_group="rg") == {"ok": True}
    ev = json.loads(caplog.records[-1].message)
    assert ev["fault_ms"] == 120
    assert ev["latency_ms"] >= 120
    assert ev["gate_ms"] < 50  # deciding takes microseconds; the delay is not the gate's


def test_no_fault_no_fault_ms(caplog):
    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        _read_tool()(resource_group="rg")
    ev = json.loads(caplog.records[-1].message)
    assert "fault_ms" not in ev
    assert "gate_ms" in ev


def test_denied_call_is_not_delayed(monkeypatch, caplog):
    monkeypatch.setenv("APPROVAL_SECRET", "s3cret")
    faults.FAULT_MS.set(500)

    @policy.governed
    def restart_container_app(**kwargs):
        return {"restarted": True}

    start = time.perf_counter()
    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        assert restart_container_app(name="x") == {"denied": True, "reason": "approval_required"}
    assert time.perf_counter() - start < 0.4
    assert "fault_ms" not in json.loads(caplog.records[-1].message)


async def test_guard_sets_the_fault_only_for_an_authenticated_caller(monkeypatch):
    monkeypatch.setenv("FAULT_INJECTION", "1")
    monkeypatch.setattr(server, "API_KEY", "k3y")
    seen: list[int] = []

    async def inner(scope, receive, send):
        seen.append(faults.FAULT_MS.get())

    async def receive():
        return {}

    async def send(message):
        pass

    def scope(authenticated: bool):
        headers = [(b"x-solvent-fault", b"latency_ms=700")]
        if authenticated:
            headers.append((b"authorization", b"Bearer k3y"))
        return {"type": "http", "path": "/mcp", "headers": headers}

    guard = server.require_key(inner)
    await guard(scope(True), receive, send)
    await guard(scope(False), receive, send)  # 401s before `inner` is ever called
    assert seen == [700]
