"""Dev-only latency injection for the demo harness.

Off unless FAULT_INJECTION=1. An authenticated caller may then send
`X-Solvent-Fault: latency_ms=N` and every allowed tool call in that request sleeps N ms
before it runs. The sleep is inside the timed region, so it shows up in mcp.tool.latency_ms,
and the audit line says `fault_ms` so an injected delay is never mistaken for a real one.
"""

import os
import re
from contextvars import ContextVar

HEADER = b"x-solvent-fault"
MAX_MS = 10_000

# Set by the ASGI guard once the caller is authenticated, read by `governed`. A ContextVar for
# the same reason as policy.CALLER: tool functions know nothing about transport.
FAULT_MS: ContextVar[int] = ContextVar("fault_ms", default=0)

_LATENCY = re.compile(rb"latency_ms=(\d{1,6})")


def from_header(value: bytes) -> int:
    """Milliseconds the header asks for; 0 when the knob is off or the value is absent or odd."""
    if os.environ.get("FAULT_INJECTION") != "1":
        return 0
    m = _LATENCY.fullmatch(value.strip())
    return min(int(m.group(1)), MAX_MS) if m else 0
