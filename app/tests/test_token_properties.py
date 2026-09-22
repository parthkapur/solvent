"""The token is the security boundary, so fuzz it rather than only exemplify it."""

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app import policy

SECRET = "s3cret"
POL = policy.Policy({"write_tool": "write"})

identities = st.text(min_size=1, max_size=60).filter(lambda s: s.strip() != "")
timestamps = st.integers(min_value=0, max_value=2**31)
values = st.text(max_size=20) | st.integers() | st.booleans() | st.none()
arg_dicts = st.dictionaries(
    st.text(min_size=1, max_size=15).filter(lambda k: k != "approval_token"),
    values,
    max_size=5,
)


@given(args=arg_dicts, who=identities, ts=timestamps)
def test_a_minted_token_verifies(args, who, ts):
    tok = policy.mint_token("write_tool", args, SECRET, who, now=ts)
    assert policy.decide("write_tool", {**args, "approval_token": tok}, POL, SECRET, now=ts) == (
        "allowed",
        "approved",
        who,
    )


@given(args=arg_dicts, other=arg_dicts, who=identities, ts=timestamps)
def test_changed_arguments_never_verify(args, other, who, ts):
    assume(args != other)
    tok = policy.mint_token("write_tool", args, SECRET, who, now=ts)
    assert policy.decide(
        "write_tool", {**other, "approval_token": tok}, POL, SECRET, now=ts
    )[:2] == ("denied", "approval_required")


@given(args=arg_dicts, who=identities, other=identities, ts=timestamps)
def test_a_relabelled_identity_never_verifies(args, who, other, ts):
    assume(who != other)
    sig, _, _ = policy.mint_token("write_tool", args, SECRET, who, now=ts).partition(".")
    forged = {**args, "approval_token": f"{sig}.{ts}.{other}"}
    assert policy.decide("write_tool", forged, POL, SECRET, now=ts)[:2] == (
        "denied",
        "approval_required",
    )


@given(
    args=arg_dicts,
    who=identities,
    ts=timestamps,
    shift=st.integers(min_value=1, max_value=10**6),
)
def test_a_moved_timestamp_never_verifies(args, who, ts, shift):
    sig, _, _ = policy.mint_token("write_tool", args, SECRET, who, now=ts).partition(".")
    forged = {**args, "approval_token": f"{sig}.{ts + shift}.{who}"}
    assert policy.decide("write_tool", forged, POL, SECRET, now=ts)[:2] == (
        "denied",
        "approval_required",
    )


@given(args=arg_dicts, who=identities, ts=timestamps, other=st.text(min_size=1, max_size=64))
def test_a_forged_signature_never_verifies(args, who, ts, other):
    real, _, _ = policy.mint_token("write_tool", args, SECRET, who, now=ts).partition(".")
    assume(other != real)
    forged = {**args, "approval_token": f"{other}.{ts}.{who}"}
    assert policy.decide("write_tool", forged, POL, SECRET, now=ts)[:2] == (
        "denied",
        "approval_required",
    )


@given(args=arg_dicts, who=identities, ts=timestamps)
@settings(max_examples=50)
def test_no_token_verifies_twice(args, who, ts):
    consumed: dict[str, int] = {}
    tok = policy.mint_token("write_tool", args, SECRET, who, now=ts)
    call = {**args, "approval_token": tok}
    assert policy.decide("write_tool", call, POL, SECRET, now=ts, consumed=consumed)[0] == "allowed"
    assert policy.decide("write_tool", call, POL, SECRET, now=ts, consumed=consumed) == (
        "denied",
        "approval_replayed",
        who,
    )


@given(
    args=arg_dicts,
    who=identities,
    ts=timestamps,
    age=st.integers(min_value=601, max_value=10**6),
)
def test_anything_past_its_window_expires(args, who, ts, age):
    tok = policy.mint_token("write_tool", args, SECRET, who, now=ts)
    assert policy.decide(
        "write_tool", {**args, "approval_token": tok}, POL, SECRET, now=ts + age
    )[:2] == ("denied", "approval_expired")
