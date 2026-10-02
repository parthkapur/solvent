import asyncio
import json
from pathlib import Path

import pytest

from demo import lanes
from demo.events import EventBus

FIX = Path(__file__).with_name("fixtures")
AUDIT = {"ts": 1_000_000.0, "caller": "shared_key", "tool": "get_resource_health",
         "args_hash": "ab", "decision": "allowed", "reason": "read", "latency_ms": 812.4,
         "gate_ms": 0.09, "trace_id": "0" * 32, "span_id": "0" * 16}


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(AUDIT),
        json.dumps({"TimeStamp": "x", "Log": json.dumps(AUDIT), "Stream_s": "stderr"}),
        json.dumps({"Log": "F INFO:solvent.audit:" + json.dumps(AUDIT)}),
        "INFO:solvent.audit:" + json.dumps(AUDIT),
    ],
)
def test_parse_audit_line_finds_the_audit_json_in_every_wrapping(raw):
    assert lanes.parse_audit_line(raw)["decision"] == "allowed"


@pytest.mark.parametrize(
    "raw",
    ["", "not json at all", '{"Log": "F INFO:     GET /healthz 200"}', '{"a": 1}', "{broken"],
)
def test_parse_audit_line_ignores_everything_else(raw):
    assert lanes.parse_audit_line(raw) is None


def test_real_logstream_fixture_parses():
    lines = (FIX / "logstream.sample.jsonl").read_text().splitlines()
    parsed = [lanes.parse_audit_line(x) for x in lines]
    audits = [p for p in parsed if p]
    assert audits, "the spike captured real audit lines; none parsed"
    assert all(p["decision"] in {"allowed", "denied"} for p in audits)


def test_to_server_event_keeps_the_fields_the_page_needs():
    ev = lanes.to_server_event({**AUDIT, "fault_ms": 3000})
    assert ev["lane"] == "server"
    assert (ev["decision"], ev["gate_ms"], ev["fault_ms"]) == ("allowed", 0.09, 3000)
    assert lanes.to_server_event(AUDIT)["fault_ms"] == 0


def audit_line(ts, decision="allowed"):
    return json.dumps({"Log": "F INFO:solvent.audit:"
                       + json.dumps({**AUDIT, "ts": ts, "decision": decision})})


class FakeProc:
    def __init__(self, out="", err="", rc=0):
        self._out, self._err, self.returncode = out.encode(), err.encode(), rc

    async def communicate(self):
        return self._out, self._err


def fake_spawn(*polls):
    """One FakeProc per poll, in order; the last one repeats."""
    calls = []

    async def spawn(*args, **kw):
        calls.append(args)
        return polls[min(len(calls) - 1, len(polls) - 1)]

    spawn.calls = calls
    return spawn


def started_bus():
    bus = EventBus()
    bus.publish({"lane": "control", "kind": "start", "t": 1_000_000_000})
    return bus


async def test_stream_server_log_publishes_each_new_audit_line_once():
    bus = started_bus()
    a, b, c = audit_line(1_000_001.0), audit_line(1_000_002.0), audit_line(1_000_003.0)
    spawn = fake_spawn(FakeProc(f"{a}\nnoise\n{b}"), FakeProc(f"{b}\n{c}"))
    await lanes.stream_server_log(bus, "solventdev-app", "solventdev-rg", spawn=spawn,
                                  period_s=0, polls=2)
    assert len([e for e in bus.buffer if e["lane"] == "server"]) == 3
    assert spawn.calls[0][:4] == ("az", "containerapp", "logs", "show")
    assert "--tail" in spawn.calls[0] and "--follow" not in spawn.calls[0]


async def test_stream_server_log_skips_lines_from_before_the_run():
    bus = started_bus()
    old, new = audit_line(999_000.0), audit_line(1_000_001.0)
    spawn = fake_spawn(FakeProc(f"{old}\n{new}"))
    await lanes.stream_server_log(bus, "a", "rg", spawn=spawn, period_s=0, polls=1)
    assert len([e for e in bus.buffer if e["lane"] == "server"]) == 1


