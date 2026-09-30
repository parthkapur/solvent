import inspect
import json
import logging

from app import nonces, policy

POLICY = policy.Policy({"read_tool": "read", "write_tool": "write"})
SECRET = "s3cret"
APPROVER = "ops@example.com"
GATED = policy.Policy(
    {"get_cost_summary": "read", "read_tool": "read", "restart_container_app": "write"},
    classification={"get_cost_summary": "financial"},
    gated=("financial",),
)


def test_read_allowed():
    assert policy.decide("read_tool", {}, POLICY, SECRET) == ("allowed", "read", "")


def test_a_gated_read_needs_an_approval():
    decision, reason, _ = policy.decide("get_cost_summary", {}, GATED, SECRET)
    assert (decision, reason) == ("denied", "classification_gated")


def test_a_gated_read_runs_with_an_approval():
    tok = policy.mint_token("get_cost_summary", {}, SECRET, APPROVER)
    decision, reason, who = policy.decide(
        "get_cost_summary", {"approval_token": tok}, GATED, SECRET
    )
    assert (decision, reason, who) == ("allowed", "approved", APPROVER)


def test_an_unclassified_read_still_runs_freely():
    decision, reason, _ = policy.decide("read_tool", {}, GATED, SECRET)
    assert (decision, reason) == ("allowed", "read")


def test_load_policy_reads_classification_and_gated(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text(
        "tools:\n  a: read\nclassification:\n  a: financial\ngated:\n  - financial\n"
    )
    p = policy.load_policy(f)
    assert p.classification == {"a": "financial"} and p.gated == ("financial",)


def test_unknown_tool_denied():
    assert policy.decide("nope", {}, POLICY, SECRET) == ("denied", "not_in_policy", "")


def test_write_without_token_denied():
    assert policy.decide("write_tool", {"name": "x"}, POLICY, SECRET) == (
        "denied",
        "approval_required",
        "",
    )


def test_write_with_valid_token_allowed():
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("allowed", "approved", APPROVER)


def test_tampered_args_denied():
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    args = {"name": "y", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("denied", "approval_required", "")


def test_no_secret_denies_writes():
    tok = policy.mint_token("write_tool", {"name": "x"}, "", APPROVER)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, "") == ("denied", "no_approval_secret", "")


def test_governed_emits_audit(monkeypatch, caplog):
    monkeypatch.setattr(policy, "POLICY", POLICY)
    monkeypatch.setenv("APPROVAL_SECRET", SECRET)

    @policy.governed
    def write_tool(name: str, approval_token: str = "") -> dict:
        return {"restarted": name}

    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        denied = write_tool(name="x")
        tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
        ok = write_tool(name="x", approval_token=tok)

    assert denied == {"denied": True, "reason": "approval_required"}
    assert ok == {"restarted": "x"}
    events = [json.loads(r.message) for r in caplog.records if r.name == "solvent.audit"]
    assert [e["decision"] for e in events] == ["denied", "allowed"]
    assert set(events[0]) == {
        "ts", "caller", "tool", "args_hash", "decision", "reason", "latency_ms", "trace_id",
        "span_id",
    }
    assert set(events[1]) == set(events[0]) | {"approved_by"}
    assert events[0]["tool"] == "write_tool"
    assert events[0]["args_hash"] == events[1]["args_hash"]  # token excluded from hash


def test_governed_preserves_signature():
    @policy.governed
    def t(a: int, b: str = "z") -> dict:
        return {}

    assert list(inspect.signature(t).parameters) == ["a", "b"]


def test_approve_cli(monkeypatch, capsys):
    from app import approve

    monkeypatch.setenv("APPROVAL_SECRET", SECRET)
    approve.main(["--approver", APPROVER, "write_tool", "name=x"])
    out = capsys.readouterr().out.strip()
    assert out == policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)


def test_approve_cli_coerces_numeric_args(monkeypatch, capsys):
    """`days=3` on the CLI must sign as the int 3, matching what the server's schema coerces
    args to - a str `"3"` would canonicalise differently and the HMAC would never match."""
    from app import approve

    monkeypatch.setenv("APPROVAL_SECRET", SECRET)
    approve.main(["--approver", APPROVER, "get_cost_summary", "days=3"])
    tok = capsys.readouterr().out.strip()
    gated = policy.Policy(
        {"get_cost_summary": "read"}, classification={"get_cost_summary": "financial"},
        gated=("financial",),
    )
    args = {"days": 3, "approval_token": tok}
    assert policy.decide("get_cost_summary", args, gated, SECRET) == (
        "allowed",
        "approved",
        APPROVER,
    )


