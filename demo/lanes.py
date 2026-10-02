"""Lanes 2 and 3: what the server logged (seconds), and what Azure Monitor has ingested (minutes).

Both shell out to `az` with the operator's own login. Each lane fails on its own: an error becomes
a `lane_unavailable` event with the reason and the run carries on.

Lane 2 polls `az containerapp logs show --tail` rather than following the stream: `--follow`
returns nothing on this subscription (checked cold, warm and unbuffered), `--tail` is reliable.
"""

import asyncio
import json
import math
from asyncio.subprocess import PIPE

from demo.derive import iso_ms
from demo.events import EventBus

LOG_POLL_S = 4.0
LOG_TAIL = 300
SERIES_EVERY_S = 60
ALERTS_EVERY_S = 30

KQL_SERIES = """
AppTraces
| where TimeGenerated > ago({minutes}m) and isnotempty(Properties.decision)
| summarize calls = count(), p95 = percentile(todouble(Properties.latency_ms), 95)
    by minute = bin(TimeGenerated, 1m), decision = tostring(Properties.decision)
| order by minute asc
"""


class AzError(Exception):
    pass


def unavailable(name: str, reason: str) -> dict:
    return {"lane": "sys", "kind": "lane_unavailable", "name": name, "reason": reason}


def _describe(e: Exception) -> str:
    """An `az` failure already reads well; anything else needs its type to be diagnosable."""
    return str(e) if isinstance(e, AzError) else f"{type(e).__name__}: {e}"


async def _run_az(spawn, *args: str) -> bytes:
    try:
        proc = await spawn("az", *args, stdout=PIPE, stderr=PIPE)
    except OSError as e:
        raise AzError(f"cannot run az: {e}") from e
    out, err = await proc.communicate()
    if proc.returncode:
        tail = err.decode("utf-8", "replace").strip().splitlines()
        raise AzError(tail[-1] if tail else f"az exited {proc.returncode}")
    return out


async def az_json(*args: str, spawn=asyncio.create_subprocess_exec):
    return json.loads(await _run_az(spawn, *args, "-o", "json") or b"null")


def parse_audit_line(raw: str) -> dict | None:
    """The audit JSON in one line of `az containerapp logs show --format json`, or None.

    The line is either the audit dict itself, a wrapper with the console text in `Log`
    (`F INFO:solvent.audit:{...}`), or plain console text; the JSON is whatever starts at the
    first brace.
    """
    try:
        outer = json.loads(raw)
    except ValueError:
        outer = None
    if isinstance(outer, dict) and {"tool", "decision"} <= outer.keys():
        return outer
    text = outer.get("Log", "") if isinstance(outer, dict) else raw
    start = text.find("{")
    if start < 0:
        return None
    try:
        audit = json.loads(text[start:])
    except ValueError:
        return None
    return audit if isinstance(audit, dict) and {"tool", "decision"} <= audit.keys() else None


def to_server_event(audit: dict) -> dict:
    return {
        "lane": "server",
        "tool": audit.get("tool"),
        "decision": audit.get("decision"),
        "reason": audit.get("reason", ""),
        "caller": audit.get("caller", ""),
        "latency_ms": audit.get("latency_ms"),
        "gate_ms": audit.get("gate_ms"),
        "fault_ms": audit.get("fault_ms", 0),
        "trace_id": audit.get("trace_id", ""),
    }


def run_start_ms(bus: EventBus) -> int:
    return next(
        (e["t"] for e in bus.buffer if e.get("lane") == "control" and e.get("kind") == "start"),
        bus.now_ms(),
    )


def run_minutes(bus: EventBus) -> int:
    """How far back to query: the whole run so far plus a margin for ingestion lag."""
    return max(5, math.ceil((bus.now_ms() - run_start_ms(bus)) / 60_000) + 3)