async def test_stream_server_log_reports_a_missing_az_once():
    bus = started_bus()

    async def spawn(*args, **kw):
        raise FileNotFoundError("az")

    await lanes.stream_server_log(bus, "a", "rg", spawn=spawn, period_s=0, polls=3)
    gone = [e for e in bus.buffer if e.get("kind") == "lane_unavailable"]
    assert len(gone) == 1 and gone[0]["name"] == "server"
    assert "cannot run az" in gone[0]["reason"]


async def test_stream_server_log_reports_the_last_stderr_line_of_a_failing_az():
    bus = started_bus()
    spawn = fake_spawn(FakeProc(err="warning\nERROR: Please run 'az login'\n", rc=1))
    await lanes.stream_server_log(bus, "a", "rg", spawn=spawn, period_s=0, polls=1)
    gone = [e for e in bus.buffer if e.get("kind") == "lane_unavailable"]
    assert "az login" in gone[0]["reason"]


class FakeAz:
    """Records the args and returns canned payloads keyed by the leading args."""

    def __init__(self, payloads):
        self.payloads, self.calls = payloads, []

    async def __call__(self, *args):
        self.calls.append(args)
        for key, value in self.payloads.items():
            if args[: len(key)] == key:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected az call {args}")


async def test_series_once_publishes_rows_and_how_far_azure_has_ingested():
    bus = EventBus()
    az = FakeAz({("monitor", "log-analytics", "query"): [
        {"minute": "2026-05-28T10:00:00Z", "decision": "allowed", "calls": "12", "p95": 800.5},
        {"minute": "2026-05-28T10:01:00Z", "decision": "denied", "calls": 2, "p95": None},
    ]})
    await lanes.series_once(bus, az, "ws-guid", 10)
    ev = bus.buffer[0]
    assert (ev["lane"], ev["kind"], ev["data_until"]) == ("azure", "series", "2026-05-28T10:01:00Z")
    assert ev["rows"][0] == {"minute": "2026-05-28T10:00:00Z", "decision": "allowed",
                             "calls": 12, "p95": 800.5}
    assert "ws-guid" in az.calls[0] and "ago(10m)" in az.calls[0][-1]


async def test_series_once_turns_log_analytics_strings_into_numbers_and_nulls():
    # `az monitor log-analytics query` returns every value as a string, "None" for no value.
    bus = EventBus()
    az = FakeAz({("monitor", "log-analytics", "query"): [
        {"TableName": "PrimaryResult", "minute": "2026-09-30T15:06:00Z", "decision": "denied",
         "calls": "3", "p95": "None"},
        {"TableName": "PrimaryResult", "minute": "2026-09-30T15:07:00Z", "decision": "allowed",
         "calls": "12", "p95": "812.5"},
        {"TableName": "PrimaryResult", "minute": "2026-09-30T15:08:00Z", "decision": "allowed",
         "calls": "1", "p95": ""},
    ]})
    await lanes.series_once(bus, az, "ws", 10)
    assert [(r["calls"], r["p95"]) for r in bus.buffer[0]["rows"]] == [(3, None), (12, 812.5), (1, None)]


def test_the_real_log_analytics_fixture_parses_through_series_once():
    import asyncio

    rows = json.loads((FIX / "loganalytics.sample.json").read_text())
    bus = EventBus()
    asyncio.run(lanes.series_once(bus, FakeAz({("monitor", "log-analytics", "query"): rows}), "ws", 60))
    published = bus.buffer[0]["rows"]
    assert published and all(isinstance(r["calls"], int) for r in published)
    assert all(r["p95"] is None or isinstance(r["p95"], float) for r in published)


ALERTS = {"value": [
    {"properties": {"essentials": {
        "alertRule": "/subscriptions/x/resourceGroups/rg/providers/microsoft.insights/"
                     "scheduledqueryrules/solventdev-denied-writes",
        "monitorCondition": "Resolved", "startDateTime": "2026-05-28T10:05:00Z",
        "monitorConditionResolvedDateTime": "2026-05-28T10:13:00Z"}}},
    {"properties": {"essentials": {
        "alertRule": "/x/solventdev-denied-writes", "monitorCondition": "Fired",
        "startDateTime": "2026-05-28T11:00:00Z"}}},
    {"properties": {"essentials": {
        "alertRule": "/x/solventdev-latency-slo", "monitorCondition": "Fired",
        "startDateTime": "2026-05-27T09:00:00Z"}}},
]}