def test_approve_cli_defaults_to_the_signed_in_user(monkeypatch, capsys):
    from app import approve

    monkeypatch.setenv("APPROVAL_SECRET", SECRET)
    monkeypatch.setattr(approve, "signed_in_user", lambda: APPROVER)
    approve.main(["write_tool", "name=x"])
    assert capsys.readouterr().out.strip().split(".", 2)[2] == APPROVER


def test_governed_returns_exception_as_data(monkeypatch, caplog):
    monkeypatch.setattr(policy, "POLICY", POLICY)

    @policy.governed
    def read_tool() -> dict:
        raise RuntimeError("(429) Too many requests\nsecond line")

    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        assert read_tool() == {"error": "(429) Too many requests"}
    ev = json.loads(caplog.records[-1].message)
    assert (ev["decision"], ev["reason"]) == ("allowed", "error:RuntimeError")


def test_expired_token_denied():
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER, now=1000)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET, now=1599) == (
        "allowed",
        "approved",
        APPROVER,
    )
    assert policy.decide("write_tool", args, POLICY, SECRET, now=1601) == (
        "denied",
        "approval_expired",
        APPROVER,
    )


def test_future_dated_token_denied():
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER, now=1000 + 86400)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET, now=1000) == (
        "denied",
        "approval_not_yet_valid",
        APPROVER,
    )


def test_small_clock_skew_is_tolerated():
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER, now=1030)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET, now=1000)[0] == "allowed"


def test_approve_cli_fills_defaults_the_server_fills(monkeypatch, capsys):
    """The server hands governed `days=7` even when the call omits it; a token minted without
    it must still verify, or an approver has to know every default to approve anything."""
    from app import approve

    monkeypatch.setenv("APPROVAL_SECRET", SECRET)
    approve.main(["--approver", APPROVER, "get_cost_summary"])
    tok = capsys.readouterr().out.strip()
    args = {"days": 7, "approval_token": tok}
    assert policy.decide("get_cost_summary", args, GATED, SECRET)[:2] == ("allowed", "approved")


def test_approve_cli_keeps_str_params_as_strings(monkeypatch, capsys):
    """`name=null` is a valid app name; it must sign as the string the server receives."""
    from app import approve

    monkeypatch.setenv("APPROVAL_SECRET", SECRET)
    approve.main(["--approver", APPROVER, "restart_container_app", "name=null"])
    tok = capsys.readouterr().out.strip()
    args = {"name": "null", "approval_token": tok}
    assert policy.decide("restart_container_app", args, GATED, SECRET)[:2] == (
        "allowed",
        "approved",
    )


def test_tampered_timestamp_denied():
    sig, ts, who = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER, now=1000).split(
        ".", 2
    )
    args = {"name": "x", "approval_token": f"{sig}.{int(ts) + 3600}.{who}"}
    assert policy.decide("write_tool", args, POLICY, SECRET, now=1000) == (
        "denied",
        "approval_required",
        "",
    )


def test_malformed_token_denied():
    args = {"name": "x", "approval_token": "garbage"}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("denied", "approval_required", "")


def test_policy_ttl_defaults_and_overrides():
    p = policy.Policy({"write_tool": "write"}, {"write_tool": 120})
    assert p.ttl("write_tool") == 120
    assert p.ttl("other") == policy.TOKEN_TTL_S
    assert p.max_ttl == policy.TOKEN_TTL_S  # a shorter override never shrinks the prune window


def test_policy_max_ttl_tracks_the_longest_window():
    assert policy.Policy({}, {"slow": 9000}).max_ttl == 9000


def test_per_tool_ttl_is_enforced():
    p = policy.Policy({"write_tool": "write"}, {"write_tool": 120})
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER, now=1000)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, p, SECRET, now=1119)[:2] == ("allowed", "approved")
    assert policy.decide("write_tool", args, p, SECRET, now=1121)[:2] == (
        "denied",
        "approval_expired",
    )


