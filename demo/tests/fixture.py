"""A deterministic synthetic run over every lane, both incidents included, for tests and the page
check. Mirrors what the engine and the lanes publish; nothing here talks to a network."""

import random
from datetime import UTC, datetime

from demo import derive

T0 = 1_780_000_020_000  # minute-aligned, so per-minute bins line up
PROJECT = "solventdev"
PHASES = [("baseline", "Baseline", 3), ("runaway", "Runaway agent", 4), ("quiet", "Quiet", 1),
          ("slow", "Slow dependency", 4), ("recovery", "Recovery", 3)]


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_events() -> list[dict]:
    rnd = random.Random(7)
    ev: list[dict] = [{
        "t": T0, "lane": "control", "kind": "start", "profile": "live", "step": False,
        "speed": 1.0, "project": PROJECT,
        "phases": [{"key": k, "title": t, "minutes": m} for k, t, m in PHASES],
    }, {"t": T0 + 500, "lane": "sys", "kind": "azure_context", "sub": "",
        "rg": f"{PROJECT}-rg", "appi": f"{PROJECT}-appi", "law": f"{PROJECT}-law"}]

    starts: dict[str, int] = {}
    cursor = 0
    for i, (key, title, minutes) in enumerate(PHASES):
        starts[key] = cursor
        ev.append({"t": T0 + cursor * 1000, "lane": "phase", "kind": "start", "key": key,
                   "title": title, "index": i, "of": len(PHASES), "minutes": minutes})
        cursor += minutes * 60
        ev.append({"t": T0 + cursor * 1000 - 1, "lane": "phase", "kind": "end", "key": key})
    traffic_end = cursor
    ev.append({"t": T0 + traffic_end * 1000, "lane": "phase", "kind": "traffic_done"})
    slow_lo, slow_hi = starts["slow"], starts["slow"] + 4 * 60

    def call(sec, agent, tool, outcome, reason, status, rtt, asked=0):
        t = T0 + int(sec * 1000)
        ev.append({"t": t, "lane": "client", "agent": agent, "tool": tool, "outcome": outcome,
                   "reason": reason, "status": status, "rtt_ms": round(rtt, 1),
                   "fault_asked_ms": asked})
        allowed = outcome == "allowed"
        ev.append({"t": t + 1200, "lane": "server", "tool": tool if status != 401 else "mcp",
                   "decision": "allowed" if allowed else "denied",
                   "reason": "read" if allowed else reason,
                   "caller": "" if status == 401 else "shared_key",
                   "latency_ms": round(max(1.0, rtt - 250), 2), "gate_ms": 0.1,
                   "fault_ms": asked if allowed else 0, "trace_id": f"{rnd.getrandbits(128):032x}"})

    for sec in range(0, traffic_end, 5):  # the monitor agent
        slow = slow_lo <= sec < slow_hi
        call(sec, "monitor", "get_resource_health", "allowed", "ok", 200,
             rnd.gauss(3700 if slow else 700, 150 if slow else 90), 3000 if slow else 0)
    for sec in range(60, traffic_end, 180):
        call(sec, "analyst", "get_cost_summary", "denied", "classification_gated", 200, 260)
    for sec in range(120, traffic_end, 300):
        call(sec, "scanner", "mcp", "denied", "unauthenticated", 401, 120)
    for half in range(2 * 120):  # the runaway agent: 2 per second for 120 s
        call(starts["runaway"] + half / 2, "runaway", "restart_container_app", "denied",
             "approval_required", 200, 190 + rnd.random() * 30)

    fired_denied = starts["runaway"] + 390
    fired_slow = slow_lo + 420
    resolved_slow = traffic_end + 12 * 60
    end = resolved_slow + 30
    ev += [
        {"t": T0 + fired_denied * 1000, "lane": "alert", "rule": f"{PROJECT}-denied-writes",
         "state": "Fired", "fired": _iso(T0 + fired_denied * 1000), "resolved": None},
        {"t": T0 + (fired_denied + 480) * 1000, "lane": "alert",
         "rule": f"{PROJECT}-denied-writes", "state": "Resolved",
         "fired": _iso(T0 + fired_denied * 1000),
         "resolved": _iso(T0 + (fired_denied + 480) * 1000)},
        {"t": T0 + fired_slow * 1000, "lane": "alert", "rule": f"{PROJECT}-latency-slo",
         "state": "Fired", "fired": _iso(T0 + fired_slow * 1000), "resolved": None},
        {"t": T0 + resolved_slow * 1000, "lane": "alert", "rule": f"{PROJECT}-latency-slo",
         "state": "Resolved", "fired": _iso(T0 + fired_slow * 1000),
         "resolved": _iso(T0 + resolved_slow * 1000)},
    ]

    ev.sort(key=lambda e: e["t"])
    by_minute: dict[int, list[dict]] = {}
    for e in ev:
        if e["lane"] == "client":
            by_minute.setdefault((e["t"] - T0) // 60_000, []).append(e)

    def azure_rows(upto_minute: int) -> list[dict]:
        rows = []
        for m in range(upto_minute + 1):
            for decision in ("allowed", "denied"):
                calls = [c for c in by_minute.get(m, []) if c["outcome"] == decision]
                if calls:
                    rtts = sorted(c["rtt_ms"] - 250 for c in calls)
                    rows.append({"minute": _iso(T0 + m * 60_000), "decision": decision,
                                 "calls": len(calls),
                                 "p95": round(rtts[int(0.95 * (len(rtts) - 1))], 1)
                                 if decision == "allowed" else None})
        return rows

    out: list[dict] = []
    pending = iter(ev)
    nxt = next(pending, None)
    for t in range(T0, T0 + end * 1000, 5000):
        while nxt is not None and nxt["t"] <= t:
            out.append(nxt)
            nxt = next(pending, None)
        elapsed_min = (t - T0) // 60_000
        if (t - T0) % 60_000 == 0 and elapsed_min >= 3:  # Azure is about three minutes behind
            upto = min(elapsed_min - 3, traffic_end // 60)
            rows = azure_rows(upto)
            out.append({"t": t, "lane": "azure", "kind": "series", "rows": rows,
                        "data_until": rows[-1]["minute"] if rows else None})
        out.append({"t": t, "lane": "kpi", **derive.kpis(out, t)})
        out.append({"t": t, "lane": "recon",
                    "rows": derive.reconcile(out, derive.settled(out))})
    out += [e for e in [nxt, *pending] if e is not None]
    out.append({"t": T0 + end * 1000, "lane": "control", "kind": "end"})
    out.sort(key=lambda e: e["t"])
    return out