def test_parse_alerts_keeps_the_newest_per_rule_and_only_this_run():
    since = lanes.iso_ms("2026-05-28T10:00:00Z")
    out = {e["rule"]: e for e in lanes.parse_alerts(ALERTS, since)}
    assert set(out) == {"solventdev-denied-writes"}  # the latency alert fired before the run
    denied = out["solventdev-denied-writes"]
    assert (denied["state"], denied["fired"]) == ("Fired", "2026-05-28T11:00:00Z")


async def test_alerts_once_publishes_only_changes():
    bus, seen = EventBus(), {}
    az = FakeAz({("rest",): ALERTS})
    since = lanes.iso_ms("2026-05-28T10:00:00Z")
    await lanes.alerts_once(bus, az, "sub", "rg", seen, since)
    await lanes.alerts_once(bus, az, "sub", "rg", seen, since)
    assert len(bus.buffer) == 1
    assert "targetResourceGroup=rg" in az.calls[0][-1]


def test_run_minutes_covers_the_run_plus_ingestion_lag():
    bus = EventBus(clock=lambda: 1000.0 + 20 * 60)
    bus.publish({"lane": "control", "kind": "start", "t": 1_000_000})
    assert lanes.run_start_ms(bus) == 1_000_000
    assert lanes.run_minutes(bus) == 23


async def test_az_json_reports_the_last_stderr_line():
    class Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"warning\nERROR: Please run 'az login'\n"

    async def spawn(*a, **kw):
        return Proc()

    with pytest.raises(lanes.AzError, match="az login"):
        await lanes.az_json("account", "show", spawn=spawn)


async def test_az_json_parses_stdout():
    class Proc:
        returncode = 0

        async def communicate(self):
            return b'{"id": "abc"}', b""

    async def spawn(*a, **kw):
        assert a[-2:] == ("-o", "json")
        return Proc()

    assert await lanes.az_json("account", "show", spawn=spawn) == {"id": "abc"}


async def test_poll_azure_reports_an_unexpected_shape_instead_of_dying():
    bus = EventBus()
    az = FakeAz({("monitor", "log-analytics", "workspace"): None})  # az printed "null"
    await lanes.poll_azure(bus, "solventdev", az=az)
    ev = bus.buffer[0]
    assert (ev["kind"], ev["name"]) == ("lane_unavailable", "azure")
    assert "TypeError" in ev["reason"]


async def test_forever_reports_an_unexpected_error_once_and_keeps_polling():
    bus, calls = EventBus(), []

    async def once():
        calls.append(1)
        raise TypeError("boom")

    task = asyncio.create_task(lanes._forever(bus, "azure", 0, once))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    gone = [e for e in bus.buffer if e.get("kind") == "lane_unavailable"]
    assert len(gone) == 1 and "TypeError: boom" in gone[0]["reason"]
    assert len(calls) > 1


async def test_stream_server_log_skips_a_line_with_a_non_numeric_ts_and_keeps_going():
    bus = started_bus()
    bad = json.dumps({"Log": "F INFO:solvent.audit:" + json.dumps({**AUDIT, "ts": "soon"})})
    good = audit_line(1_000_001.0)
    spawn = fake_spawn(FakeProc(f"{bad}\n{good}"))
    await lanes.stream_server_log(bus, "a", "rg", spawn=spawn, period_s=0, polls=1)
    assert len([e for e in bus.buffer if e["lane"] == "server"]) == 1


async def test_poll_azure_reports_a_failing_login_and_stops():
    bus = EventBus()
    az = FakeAz({("monitor", "log-analytics", "workspace"): lanes.AzError("Please run 'az login'")})
    await lanes.poll_azure(bus, "solventdev", az=az)
    assert bus.buffer[0]["kind"] == "lane_unavailable"
    assert bus.buffer[0]["name"] == "azure"