def test_load_policy_reads_all_three_blocks(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text(
        "tools:\n  a: read\n  b: write\nttl_s:\n  b: 120\napprovers:\n  - ops@example.com\n"
    )
    p = policy.load_policy(f)
    assert p.tools == {"a": "read", "b": "write"}
    assert p.ttl("b") == 120
    assert p.approvers == ("ops@example.com",)


def test_load_policy_tolerates_missing_optional_blocks(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("tools:\n  a: read\n")
    p = policy.load_policy(f)
    assert p.ttl_s == {} and p.approvers == ()


def test_token_carries_the_approver():
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    assert tok.split(".", 2)[2] == APPROVER
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("allowed", "approved", APPROVER)


def test_approver_with_dots_survives_the_split():
    who = "first.last@sub.example.co.uk"
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, who)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("allowed", "approved", who)


def test_swapped_approver_denied():
    """The identity is inside the signed message, so it cannot be relabelled."""
    sig, ts, _ = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER).split(".", 2)
    args = {"name": "x", "approval_token": f"{sig}.{ts}.someone.else@example.com"}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("denied", "approval_required", "")


def test_superscript_timestamp_denied_not_crashed():
    """str.isdigit() is also true of "²", which int() then rejects with ValueError - and
    decide() runs outside governed's try/finally, so that would reach the caller as an
    unaudited crash rather than a denial. isdecimal() is exactly the set int() accepts."""
    args = {"name": "x", "approval_token": "a" * 64 + ".².bob"}
    assert policy.decide("write_tool", args, POLICY, SECRET) == (
        "denied",
        "approval_required",
        "",
    )


def test_token_without_approver_denied():
    args = {"name": "x", "approval_token": "deadbeef.1700000000"}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("denied", "approval_required", "")


def test_audit_names_the_approver(monkeypatch, caplog):
    monkeypatch.setattr(policy, "POLICY", POLICY)
    monkeypatch.setenv("APPROVAL_SECRET", SECRET)

    @policy.governed
    def write_tool(name: str, approval_token: str = "") -> dict:
        return {"restarted": name}

    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        write_tool(name="x", approval_token=tok)
    ev = json.loads(caplog.records[-1].message)
    assert ev["approved_by"] == APPROVER


def test_audit_omits_approver_for_reads(monkeypatch, caplog):
    monkeypatch.setattr(policy, "POLICY", POLICY)

    @policy.governed
    def read_tool() -> dict:
        return {}

    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        read_tool()
    assert "approved_by" not in json.loads(caplog.records[-1].message)


def test_unlisted_approver_denied():
    p = policy.Policy({"write_tool": "write"}, {}, ("ops@example.com",))
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, "intern@example.com")
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, p, SECRET) == (
        "denied",
        "approver_not_authorized",
        "intern@example.com",
    )


def test_listed_approver_allowed():
    p = policy.Policy({"write_tool": "write"}, {}, ("ops@example.com",))
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, "ops@example.com")
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, p, SECRET)[:2] == ("allowed", "approved")


def test_empty_allow_list_permits_any_identity():
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, "anyone@example.com")
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET)[:2] == ("allowed", "approved")


def test_allow_list_is_checked_after_the_signature():
    """An unsigned claim must never reveal who is on the list."""
    p = policy.Policy({"write_tool": "write"}, {}, ("ops@example.com",))
    args = {"name": "x", "approval_token": "deadbeef.1700000000.ops@example.com"}
    assert policy.decide("write_tool", args, p, SECRET) == ("denied", "approval_required", "")


def test_env_var_overrides_the_file(tmp_path, monkeypatch):
    f = tmp_path / "policy.yaml"
    f.write_text("tools:\n  a: read\napprovers:\n  - file@example.com\n")
    monkeypatch.setenv("APPROVERS", "env1@example.com, env2@example.com")
    assert policy.load_policy(f).approvers == ("env1@example.com", "env2@example.com")


def test_token_is_single_use():
    consume = nonces.local_consumer()
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    args = {"name": "x", "approval_token": tok}
    first = policy.decide("write_tool", args, POLICY, SECRET, consume=consume)
    second = policy.decide("write_tool", args, POLICY, SECRET, consume=consume)
    assert first == ("allowed", "approved", APPROVER)
    assert second == ("denied", "approval_replayed", APPROVER)


def test_ledger_outage_is_denied_and_audited():
    """An outage in the ledger must deny and be named, not crash before governed can log it."""

    def boom(sig, ts, now, max_ttl):
        raise RuntimeError("table unavailable")

    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET, consume=boom) == (
        "denied",
        "ledger_unavailable",
        APPROVER,
    )


def test_replay_check_is_opt_in():
    """decide() with no ledger is a pure decision - the same token verifies twice."""
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET)[:2] == ("allowed", "approved")
    assert policy.decide("write_tool", args, POLICY, SECRET)[:2] == ("allowed", "approved")


