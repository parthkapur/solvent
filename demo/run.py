"""Drive the mock incident against dev and watch it happen.

  python -m demo.run --dry-run                                    # print the schedule, touch nothing
  SOLVENT_URL=... SOLVENT_API_KEY=... python -m demo.run --ui     # mission-control page
  python -m demo.run --replay demo/out/events-....jsonl           # replay a capture, no traffic
"""

import argparse
import asyncio
import os
import secrets
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from demo import live, schedule
from demo.engine import Config, Engine
from demo.events import EventBus, read_events

OUT = Path(__file__).with_name("out")
DEV_SUFFIX = ".azurecontainerapps.io"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_COLOURS = {"good": "32", "warn": "33", "fault": "35", "bad": "31", "dim": "2"}


def check_target(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    # https only for anything off this machine: the bearer key must not cross the network in clear
    dev = host.startswith("solventdev-app.") and host.endswith(DEV_SUFFIX)
    if not ((dev and parsed.scheme == "https") or host in LOCAL_HOSTS):
        raise SystemExit(
            f"refusing {url!r}: the demo only targets the dev app "
            f"(https://solventdev-app.*{DEV_SUFFIX}) or localhost"
        )


def _paint(text: str, key: str) -> str:
    return text if os.environ.get("NO_COLOR") else f"\x1b[{_COLOURS[key]}m{text}\x1b[0m"


def format_line(ev: dict, t0: int) -> str | None:
    lane, clock = ev.get("lane"), f"T+{(ev['t'] - t0) / 1000:7.1f}s"
    if lane == "phase" and ev.get("kind") == "start":
        return f"{clock}  == phase {ev['index'] + 1}/{ev['of']}: {ev['title']}"
    if lane == "phase" and ev.get("kind") == "traffic_done":
        return f"{clock}  == traffic finished; still watching Azure"
    if lane == "client":
        colour = {"allowed": "good", "denied": "warn"}.get(ev["outcome"], "bad")
        asked = ev.get("fault_asked_ms")
        fault = _paint(f" +{asked}ms fault", "fault") if asked else ""
        return (f"{clock}  agent   {_paint(ev['outcome'].ljust(7), colour)} "
                f"{ev['tool']:<22} {ev['reason']:<22} {ev['rtt_ms']:>6.0f}ms{fault}")
    if lane == "server":
        fault = _paint(f" fault {ev['fault_ms']}ms", "fault") if ev.get("fault_ms") else ""
        gate = f" gate {ev['gate_ms']}ms" if ev.get("gate_ms") is not None else ""
        return (f"{clock}  server  {_paint(str(ev['decision']).ljust(7), 'dim')} "
                f"{ev['tool']!s:<22} {ev['reason']!s:<22} {ev['latency_ms']}ms{gate}{fault}")
    if lane == "azure" and ev.get("kind") == "series":
        return f"{clock}  azure   ingested through {ev.get('data_until')} ({len(ev['rows'])} rows)"
    if lane == "alert":
        colour = "bad" if ev["state"] == "Fired" else "good"
        return f"{clock}  ALERT   {ev['rule']} {_paint(ev['state'], colour)}"
    if lane == "sys" and ev.get("kind") == "lane_unavailable":
        return f"{clock}  !! lane {ev['name']} unavailable: {ev['reason']}"
    return None


async def print_events(bus: EventBus) -> None:
    q, t0 = bus.subscribe(), None
    while (ev := await q.get()) is not None:
        t0 = t0 or ev["t"]
        if line := format_line(ev, t0):
            print(line, flush=True)


def dry_run(profile: str) -> None:
    clock = 0.0
    for p in schedule.PROFILES[profile]:
        extra = f", runaway agent for {p.runaway_s}s" if p.runaway_s else ""
        extra += f", latency +{p.fault_ms}ms" if p.fault_ms else ""
        print(f"{clock:5.1f}-{clock + p.minutes:5.1f} min  {p.title}{extra}")
        clock += p.minutes
    worst = schedule.baseline_denials()
    print(f"baseline denials in any {schedule.DENIED_ALERT_WINDOW_S}s: at most {worst} "
          f"(the alert fires above {schedule.DENIED_ALERT_THRESHOLD})")
    assert worst <= 3, "the baseline would false-alarm the denial alert"


async def amain(args: argparse.Namespace) -> None:
    if args.replay:
        events = read_events(Path(args.replay))
        html = live.inline_events(live.load_html(), events).replace("__TOKEN__", "")
        print(f"replaying {len(events)} events at http://127.0.0.1:{args.port}/  (Ctrl-C to quit)")
        webbrowser.open(f"http://127.0.0.1:{args.port}/")
        await live.serve_static(html, args.port)
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT / f"events-{stamp}.jsonl"
    bus = EventBus(path)
    cfg = Config(url=os.environ["SOLVENT_URL"], api_key=os.environ["SOLVENT_API_KEY"],
                 project=os.environ.get("SOLVENT_PROJECT", "solventdev"), profile=args.profile,
                 step=args.step, speed=args.speed, lanes=not args.no_azure)
    engine = Engine(cfg, bus)
    printer = asyncio.create_task(print_events(bus))
    print(f"capturing to {path}")
    try:
        if args.ui:
            token = secrets.token_urlsafe(16)
            app = live.make_app(bus, engine, live.load_html(), token)
            server = asyncio.create_task(live.serve(app, args.port))
            await asyncio.sleep(0.5)
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
            print(f"page at http://127.0.0.1:{args.port}/ : press Start. Ctrl-C to quit.")
            await engine.finished.wait()
            print("run finished; the page stays up. Ctrl-C to quit.")
            await server
        else:
            await engine.run()
    finally:
        bus.close()
        await printer
        print(f"capture: {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m demo.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=sorted(schedule.PROFILES), default="live")
    ap.add_argument("--step", action="store_true", help="hold each phase until Next")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="compress phase lengths and cadences (Azure's lag stays real)")
    ap.add_argument("--ui", action="store_true", help="serve the page and open the browser")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-azure", action="store_true", help="skip the Azure lanes")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replay", metavar="FILE")
    args = ap.parse_args(argv)
    if args.step and not (args.ui or args.dry_run or args.replay):
        raise SystemExit("--step needs --ui: the Next phase button lives on the page")
    if args.dry_run:
        dry_run(args.profile)
        return 0
    if args.replay:
        asyncio.run(amain(args))
        return 0
    for var in ("SOLVENT_URL", "SOLVENT_API_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"set {var} (see the demo section of the README)")
    check_target(os.environ["SOLVENT_URL"])
    if args.speed != 1.0 and not args.no_azure:
        print("note: --speed compresses the scenario, not Azure's ingestion lag")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
