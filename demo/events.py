"""One in-process event stream. Every lane publishes here; the terminal, the file and the page
subscribe. Events are plain dicts: {"t": epoch_ms, "lane": "client|server|azure|alert|phase|...", ...}.
"""

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

Event = dict[str, Any]


class EventBus:
    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time):
        self.buffer: list[Event] = []
        self._subs: set[asyncio.Queue[Event | None]] = set()
        self._clock = clock
        self._closed = False
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8")  # one file per run, named by the caller

    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    def publish(self, ev: Event) -> Event:
        if self._closed:
            return ev
        ev.setdefault("t", self.now_ms())
        self.buffer.append(ev)
        if self._fh:
            self._fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
            self._fh.flush()
        for q in self._subs:
            q.put_nowait(ev)
        return ev

    def subscribe(self) -> "asyncio.Queue[Event | None]":
        """A queue that yields everything published so far, then live events; None ends it."""
        q: asyncio.Queue[Event | None] = asyncio.Queue()
        for ev in self.buffer:
            q.put_nowait(ev)
        if self._closed:
            q.put_nowait(None)
        else:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[Event | None]") -> None:
        self._subs.discard(q)

    def close(self) -> None:
        self._closed = True
        for q in self._subs:
            q.put_nowait(None)
        self._subs.clear()
        if self._fh:
            self._fh.close()
            self._fh = None


def read_events(path: Path) -> list[Event]:
    """Every whole line of a capture. A run killed mid-write leaves a truncated last line;
    skip it rather than lose the capture."""
    events: list[Event] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events