async def stream_server_log(
    bus: EventBus,
    app: str,
    rg: str,
    spawn=asyncio.create_subprocess_exec,
    period_s: float = LOG_POLL_S,
    polls: int | None = None,  # None polls until cancelled; tests pass a count
) -> None:
    """Publish each new audit line from the app's console log, seconds after it was written."""
    cmd = ("containerapp", "logs", "show", "-n", app, "-g", rg,
           "--tail", str(LOG_TAIL), "--format", "json")
    seen: set[str] = set()
    last_error = None
    done = 0
    while polls is None or done < polls:
        done += 1
        try:
            out = await _run_az(spawn, *cmd)
            last_error = None
        except AzError as e:
            if str(e) != last_error:  # say it once, not every poll
                bus.publish(unavailable("server", str(e)))
                last_error = str(e)
            await asyncio.sleep(period_s)
            continue
        cutoff = run_start_ms(bus) / 1000 - 2  # audit `ts` is epoch seconds
        current: set[str] = set()
        for raw in out.decode("utf-8", "replace").splitlines():
            current.add(raw)
            if raw in seen:
                continue
            audit = parse_audit_line(raw)
            try:
                fresh = bool(audit) and audit.get("ts", 0) >= cutoff
            except TypeError:  # a `ts` that is not a number cannot be placed in time: skip the line
                continue
            if fresh:
                bus.publish(to_server_event(audit))
        seen = current
        await asyncio.sleep(period_s)


def _num(value) -> float | None:
    """`az monitor log-analytics query` returns every value as a string, "None" for no value."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


async def series_once(bus: EventBus, az, workspace_id: str, minutes: int) -> None:
    rows = await az(
        "monitor", "log-analytics", "query", "-w", workspace_id,
        "--analytics-query", KQL_SERIES.format(minutes=minutes),
    )
    rows = [
        {"minute": r["minute"], "decision": r["decision"], "calls": int(r["calls"]),
         "p95": _num(r.get("p95"))}
        for r in rows
    ]
    bus.publish({"lane": "azure", "kind": "series", "rows": rows,
                 "data_until": max((r["minute"] for r in rows), default=None)})


def parse_alerts(payload: dict, since_ms: float) -> list[dict]:
    """The newest alert instance per rule that fired at or after `since_ms`."""
    best: dict[str, dict] = {}
    for item in payload.get("value", []):
        e = item.get("properties", {}).get("essentials", {})
        fired = e.get("startDateTime")
        if not fired or iso_ms(fired) < since_ms:
            continue
        rule = str(e.get("alertRule", "")).rsplit("/", 1)[-1]
        if rule not in best or fired > best[rule]["fired"]:
            best[rule] = {
                "lane": "alert", "rule": rule, "state": e.get("monitorCondition", "Fired"),
                "fired": fired, "resolved": e.get("monitorConditionResolvedDateTime"),
            }
    return list(best.values())


async def alerts_once(bus: EventBus, az, sub: str, rg: str, seen: dict, since_ms: float) -> None:
    url = (
        f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.AlertsManagement/"
        f"alerts?api-version=2019-05-05-preview&timeRange=1d&targetResourceGroup={rg}"
    )
    for ev in parse_alerts(await az("rest", "--method", "get", "--url", url), since_ms):
        key = (ev["state"], ev["fired"], ev["resolved"])
        if seen.get(ev["rule"]) != key:
            seen[ev["rule"]] = key
            bus.publish(ev)


async def _forever(bus: EventBus, name: str, period: float, once) -> None:
    last = None
    while True:
        try:
            await once()
            last = None
        except Exception as e:  # noqa: BLE001 - a lane must report any failure, never die silently
            if _describe(e) != last:  # say it once, not every poll
                bus.publish(unavailable(name, _describe(e)))
                last = _describe(e)
        await asyncio.sleep(period)


async def poll_azure(bus: EventBus, project: str, az=az_json) -> None:
    rg = f"{project}-rg"
    try:
        ws = await az("monitor", "log-analytics", "workspace", "show", "-g", rg,
                      "-n", f"{project}-law")
        workspace_id = ws["customerId"]
        sub = (await az("account", "show"))["id"]
    except Exception as e:  # noqa: BLE001 - report any setup failure instead of dying silently
        bus.publish(unavailable("azure", _describe(e)))
        return
    bus.publish({"lane": "sys", "kind": "azure_context", "sub": sub, "rg": rg,
                 "appi": f"{project}-appi", "law": f"{project}-law"})
    seen: dict = {}
    await asyncio.gather(
        _forever(bus, "azure", SERIES_EVERY_S,
                 lambda: series_once(bus, az, workspace_id, run_minutes(bus))),
        _forever(bus, "alerts", ALERTS_EVERY_S,
                 lambda: alerts_once(bus, az, sub, rg, seen, run_start_ms(bus) - 60_000)),
    )
