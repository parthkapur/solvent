"""The controls layer. Read tools run freely; write tools need a human-minted token."""

import functools
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml
from opentelemetry import metrics, trace

POLICY_PATH = Path(__file__).with_name("policy.yaml")
_audit = logging.getLogger("solvent.audit")
_meter = metrics.get_meter("solvent")
_calls = _meter.create_counter("mcp.tool.calls", description="tool calls by decision")
_latency = _meter.create_histogram("mcp.tool.latency_ms", unit="ms")


def load_policy(path: Path = POLICY_PATH) -> dict[str, str]:
    return yaml.safe_load(path.read_text())["tools"]


POLICY = load_policy()


def canonical(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def mint_token(tool: str, args: dict[str, Any], secret: str) -> str:
    msg = f"{tool}:{canonical(args)}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def decide(
    tool: str, args: dict[str, Any], policy: dict[str, str], secret: str
) -> tuple[str, str]:
    cls = policy.get(tool)
    if cls is None:
        return "denied", "not_in_policy"
    if cls == "read":
        return "allowed", "read"
    if not secret:
        return "denied", "no_approval_secret"
    token = args.get("approval_token", "")
    expected = mint_token(tool, _without_token(args), secret)
    if token and hmac.compare_digest(token, expected):
        return "allowed", "approved"
    return "denied", "approval_required"


def _without_token(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k != "approval_token"}


def governed(fn):
    tool = fn.__name__

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> dict[str, Any]:
        start = time.perf_counter()
        decision, reason = decide(tool, kwargs, POLICY, os.environ.get("APPROVAL_SECRET", ""))
        try:
            if decision != "allowed":
                return {"denied": True, "reason": reason}
            try:
                return fn(**kwargs)
            except Exception as e:  # noqa: BLE001 - surface the cause as data, not a bare "tool error"
                reason = f"error:{type(e).__name__}"
                return {"error": str(e).splitlines()[0][:300]}
        finally:
            _record(tool, kwargs, decision, reason, (time.perf_counter() - start) * 1000)

    return wrapper


def _record(
    tool: str, args: dict[str, Any], decision: str, reason: str, latency_ms: float
) -> None:
    ctx = trace.get_current_span().get_span_context()
    event = {
        "ts": time.time(),
        "tool": tool,
        "args_hash": hashlib.sha256(canonical(_without_token(args)).encode()).hexdigest()[:16],
        "decision": decision,
        "reason": reason,
        "latency_ms": round(latency_ms, 2),
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }
    _audit.info(json.dumps(event), extra=event)
    _calls.add(1, {"tool": tool, "decision": decision})
    _latency.record(latency_ms, {"tool": tool})
