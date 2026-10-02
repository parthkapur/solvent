"""The controls layer. Reads run freely unless gated; writes need a human-minted token."""

import functools
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

import yaml
from opentelemetry import metrics, trace

from app import entra, faults, nonces

POLICY_PATH = Path(__file__).with_name("policy.yaml")

# An HMAC is always 64 hex digits; a JWS header, base64url of `{"alg":...}`, never starts
# with 64 hex-only characters. That shape - not the first ~ - is what tells a bare token
# apart from a proven one, because an approver identity may itself contain a ~.
_SIGNED = re.compile(r"[0-9a-f]{64}\.")
_audit = logging.getLogger("solvent.audit")
_meter = metrics.get_meter("solvent")
_calls = _meter.create_counter("mcp.tool.calls", description="tool calls by decision")
_latency = _meter.create_histogram("mcp.tool.latency_ms", unit="ms")

# Set by the ASGI guard, read by every audit line in this request's context. A ContextVar rather
# than a parameter because `governed` wraps tool functions that know nothing about transport.
CALLER: ContextVar[str] = ContextVar("caller", default="")


class Policy(NamedTuple):
    """The five things that always travel together: what is gated, how long, by whom, and why."""

    tools: dict[str, str]
    ttl_s: Mapping[str, int] = MappingProxyType({})
    approvers: tuple[str, ...] = ()
    classification: Mapping[str, str] = MappingProxyType({})
    gated: tuple[str, ...] = ()

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
    return Policy(
        raw["tools"],
        raw.get("ttl_s") or {},
        approvers,
        raw.get("classification") or {},
        tuple(raw.get("gated") or ()),
    )


POLICY = load_policy()


def canonical(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


TOKEN_TTL_S = 600  # an approval is for "now", not forever
CLOCK_SKEW_S = 60  # how far ahead of the server's clock an approver's clock may run


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
    consume: Callable[[str, int, float, int], bool] | None = None,
) -> tuple[str, str, str]:
    """(decision, reason, approved_by). approved_by is "" until a signature has verified."""
    cls = policy.tools.get(tool)
    if cls is None:
        return "denied", "not_in_policy", ""
    # A read of gated data is not a free read: blast radius is not the only thing worth a human.
    gated = policy.classification.get(tool) in policy.gated
    if cls == "read" and not gated:
        return "allowed", "read", ""
    missing = "classification_gated" if cls == "read" else "approval_required"
    if not secret:
        return "denied", "no_approval_secret", ""
    # <jwt>~<hmac>.<ts>.<approver>, or the bare <hmac>.<ts>.<approver> from before proofs
    # existed. _SIGNED, not the first ~, tells them apart: an approver identity may itself
    # contain a ~, and then partitioning on it would split inside the identity rather than
    # at the proof boundary.
    raw = str(args.get("approval_token", ""))
    if _SIGNED.match(raw):
        proof, signed = "", raw
    else:
        proof, _, signed = raw.partition("~")
    sig, _, rest = signed.partition(".")
    ts, _, approver = rest.partition(".")
    # isdecimal(), not isdigit(): isdigit() is also true of superscripts like "²", which
    # int() then rejects with ValueError - outside governed's try/finally, so that exception
    # would skip _record and reach the caller as an unaudited 500. isdecimal() is exactly the
    # set int() accepts.
    if not ts.isdecimal() or not approver:
        return "denied", missing, ""
    expected = mint_token(tool, _without_token(args), secret, approver, now=int(ts))
    # compare_digest refuses non-ASCII str, and an attacker picks the token; compare bytes.
    if not hmac.compare_digest(sig.encode(), expected.partition(".")[0].encode()):
        return "denied", missing, ""
    # Past this line the identity is signed, so it is safe to name it in a denial.
    if entra.configured():
        claims = entra.verified_claims(proof)
        if claims is None:
            return "denied", "approver_unproven", approver
        if approver not in entra.approver_names(claims):
            return "denied", "approver_mismatch", approver
    if policy.approvers and approver not in policy.approvers:
        return "denied", "approver_not_authorized", approver
    age = (now if now is not None else time.time()) - int(ts)
    if age > policy.ttl(tool):
        return "denied", "approval_expired", approver
    # A future timestamp makes age negative, which the check above never catches: a token
    # dated a year ahead would stay good for a year. Allow a little clock skew, nothing more.
    if age < -CLOCK_SKEW_S:
        return "denied", "approval_not_yet_valid", approver
    if consume is not None:
        # This runs before governed's try/finally, so a bare exception here would skip
        # _record entirely - the one path this project exists to log. Deny and audit instead.
        try:
            first_use = consume(
                sig, int(ts), now if now is not None else time.time(), policy.max_ttl
            )
        except Exception:  # noqa: BLE001 - any ledger failure is the same answer: deny
            return "denied", "ledger_unavailable", approver
        if not first_use:
            return "denied", "approval_replayed", approver
    return "allowed", "approved", approver


def _without_token(args: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k != "approval_token"}


def governed(fn):
    tool = fn.__name__

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> dict[str, Any]:
        start = time.perf_counter()
        decision, reason, approved_by = decide(
            tool, kwargs, POLICY, os.environ.get("APPROVAL_SECRET", ""), consume=nonces.consume
        )
        gate_ms = (time.perf_counter() - start) * 1000
        fault_ms = 0
        try:
            if decision != "allowed":
                return {"denied": True, "reason": reason}
            try:
                fault_ms = faults.FAULT_MS.get()
                if fault_ms:
                    time.sleep(fault_ms / 1000)  # demo fault; inside the timed region on purpose
                return fn(**kwargs)
            except Exception as e:  # noqa: BLE001 - surface the cause as data, not a bare "tool error"
                reason = f"error:{type(e).__name__}"
                return {"error": str(e).splitlines()[0][:300]}
        finally:
            _record(
                tool,
                kwargs,
                decision,
                reason,
                (time.perf_counter() - start) * 1000,
                approved_by,
                gate_ms,
                fault_ms,
            )

    return wrapper


def audit_event(**fields: Any) -> None:
    """One audit line, trace-correlated. Used for calls that never reached a tool too."""
    ctx = trace.get_current_span().get_span_context()
    event = {
        "ts": time.time(),
        "caller": CALLER.get(),
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
    gate_ms: float = 0.0,
    fault_ms: int = 0,
) -> None:
    event = {
        "tool": tool,
        "args_hash": hashlib.sha256(canonical(_without_token(args)).encode()).hexdigest()[:16],
        "decision": decision,
        "reason": reason,
        "latency_ms": round(latency_ms, 2),
        # Time spent deciding. gate_ms flat while latency_ms climbs means the delay is upstream
        # of the policy layer.
        "gate_ms": round(gate_ms, 2),
    }
    if approved_by:
        event["approved_by"] = approved_by
    if fault_ms:
        event["fault_ms"] = fault_ms  # an injected delay, never silent (see app/faults.py)
    audit_event(**event)
    _calls.add(1, {"tool": tool, "decision": decision})
    _latency.record(latency_ms, {"tool": tool})
