"""python -m demo.report demo/out/events-....jsonl [--force]

Turns a captured run into docs/demo/report.html (the page, with the events inlined, replay mode)
and docs/demo/postmortem.md (numbers and timeline filled in; root cause and actions are left for
a human, who has looked at the run).
"""

import argparse
import re
from datetime import UTC, datetime
from pathlib import Path

from demo import derive, live
from demo.events import read_events

DOCS = Path(__file__).resolve().parent.parent / "docs" / "demo"
MARK = "<!-- write after reviewing the run -->"


_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _redact(text: str) -> str:
    """`az` stderr can name the operator, their object id and the subscription."""
    return _GUID.sub("<id>", _EMAIL.sub("<user>", text))


def scrub(events: list[dict]) -> list[dict]:
    """Blank the identifiers the events carry that the repo does not: the subscription id, and
    anything identifying inside a lane's failure reason."""
    out = []
    for e in events:
        if e.get("kind") == "azure_context":
            e = {**e, "sub": ""}
        elif e.get("kind") == "lane_unavailable":
            e = {**e, "reason": _redact(str(e.get("reason", "")))}
        out.append(e)
    return out


def _hms(ms: float | None) -> str:
    if ms is None:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%H:%M:%SZ")


def numbers(events: list[dict]) -> dict:
    start = next(e for e in events if e.get("lane") == "control" and e.get("kind") == "start")
    project = start.get("project", "solventdev")
    phases: dict[str, dict] = {}
    for e in events:
        if e.get("lane") == "phase" and e.get("kind") == "start":
            phases[e["key"]] = {"key": e["key"], "title": e["title"], "start": e["t"], "end": None}
        elif e.get("lane") == "phase" and e.get("kind") == "end" and e["key"] in phases:
            phases[e["key"]]["end"] = e["t"]
    client = [e for e in events if e.get("lane") == "client" and not e.get("warmup")]
    latest: dict[str, dict] = {}
    for e in events:
        if e.get("lane") == "alert":
            latest[e["rule"]] = e
    causes = {f"{project}-denied-writes": phases.get("runaway", {}).get("start"),
              f"{project}-latency-slo": phases.get("slow", {}).get("start")}
    alerts = []
    for rule, cause in causes.items():
        a = latest.get(rule)
        fired = derive.iso_ms(a["fired"]) if a and a.get("fired") else None
        resolved = derive.iso_ms(a["resolved"]) if a and a.get("resolved") else None
        alerts.append({"rule": rule, "cause": cause, "fired": fired, "resolved": resolved,
                       "lag_s": round((fired - cause) / 1000) if fired and cause else None})
    end_t = events[-1]["t"]
    totals = derive.kpis(events, end_t)
    recon = next((e["rows"] for e in reversed(events) if e.get("lane") == "recon"), {})
    return {
        "start": start["t"], "end": end_t, "profile": start.get("profile"),
        "phases": list(phases.values()), "alerts": alerts,
        "denied_runaway": sum(
            1 for e in client if e["agent"] == "runaway" and e["outcome"] == "denied"),
        "slow_calls": sum(
            1 for e in client if e["outcome"] == "allowed" and e.get("fault_asked_ms")),
        "minutes_over_slo": derive.minutes_over_slo(events),
        "allowed_total": totals["allowed_total"], "slow_total": totals["slow_total"],
        "budget_consumed_pct": totals["budget_consumed_pct"], "recon": recon,
    }


def render_postmortem(n: dict) -> str:
    rows = "\n".join(
        f"| {_hms(p['start'])} | {p['title']} | {_hms(p['end'])} |" for p in n["phases"])
    alerts = "\n".join(
        f"| `{a['rule']}` | {_hms(a['cause'])} | {_hms(a['fired'])} | {_hms(a['resolved'])} | "
        f"{'n/a' if a['lag_s'] is None else str(a['lag_s']) + ' s'} |" for a in n["alerts"])
    recon = "; ".join(f"{d}: {r['client']} sent, {r['server']} logged, {r['azure']} ingested "
                      f"({r['status']})" for d, r in n["recon"].items()) or "n/a"
    return f"""# Postmortem: mock incident on solvent dev

Run of {_hms(n['start'])} to {_hms(n['end'])} ({n['profile']} profile). Blameless format. Two
injected incidents: a runaway agent retrying a write without approval, and a slow upstream
dependency. Numbers below are computed from the captured events, not typed.

## Timeline (UTC)

| Phase start | Phase | Phase end |
|---|---|---|
{rows}

| Alert | Cause began | Fired | Resolved | Cause to fire |
|---|---|---|---|---|
{alerts}

## Impact

- Runaway agent: {n['denied_runaway']} refused restart attempts, none executed (the gate held).
- Slow dependency: {n['slow_calls']} calls carried the injected delay; {n['minutes_over_slo']}
  minute(s) had a client p95 over the 2 s objective.
- Error budget (99% of allowed calls within 2 s, client measured): {n['budget_consumed_pct']}%
  consumed ({n['slow_total']} of {n['allowed_total']} allowed calls over 2 s).
- Audit completeness at the end of the run: {recon}.

## Root cause

{MARK}

## What went well

{MARK}

## Action items

{MARK}
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m demo.report")
    ap.add_argument("events", type=Path)
    ap.add_argument("--force", action="store_true", help="overwrite an existing postmortem.md")
    args = ap.parse_args(argv)
    events = read_events(args.events)
    DOCS.mkdir(parents=True, exist_ok=True)
    html = live.inline_events(live.load_html(), scrub(events)).replace("__TOKEN__", "")
    (DOCS / "report.html").write_text(html, encoding="utf-8")
    print(f"wrote {DOCS / 'report.html'}")
    pm = DOCS / "postmortem.md"
    if pm.exists() and not args.force:
        print(f"{pm} exists and may hold hand-written sections; pass --force to overwrite")
        return 1
    pm.write_text(render_postmortem(numbers(events)), encoding="utf-8")
    print(f"wrote {pm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
