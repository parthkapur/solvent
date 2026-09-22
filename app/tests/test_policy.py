import inspect
import json
import logging

from app import policy

POLICY = policy.Policy({"read_tool": "read", "write_tool": "write"})
SECRET = "s3cret"
APPROVER = "ops@example.com"


def test_read_allowed():
    assert policy.decide("read_tool", {}, POLICY, SECRET) == ("allowed", "read", "")


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
        "ts", "tool", "args_hash", "decision", "reason", "latency_ms", "trace_id", "span_id",
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
    consumed: dict[str, int] = {}
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER)
    args = {"name": "x", "approval_token": tok}
    first = policy.decide("write_tool", args, POLICY, SECRET, consumed=consumed)
    second = policy.decide("write_tool", args, POLICY, SECRET, consumed=consumed)
    assert first == ("allowed", "approved", APPROVER)
    assert second == ("denied", "approval_replayed", APPROVER)


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
        policy.decide("write_tool", args, POLICY, SECRET, now=1601, consumed=consumed)[1]
        == "approval_expired"
    )
    assert consumed == {}  # an expired token must not burn a ledger slot


def test_consumed_ledger_is_pruned():
    consumed: dict[str, int] = {}
    old = policy.mint_token("write_tool", {"name": "x"}, SECRET, APPROVER, now=1000)
    policy.decide(
        "write_tool", {"name": "x", "approval_token": old}, POLICY, SECRET,
        now=1000, consumed=consumed,
    )
    assert len(consumed) == 1
    fresh = policy.mint_token("write_tool", {"name": "y"}, SECRET, APPROVER, now=9000)
    policy.decide(
        "write_tool", {"name": "y", "approval_token": fresh}, POLICY, SECRET,
        now=9000, consumed=consumed,
    )
    assert len(consumed) == 1  # the 1000s entry aged out of every possible window


def test_governed_rejects_a_replayed_token(monkeypatch):
    monkeypatch.setattr(policy, "POLICY", POLICY)
    monkeypatch.setattr(policy, "_CONSUMED", {})
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
