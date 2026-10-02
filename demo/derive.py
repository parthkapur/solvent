"""Numbers computed from the event list. The page renders these and the postmortem quotes them,
so both always agree: neither recomputes anything."""

import math
from datetime import datetime

from demo.schedule import SLO_MS

SLO_BUDGET = 0.01  # objective: 99% of allowed calls within SLO_MS


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def iso_ms(s: str) -> float:
    return datetime.fromisoformat(s).timestamp() * 1000


def _client(events: list[dict]):
    return (e for e in events if e.get("lane") == "client" and not e.get("warmup"))


def kpis(events: list[dict], now_ms: float) -> dict:
    calls_1m, rtts, denied_5m, allowed, slow = 0, [], 0, 0, 0
    for e in _client(events):
        age = now_ms - e["t"]
        if 0 <= age <= 60_000:
            calls_1m += 1
            if e["outcome"] == "allowed":
                rtts.append(e["rtt_ms"])
        if 0 <= age <= 300_000 and e["outcome"] == "denied":
            denied_5m += 1
        if e["outcome"] == "allowed":
            allowed += 1
            if e["rtt_ms"] > SLO_MS:
                slow += 1
    consumed = 100.0 * slow / (SLO_BUDGET * allowed) if allowed else 0.0
    return {
        "calls_1m": calls_1m,
        "p95_ms": p95(rtts),
        "denied_5m": denied_5m,
        "budget_left_pct": round(max(0.0, 100.0 - consumed), 1),
        "budget_consumed_pct": round(consumed, 1),  # unclamped: 500 means five budgets spent
        "allowed_total": allowed,
        "slow_total": slow,
    }


def _series(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("lane") == "azure" and e.get("kind") == "series"]


def settled(events: list[dict]) -> bool:
    """Traffic has stopped and Azure has ingested the minute of the last call.

    The reference is the last call, not the moment traffic was declared done: Azure's
    `data_until` is the latest bin that has rows, and no rows can appear after the last call,
    so a `traffic_done` stamped in a later minute could never be reached.
    """
    done = next(
        (e["t"] for e in events if e.get("lane") == "phase" and e.get("kind") == "traffic_done"),
        None,
    )
    series = [e for e in _series(events) if e.get("data_until")]
    if done is None or not series:
        return False
    last_call = max((e["t"] for e in events if e.get("lane") == "client"), default=done)
    return iso_ms(series[-1]["data_until"]) >= last_call - (last_call % 60_000)


def reconcile(events: list[dict], settled: bool) -> dict[str, dict]:
    """Per decision: what the agents sent, what the server logged, what Azure has ingested."""
    rows = {d: {"client": 0, "server": 0, "azure": 0} for d in ("allowed", "denied")}
    t0 = next(
        (e["t"] for e in events if e.get("lane") == "control" and e.get("kind") == "start"), 0
    )
    for e in events:
        if e.get("lane") == "client" and e.get("outcome") in rows:
            rows[e["outcome"]]["client"] += 1
        elif e.get("lane") == "server" and e.get("decision") in rows:
            rows[e["decision"]]["server"] += 1
    series = _series(events)
    if series:
        floor = t0 - (t0 % 60_000)
        for r in series[-1]["rows"]:
            if r["decision"] in rows and iso_ms(r["minute"]) >= floor:
                rows[r["decision"]]["azure"] += int(r["calls"])
    for r in rows.values():
        r["gap"] = r["client"] - max(r["server"], r["azure"])
        r["status"] = "ok" if r["gap"] <= 0 else ("unaccounted" if settled else "pending")
    return rows


def minutes_over_slo(events: list[dict]) -> int:
    """Minutes (since the first agent call) whose p95 client round trip broke the objective."""
    calls = [e for e in _client(events) if e["outcome"] == "allowed"]
    if not calls:
        return 0
    first = min(e["t"] for e in calls)
    bins: dict[int, list[float]] = {}
    for e in calls:
        bins.setdefault(int((e["t"] - first) // 60_000), []).append(e["rtt_ms"])
    return sum(1 for v in bins.values() if (p95(v) or 0) > SLO_MS)
