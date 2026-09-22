"""The controls layer. Read tools run freely; write tools need a human-minted token."""

import functools
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

import yaml
from opentelemetry import metrics, trace

POLICY_PATH = Path(__file__).with_name("policy.yaml")
_audit = logging.getLogger("solvent.audit")
_meter = metrics.get_meter("solvent")
_calls = _meter.create_counter("mcp.tool.calls", description="tool calls by decision")
_latency = _meter.create_histogram("mcp.tool.latency_ms", unit="ms")



class Policy(NamedTuple):
    """The three things that always travel together: what is gated, how long, and by whom."""

    tools: dict[str, str]
    ttl_s: Mapping[str, int] = MappingProxyType({})
    approvers: tuple[str, ...] = ()

    def ttl(self, tool: str) -> int:
        return self.ttl_s.get(tool, TOKEN_TTL_S)

    @property
    def max_ttl(self) -> int:
        """The longest window any tool allows - how far back replay bookkeeping must remember."""
        return max([TOKEN_TTL_S, *self.ttl_s.values()])


def load_policy(path: Path = POLICY_PATH) -> Policy:
    raw = yaml.safe_load(path.read_text())
    # APPROVERS is set per environment by Terraform; the file is the default.
    env = [a.strip() for a in os.environ.get("APPROVERS", "").split(",") if a.strip()]
    approvers = tuple(env) or tuple(raw.get("approvers") or ())
    return Policy(raw["tools"], raw.get("ttl_s") or {}, approvers)


POLICY = load_policy()


def canonical(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


TOKEN_TTL_S = 600  # an approval is for "now", not forever


def mint_token(
    tool: str, args: dict[str, Any], secret: str, approver: str, now: float | None = None
) -> str:
    """Return "<hmac>.<unix_ts>.<approver>".

    The timestamp and the approver are both inside the signed message, so neither can be
    moved nor relabelled. The approver goes last so an identity containing dots survives
    the split.
    """
    ts = int(now if now is not None else time.time())
    msg = f"{tool}:{canonical(args)}:{approver}:{ts}".encode()
    return f"{hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()}.{ts}.{approver}"


def decide(
    tool: str,
    args: dict[str, Any],
    policy: Policy,
    secret: str,
    now: float | None = None,
) -> tuple[str, str, str]:
    """(decision, reason, approved_by). approved_by is "" until a signature has verified."""
    cls = policy.tools.get(tool)
    if cls is None:
        return "denied", "not_in_policy", ""
    if cls == "read":
        return "allowed", "read", ""
    if not secret:
        return "denied", "no_approval_secret", ""
    sig, _, rest = str(args.get("approval_token", "")).partition(".")
    ts, _, approver = rest.partition(".")
    if not ts.isdigit() or not approver:
        return "denied", "approval_required", ""
    expected = mint_token(tool, _without_token(args), secret, approver, now=int(ts))
    if not hmac.compare_digest(sig, expected.partition(".")[0]):
        return "denied", "approval_required", ""
    # Past this line the identity is signed, so it is safe to name it in a denial.
    if policy.approvers and approver not in policy.approvers:
        return "denied", "approver_not_authorized", approver
    if (now if now is not None else time.time()) - int(ts) > policy.ttl(tool):
        return "denied", "approval_expired", approver
    return "allowed", "approved", approver


def _without_token(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k != "approval_token"}


def governed(fn):
    tool = fn.__name__

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> dict[str, Any]:
        start = time.perf_counter()
        decision, reason, approved_by = decide(
            tool, kwargs, POLICY, os.environ.get("APPROVAL_SECRET", "")
        )
        try:
            if decision != "allowed":
                return {"denied": True, "reason": reason}
            try:
                return fn(**kwargs)
            except Exception as e:  # noqa: BLE001 - surface the cause as data, not a bare "tool error"
                reason = f"error:{type(e).__name__}"
                return {"error": str(e).splitlines()[0][:300]}
        finally:
            _record(
                tool, kwargs, decision, reason, (time.perf_counter() - start) * 1000, approved_by
            )

    return wrapper


def audit_event(**fields: Any) -> None:
    """One audit line, trace-correlated. Used for calls that never reached a tool too."""
    ctx = trace.get_current_span().get_span_context()
    event = {
        "ts": time.time(),
        **fields,
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }
    _audit.info(json.dumps(event), extra=event)


def _record(
    tool: str,
    args: dict[str, Any],
    decision: str,
    reason: str,
    latency_ms: float,
    approved_by: str = "",
) -> None:
    event = {
        "tool": tool,
        "args_hash": hashlib.sha256(canonical(_without_token(args)).encode()).hexdigest()[:16],
        "decision": decision,
        "reason": reason,
        "latency_ms": round(latency_ms, 2),
    }
    if approved_by:
        event["approved_by"] = approved_by
    audit_event(**event)
    _calls.add(1, {"tool": tool, "decision": decision})
    _latency.record(latency_ms, {"tool": tool})
