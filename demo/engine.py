"""The run: phase clock, simulated agents, pause / step / fault controls, and the Azure lanes."""

import asyncio
import math
from contextlib import suppress
from dataclasses import dataclass

import httpx

from demo import agents, derive, lanes, schedule
from demo.events import EventBus

MAX_FAULT_MS = 10_000  # mirrors app.faults.MAX_MS; the server clamps too
TICK_S = 0.25  # scenario seconds per phase-clock step
DERIVE_EVERY_S = 5
TAIL_MAX_S = 30 * 60  # how long to keep watching Azure after traffic stops


@dataclass
class Config:
    url: str
    api_key: str
    project: str = "solventdev"
    profile: str = "full"
    step: bool = False
    speed: float = 1.0  # compresses phase lengths and cadences only; Azure's lag is real
    lanes: bool = True  # start the Azure lanes; tests turn this off


class Engine:
    def __init__(self, cfg: Config, bus: EventBus, *, transport=None, phases=None):
        self.cfg, self.bus = cfg, bus
        self.phases = phases or schedule.PROFILES[cfg.profile]
        self.rg, self.app = f"{cfg.project}-rg", f"{cfg.project}-app"
        self._transport = transport
        self.caller: agents.Caller | None = None
        self.phase: schedule.Phase | None = None
        self._clock = 0.0  # scenario seconds into the current phase
        self._running = asyncio.Event()  # cleared while paused
        self._running.set()
        self._next = asyncio.Event()
        self._stop = asyncio.Event()
        self._traffic = asyncio.Event()  # set when the agents should wind down
        self.finished = asyncio.Event()
        self.manual_fault_ms = 0
        self.started = False
        self.task: asyncio.Task | None = None

    # ---- controls -------------------------------------------------------------------------

    def start(self) -> asyncio.Task:
        self.started = True
        self.task = asyncio.create_task(self.run())
        return self.task

    def handle(self, action: str, **kw) -> dict:
        extra: dict = {}
        if action == "start":
            if self.started:
                return {"ok": False, "error": "already started"}
            self.start()
            return {"ok": True}
        if action == "pause":
            self._running.clear()
        elif action == "resume":
            self._running.set()
        elif action == "next":
            self._next.set()
        elif action == "stop":
            self._stop.set()
            self._traffic.set()
            self._running.set()  # wake anything parked on a pause
        elif action == "fault":
            raw = float(kw.get("ms", 0))
            if not math.isfinite(raw):
                raise ValueError("ms must be a finite number")
            self.manual_fault_ms = max(0, min(int(raw), MAX_FAULT_MS))
            extra = {"ms": self.manual_fault_ms}
        else:
            return {"ok": False, "error": f"unknown action {action}"}
        self.bus.publish({"lane": "control", "kind": action, **extra})
        return {"ok": True}

    # ---- the run --------------------------------------------------------------------------

    async def run(self) -> None:
        self.started = True
        cfg = self.cfg
        self.bus.publish({
            "lane": "control", "kind": "start", "profile": cfg.profile, "step": cfg.step,
            "speed": cfg.speed, "project": cfg.project,
            "phases": [{"key": p.key, "title": p.title, "minutes": p.minutes}
                       for p in self.phases],
        })
        background: list[asyncio.Task] = []
        try:
            async with httpx.AsyncClient(
                base_url=cfg.url, transport=self._transport, timeout=30.0
            ) as client:
                self.caller = agents.Caller(client, cfg.api_key)
                await self._warmup()
                background.append(asyncio.create_task(self._derive()))
                if cfg.lanes:
                    background.append(asyncio.create_task(
                        lanes.stream_server_log(self.bus, self.app, self.rg)))
                    background.append(asyncio.create_task(
                        lanes.poll_azure(self.bus, cfg.project)))
                workers = [asyncio.create_task(self._agent(every, fn))
                           for every, fn in self._agents()]
                try:
                    for i, ph in enumerate(self.phases):
                        if self._stop.is_set():
                            break
                        await self._run_phase(ph, i)
                finally:
                    self._traffic.set()
                    self.phase = None
                    await asyncio.gather(*workers, return_exceptions=True)
                self.bus.publish({"lane": "phase", "kind": "traffic_done"})
                if cfg.lanes:
                    await self._tail()
        finally:
            for t in background:
                t.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            self._publish_derived()
            self.bus.publish({"lane": "control", "kind": "end"})
            self.finished.set()

    async def _warmup(self) -> None:
        """The dev app scales to zero: the first call pays the cold start and is not measured."""
        seen = await self.caller.call("get_resource_health", {"resource_group": self.rg})
        self._emit("monitor", "get_resource_health", seen, 0, warmup=True)

    async def _run_phase(self, ph: schedule.Phase, index: int) -> None:
        self.phase, self._clock = ph, 0.0
        self.bus.publish({"lane": "phase", "kind": "start", "key": ph.key, "title": ph.title,
                          "index": index, "of": len(self.phases), "minutes": ph.minutes})
        budget = ph.minutes * 60
        while not self._stop.is_set():
            if self._next.is_set():
                self._next.clear()
                break
            if not self.cfg.step and self._clock >= budget:
                break
            await self._running.wait()
            await asyncio.sleep(TICK_S / self.cfg.speed)
            if self._running.is_set():
                self._clock += TICK_S
        self.bus.publish({"lane": "phase", "kind": "end", "key": ph.key})

    async def _tail(self) -> None:
        """Traffic is over; keep watching until both alerts have resolved (or the user stops)."""
        waited = 0.0
        while not self._stop.is_set() and waited < TAIL_MAX_S and not self._alerts_settled():
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), DERIVE_EVERY_S)
            waited += DERIVE_EVERY_S

    def _alerts_settled(self) -> bool:
        latest: dict[str, str] = {}
        for e in self.bus.buffer:
            if e.get("lane") == "alert":
                latest[e["rule"]] = e["state"]
        rules = (f"{self.cfg.project}-denied-writes", f"{self.cfg.project}-latency-slo")
        return all(latest.get(r) == "Resolved" for r in rules)

    # ---- agents ---------------------------------------------------------------------------

    def _agents(self):
        return [
            (schedule.MONITOR_EVERY_S, self._monitor),
            (schedule.ANALYST_EVERY_S, self._analyst),
            (schedule.SCANNER_EVERY_S, self._scanner),
            (1 / schedule.RUNAWAY_PER_S, self._runaway),
        ]

    async def _sleep(self, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._traffic.wait(), seconds / self.cfg.speed)

    async def _agent(self, every_s: float, fn) -> None:
        while not self._traffic.is_set():
            await self._running.wait()
            if self._traffic.is_set():
                break
            await fn()
            await self._sleep(every_s)

    def _fault(self) -> int:
        return self.manual_fault_ms or (self.phase.fault_ms if self.phase else 0)

    async def _monitor(self) -> None:
        fault = self._fault()
        seen = await self.caller.call("get_resource_health", {"resource_group": self.rg},
                                      fault_ms=fault)
        self._emit("monitor", "get_resource_health", seen, fault)

    async def _analyst(self) -> None:
        seen = await self.caller.call("get_cost_summary", {"days": 3})
        self._emit("analyst", "get_cost_summary", seen, 0)

    async def _scanner(self) -> None:
        seen = await self.caller.call("get_resource_health", {"resource_group": self.rg},
                                      auth=False)
        self._emit("scanner", "mcp", seen, 0)

    async def _runaway(self) -> None:
        ph = self.phase
        if ph and self._clock < ph.runaway_s:
            seen = await self.caller.call("restart_container_app", {"name": self.app})
            self._emit("runaway", "restart_container_app", seen, 0)

    def _emit(self, agent: str, tool: str, seen: agents.Seen, fault_asked: int,
              warmup: bool = False) -> None:
        ev = {"lane": "client", "agent": agent, "tool": tool, "outcome": seen.outcome,
              "reason": seen.reason, "status": seen.status, "rtt_ms": round(seen.rtt_ms, 1),
              "fault_asked_ms": fault_asked}
        if warmup:
            ev["warmup"] = True
        self.bus.publish(ev)

    # ---- derived numbers ------------------------------------------------------------------

    def _publish_derived(self) -> None:
        events = self.bus.buffer
        self.bus.publish({"lane": "kpi", **derive.kpis(events, self.bus.now_ms())})
        self.bus.publish({"lane": "recon",
                          "rows": derive.reconcile(events, derive.settled(events))})

    async def _derive(self) -> None:
        while True:
            await asyncio.sleep(DERIVE_EVERY_S / self.cfg.speed)
            self._publish_derived()
