import asyncio
import json

import httpx
import pytest

from demo.engine import Config, Engine
from demo.events import EventBus
from demo.schedule import Phase

PHASES = (
    Phase("baseline", "Baseline", 1),
    Phase("runaway", "Runaway", 1, runaway_s=30),
    Phase("slow", "Slow", 1, fault_ms=3000),
)


def rpc(data):
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [], "structuredContent": data, "isError": False}}


def make(step=False, phases=PHASES, speed=300.0):
    seen: list[tuple[str, str | None]] = []

    def handler(request):
        tool = json.loads(request.content)["params"]["name"]
        seen.append((tool, request.headers.get("x-solvent-fault")))
        if "authorization" not in request.headers:
            return httpx.Response(401, json={"denied": True, "reason": "unauthenticated"})
        if tool == "get_cost_summary":
            return httpx.Response(200, json=rpc({"denied": True, "reason": "classification_gated"}))
        if tool == "restart_container_app":
            return httpx.Response(200, json=rpc({"denied": True, "reason": "approval_required"}))
        return httpx.Response(200, json=rpc({"resource_group": "x", "items": []}))

    bus = EventBus()
    cfg = Config(url="http://t", api_key="k", speed=speed, step=step, lanes=False)
    return Engine(cfg, bus, transport=httpx.MockTransport(handler), phases=phases), bus, seen


async def until(pred, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.005)


def phase_events(bus, kind):
    return [e for e in bus.buffer if e.get("lane") == "phase" and e.get("kind") == kind]


def client_events(bus):
    return [e for e in bus.buffer if e.get("lane") == "client" and not e.get("warmup")]


def index_of(bus, **kv):
    return next(i for i, e in enumerate(bus.buffer) if all(e.get(k) == v for k, v in kv.items()))


async def test_a_full_run_walks_the_phases_in_order():
    engine, bus, _ = make()
    await asyncio.wait_for(engine.run(), 30)
    assert [e["key"] for e in phase_events(bus, "start")] == ["baseline", "runaway", "slow"]
    assert [e["key"] for e in phase_events(bus, "end")] == ["baseline", "runaway", "slow"]
    lanes_kinds = [(e["lane"], e.get("kind")) for e in bus.buffer]
    assert lanes_kinds[0] == ("control", "start")
    assert lanes_kinds[-1] == ("control", "end")
    assert ("phase", "traffic_done") in lanes_kinds
    start = bus.buffer[0]
    assert [p["key"] for p in start["phases"]] == ["baseline", "runaway", "slow"]
    assert engine.finished.is_set()


async def test_first_call_is_a_warmup_and_kpis_and_recon_are_published():
    engine, bus, _ = make()
    await asyncio.wait_for(engine.run(), 30)
    first_client = next(e for e in bus.buffer if e["lane"] == "client")
    assert first_client.get("warmup") is True
    assert any(e["lane"] == "kpi" for e in bus.buffer)
    assert any(e["lane"] == "recon" for e in bus.buffer)


async def test_runaway_traffic_only_in_the_incident_and_only_for_runaway_s():
    engine, bus, _ = make()
    await asyncio.wait_for(engine.run(), 30)
    runaway = [(i, e) for i, e in enumerate(bus.buffer)
               if e.get("lane") == "client" and e.get("agent") == "runaway"]
    assert runaway and all(e["outcome"] == "denied" for _, e in runaway)
    start = index_of(bus, lane="phase", kind="start", key="runaway")
    end = index_of(bus, lane="phase", kind="end", key="runaway")
    assert all(start < i < end for i, _ in runaway)
    assert len(runaway) <= 30 * 2 + 2  # runaway_s * RUNAWAY_PER_S, plus a tick of slack


async def test_fault_header_only_during_the_slow_phase():
    engine, bus, seen = make()
    await asyncio.wait_for(engine.run(), 30)
    faulted = [(i, e) for i, e in enumerate(bus.buffer)
               if e.get("lane") == "client" and e.get("fault_asked_ms")]
    assert faulted and all(e["agent"] == "monitor" for _, e in faulted)
    slow_start = index_of(bus, lane="phase", kind="start", key="slow")
    assert all(i > slow_start for i, _ in faulted)
    assert ("get_resource_health", "latency_ms=3000") in seen


async def test_baseline_only_denies_what_the_baseline_agents_deny():
    engine, bus, _ = make(phases=(Phase("baseline", "Baseline", 2),))
    await asyncio.wait_for(engine.run(), 30)
    denied = {e["agent"] for e in client_events(bus) if e["outcome"] == "denied"}
    assert denied <= {"analyst", "scanner"}


async def test_step_mode_holds_a_phase_until_next():
    engine, bus, _ = make(step=True)
    task = engine.start()
    await until(lambda: len(phase_events(bus, "start")) == 1)
    await asyncio.sleep(0.4)  # far longer than the phase's own minute at this speed
    assert len(phase_events(bus, "end")) == 0
    assert engine.handle("next") == {"ok": True}
    await until(lambda: len(phase_events(bus, "start")) == 2)
    engine.handle("stop")
    await asyncio.wait_for(task, 10)


async def test_pause_freezes_traffic_and_resume_restarts_it():
    engine, bus, _ = make(phases=(Phase("baseline", "Baseline", 30),))
    task = engine.start()
    await until(lambda: len(client_events(bus)) >= 3)
    engine.handle("pause")
    await asyncio.sleep(0.05)
    frozen = len(client_events(bus))
    await asyncio.sleep(0.3)
    assert len(client_events(bus)) - frozen <= 1  # at most one call already in flight
    engine.handle("resume")
    await until(lambda: len(client_events(bus)) > frozen + 2)
    engine.handle("stop")
    await asyncio.wait_for(task, 10)


async def test_stop_while_paused_does_not_hang():
    engine, bus, _ = make(phases=(Phase("baseline", "Baseline", 30),))
    task = engine.start()
    await until(lambda: len(client_events(bus)) >= 1)
    engine.handle("pause")
    engine.handle("stop")
    await asyncio.wait_for(task, 5)
    assert engine.finished.is_set()


async def test_manual_fault_is_sent_and_clamped():
    engine, bus, seen = make(phases=(Phase("baseline", "Baseline", 30),))
    task = engine.start()
    await until(lambda: len(client_events(bus)) >= 2)
    engine.handle("fault", ms=1500)
    await until(lambda: ("get_resource_health", "latency_ms=1500") in seen)
    assert engine.handle("fault", ms=-5) == {"ok": True}
    assert engine.manual_fault_ms == 0
    engine.handle("fault", ms=10**9)
    assert engine.manual_fault_ms == 10_000
    engine.handle("stop")
    await asyncio.wait_for(task, 10)


async def test_fault_rejects_a_non_number_and_unknown_actions_are_refused():
    engine, _, _ = make()
    with pytest.raises(ValueError):
        engine.handle("fault", ms="abc")
    assert engine.handle("explode")["ok"] is False


async def test_fault_rejects_infinite_and_nan():
    engine, _, _ = make()
    for bad in (float("inf"), float("-inf"), float("nan"), "1e999"):
        with pytest.raises(ValueError):
            engine.handle("fault", ms=bad)
    assert engine.manual_fault_ms == 0


async def test_a_second_start_is_refused():
    engine, _, _ = make(phases=(Phase("baseline", "Baseline", 30),))
    task = engine.start()
    assert engine.handle("start") == {"ok": False, "error": "already started"}
    engine.handle("stop")
    await asyncio.wait_for(task, 10)
