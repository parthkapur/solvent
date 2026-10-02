from demo import derive
from demo.tests import fixture

EVENTS = fixture.build_events()


def of(lane, **kv):
    return [e for e in EVENTS if e["lane"] == lane and all(e.get(k) == v for k, v in kv.items())]


def test_every_lane_is_present_and_time_ordered():
    assert {e["lane"] for e in EVENTS} >= {
        "control", "phase", "client", "server", "azure", "alert", "sys", "kpi", "recon"}
    ts = [e["t"] for e in EVENTS]
    assert ts == sorted(ts)
    assert EVENTS[0]["kind"] == "start" and EVENTS[-1]["kind"] == "end"


def test_runaway_trips_the_denial_alert_and_baseline_does_not():
    kpis = of("kpi")
    baseline_end = fixture.T0 + 3 * 60_000
    assert max(k["denied_5m"] for k in kpis if k["t"] < baseline_end) <= 3
    assert max(k["denied_5m"] for k in kpis) > 5


def test_slow_phase_breaks_the_latency_objective():
    kpis = of("kpi")
    slow_start = fixture.T0 + 8 * 60_000
    assert max(k["p95_ms"] or 0 for k in kpis if k["t"] < slow_start) < 2000
    assert max(k["p95_ms"] or 0 for k in kpis) > 2000
    assert derive.minutes_over_slo(EVENTS) >= 3


def test_alerts_fire_after_their_cause_and_resolve():
    for rule in ("denied-writes", "latency-slo"):
        fired = [e for e in of("alert") if e["rule"] == f"{fixture.PROJECT}-{rule}"]
        assert [e["state"] for e in fired] == ["Fired", "Resolved"]
        assert fired[-1]["fired"] and fired[-1]["resolved"]  # Resolved carries both times


def test_azure_lags_the_agents_and_reconciliation_settles_ok():
    recon = of("recon")
    assert any(r["rows"]["allowed"]["status"] == "pending" for r in recon)
    final = recon[-1]["rows"]
    assert final["allowed"]["status"] == "ok" and final["denied"]["status"] == "ok"
    assert derive.settled(EVENTS)
