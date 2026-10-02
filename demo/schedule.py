"""The scenario as data: phases, agent cadences, and the denial budget the alerts impose."""

import math
from dataclasses import dataclass

MONITOR_EVERY_S = 5  # get_resource_health, allowed
ANALYST_EVERY_S = 180  # get_cost_summary, refused: classification_gated
SCANNER_EVERY_S = 300  # unauthenticated /mcp, 401
RUNAWAY_PER_S = 2  # restart_container_app with no token, incident 1 only

DENIED_ALERT_THRESHOLD = 5  # `denied-writes` fires above this many denials ...
DENIED_ALERT_WINDOW_S = 300  # ... in this window
SLO_MS = 2000


@dataclass(frozen=True)
class Phase:
    key: str
    title: str
    minutes: float
    runaway_s: int = 0  # seconds of runaway-agent traffic at the start of the phase
    fault_ms: int = 0  # latency the monitor agent asks for while the phase runs


PROFILES: dict[str, tuple[Phase, ...]] = {
    "full": (
        Phase("baseline", "Baseline", 10),
        Phase("runaway", "Runaway agent", 10, runaway_s=180),
        Phase("quiet", "Quiet", 5),
        Phase("slow", "Slow dependency", 8, fault_ms=3000),
        Phase("recovery", "Recovery", 17),
    ),
    "live": (
        Phase("baseline", "Baseline", 3),
        Phase("runaway", "Runaway agent", 4, runaway_s=120),
        Phase("quiet", "Quiet", 1),
        Phase("slow", "Slow dependency", 4, fault_ms=3000),
        Phase("recovery", "Recovery", 3),
    ),
}


def baseline_denials(window_s: int = DENIED_ALERT_WINDOW_S) -> int:
    """Worst case denials any window this long can hold from the baseline agents alone."""
    return sum(math.ceil(window_s / p) for p in (ANALYST_EVERY_S, SCANNER_EVERY_S))


def total_minutes(profile: str) -> float:
    return sum(p.minutes for p in PROFILES[profile])