def test_expired_beats_replayed():
    """An old token reads as expired, not as a replay - the earlier check wins."""
    consumed: dict[str, int] = {}
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER, now=1000)
    args = {"name": "x", "approval_token": tok}
    assert (
        policy.decide(
            "write_tool", args, POLICY, SECRET, now=1601, consume=nonces.local_consumer(consumed)
        )[1]
        == "approval_expired"
    )
    assert consumed == {}  # an expired token must not burn a ledger slot


def test_consumed_ledger_is_pruned():
    consumed: dict[str, int] = {}
    consume = nonces.local_consumer(consumed)
    old = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER, now=1000)
    policy.decide(
        "write_tool", {"name": "x", "approval_token": old}, POLICY, SECRET,
        now=1000, consume=consume,
    )
    assert len(consumed) == 1
    fresh = policy.mint_token("write_tool", {"name": "y"}, SECRET, APPROVER, now=9000)
    policy.decide(
        "write_tool", {"name": "y", "approval_token": fresh}, POLICY, SECRET,
        now=9000, consume=consume,
    )
    assert len(consumed) == 1  # the 1000s entry aged out of every possible window


def test_governed_rejects_a_replayed_token(monkeypatch):
    monkeypatch.setattr(policy, "POLICY", POLICY)
    monkeypatch.setattr(policy.nonces, "_PROCESS", {})
    monkeypatch.setenv("APPROVAL_SECRET", SECRET)

    @policy.governed
    def write_tool(name: str, approval_token: str = "") -> dict:
        return {"restarted": name}

    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    assert write_tool(name="x", approval_token=tok) == {"restarted": "x"}
    assert write_tool(name="x", approval_token=tok) == {
        "denied": True,
        "reason": "approval_replayed",
    }


def test_non_ascii_token_denied_not_crashed():
    """compare_digest refuses non-ASCII str; the caller picks the token, so compare bytes."""
    args = {"name": "x", "approval_token": "\x80.1700000000.who"}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("denied", "approval_required", "")


def test_non_ascii_approver_round_trips():
    who = "opsé@example.com"
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, who)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("allowed", "approved", who)


def _entra_on(monkeypatch, names=None):
    names = names or {"ops@example.com"}
    monkeypatch.setattr(policy.entra, "configured", lambda: True)
    monkeypatch.setattr(policy.entra, "verified_claims", lambda t: {"tok": t} if t else None)
    monkeypatch.setattr(policy.entra, "approver_names", lambda c: names)


def test_proof_is_required_once_entra_is_configured(monkeypatch):
    _entra_on(monkeypatch)
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    decision, reason, _ = policy.decide(
        "write_tool", {"name": "x", "approval_token": tok}, POLICY, SECRET
    )
    assert (decision, reason) == ("denied", "approver_unproven")


def test_a_proven_approver_is_allowed(monkeypatch):
    _entra_on(monkeypatch)
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    decision, reason, who = policy.decide(
        "write_tool", {"name": "x", "approval_token": f"a.jwt.here~{tok}"}, POLICY, SECRET
    )
    assert (decision, reason, who) == ("allowed", "approved", APPROVER)


def test_a_token_proving_someone_else_is_denied(monkeypatch):
    _entra_on(monkeypatch, names={"someone.else@example.com"})
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    decision, reason, _ = policy.decide(
        "write_tool", {"name": "x", "approval_token": f"a.jwt.here~{tok}"}, POLICY, SECRET
    )
    assert (decision, reason) == ("denied", "approver_mismatch")


def test_proof_is_skipped_when_entra_is_absent(monkeypatch):
    monkeypatch.setattr(policy.entra, "configured", lambda: False)
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    decision, reason, _ = policy.decide(
        "write_tool", {"name": "x", "approval_token": tok}, POLICY, SECRET
    )
    assert (decision, reason) == ("allowed", "approved")


def test_approver_containing_tilde_survives_unproven():
    """An identity may itself contain ~; a bare token still has to parse by HMAC shape, not by

    splitting on the first ~, or the split would land inside the identity instead of at a
    (non-existent) proof boundary.
    """
    who = "a~b"
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, who)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("allowed", "approved", who)


def test_approver_containing_tilde_survives_with_proof(monkeypatch):
    """Same identity, but now a real proof is prepended - the first ~ is still the proof

    boundary because a JWT itself can never contain one, so the split must not be fooled by
    a second ~ further along inside the identity.
    """
    who = "a~b"
    _entra_on(monkeypatch, names={who})
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, who)
    decision, reason, approver = policy.decide(
        "write_tool", {"name": "x", "approval_token": f"a.jwt.here~{tok}"}, POLICY, SECRET
    )
    assert (decision, reason, approver) == ("allowed", "approved", who)
