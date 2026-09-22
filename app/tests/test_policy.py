import inspect
import json
import logging

from app import policy

POLICY = policy.Policy({"read_tool": "read", "write_tool": "write"})
SECRET = "s3cret"


def test_read_allowed():
    assert policy.decide("read_tool", {}, POLICY, SECRET) == ("allowed", "read")


def test_unknown_tool_denied():
    assert policy.decide("nope", {}, POLICY, SECRET) == ("denied", "not_in_policy")


def test_write_without_token_denied():
    assert policy.decide("write_tool", {"name": "x"}, POLICY, SECRET) == (
        "denied",
        "approval_required",
    )


def test_write_with_valid_token_allowed():
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("allowed", "approved")


def test_tampered_args_denied():
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET)
    args = {"name": "y", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("denied", "approval_required")


def test_no_secret_denies_writes():
    tok = policy.mint_token("write_tool", {"name": "x"}, "")
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, "") == ("denied", "no_approval_secret")


def test_governed_emits_audit(monkeypatch, caplog):
    monkeypatch.setattr(policy, "POLICY", POLICY)
    monkeypatch.setenv("APPROVAL_SECRET", SECRET)

    @policy.governed
    def write_tool(name: str, approval_token: str = "") -> dict:
        return {"restarted": name}

    with caplog.at_level(logging.INFO, logger="solvent.audit"):
        denied = write_tool(name="x")
        tok = policy.mint_token("write_tool", {"name": "x"}, SECRET)
        ok = write_tool(name="x", approval_token=tok)

    assert denied == {"denied": True, "reason": "approval_required"}
    assert ok == {"restarted": "x"}
    events = [json.loads(r.message) for r in caplog.records if r.name == "solvent.audit"]
    assert [e["decision"] for e in events] == ["denied", "allowed"]
    assert set(events[0]) == {
        "ts", "tool", "args_hash", "decision", "reason", "latency_ms", "trace_id", "span_id",
    }
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
    approve.main(["write_tool", "name=x"])
    out = capsys.readouterr().out.strip()
    assert out == policy.mint_token("write_tool", {"name": "x"}, SECRET)


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
    tok = policy.mint_token("write_tool", {"name": "x"}, SECRET, now=1000)
    args = {"name": "x", "approval_token": tok}
    assert policy.decide("write_tool", args, POLICY, SECRET, now=1000 + 599) == ("allowed", "approved")
    assert policy.decide("write_tool", args, POLICY, SECRET, now=1000 + 601) == ("denied", "approval_expired")


def test_tampered_timestamp_denied():
    sig, ts = policy.mint_token("write_tool", {"name": "x"}, SECRET, now=1000).split(".")
    args = {"name": "x", "approval_token": f"{sig}.{int(ts) + 3600}"}
    assert policy.decide("write_tool", args, POLICY, SECRET, now=1000) == ("denied", "approval_required")


def test_malformed_token_denied():
    args = {"name": "x", "approval_token": "garbage"}
    assert policy.decide("write_tool", args, POLICY, SECRET) == ("denied", "approval_required")


def test_policy_ttl_defaults_and_overrides():
    p = policy.Policy({"write_tool": "write"}, {"write_tool": 120})
    assert p.ttl("write_tool") == 120
    assert p.ttl("other") == policy.TOKEN_TTL_S
    assert p.max_ttl == policy.TOKEN_TTL_S  # a shorter override never shrinks the prune window


def test_policy_max_ttl_tracks_the_longest_window():
    assert policy.Policy({}, {"slow": 9000}).max_ttl == 9000


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
