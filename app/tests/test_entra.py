"""Token verification, offline. A locally generated RSA key stands in for the tenant's."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import entra

TENANT = "00604cdb-437a-4776-871d-5fe9dfcf2730"
AUDIENCE = "api://solvent"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(**overrides):
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": AUDIENCE,
        "oid": "11111111-2222-3333-4444-555555555555",
        "preferred_username": "ops@example.com",
        "exp": int(time.time()) + 300,
        "nbf": int(time.time()) - 5,
    }
    claims.update(overrides)
    return jwt.encode(claims, _KEY, algorithm="RS256")


@pytest.fixture
def entra_configured(monkeypatch):
    """Point the module at our key instead of the tenant's JWKS endpoint."""
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("ENTRA_AUDIENCE", AUDIENCE)

    class _FakeClient:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": _KEY.public_key()})()

    monkeypatch.setattr(entra, "_client", lambda: _FakeClient())


def test_configured_is_false_without_env(monkeypatch):
    monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
    monkeypatch.delenv("ENTRA_AUDIENCE", raising=False)
    assert entra.configured() is False


def test_valid_token_returns_claims(entra_configured):
    claims = entra.verified_claims(_token())
    assert claims is not None
    assert claims["preferred_username"] == "ops@example.com"


def test_wrong_audience_is_rejected(entra_configured):
    assert entra.verified_claims(_token(aud="api://something-else")) is None


def test_wrong_issuer_is_rejected(entra_configured):
    assert entra.verified_claims(_token(iss="https://evil.example.com/v2.0")) is None


def test_v1_issuer_is_accepted(entra_configured):
    """`az account get-access-token` - what app/approve.py runs - mints a v1 token whenever
    the app registration's accessTokenAcceptedVersion is unset (the default), and a v1 token's
    iss is the sts.windows.net form, not the v2.0 one. PyJWT's issuer param takes a container
    of strings (verified against the installed 2.14.0 via jwt.api_jwt.PyJWT._validate_iss,
    which does `iss not in issuer` when issuer isn't a str), so both forms must verify."""
    claims = entra.verified_claims(_token(iss=f"https://sts.windows.net/{TENANT}/"))
    assert claims is not None
    assert claims["preferred_username"] == "ops@example.com"


def test_wrong_tenant_v1_issuer_is_still_rejected(entra_configured):
    """Accepting the v1 issuer form must not widen the check to any tenant - it stays
    tenant-scoped, just tolerant of which of the two forms Entra used to write it."""
    other_tenant = "11111111-1111-1111-1111-111111111111"
    assert entra.verified_claims(_token(iss=f"https://sts.windows.net/{other_tenant}/")) is None


def test_expired_token_is_rejected(entra_configured):
    assert entra.verified_claims(_token(exp=int(time.time()) - 10)) is None


def test_garbage_never_raises(entra_configured):
    for bad in ("", "not-a-jwt", "a.b.c", "Bearer x"):
        assert entra.verified_claims(bad) is None


def test_unconfigured_rejects_even_a_good_token(monkeypatch):
    monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
    assert entra.verified_claims(_token()) is None


def test_caller_name_prefers_the_immutable_oid(entra_configured):
    claims = entra.verified_claims(_token())
    assert entra.caller_name(claims) == "11111111-2222-3333-4444-555555555555"


def test_approver_names_include_both_shapes(entra_configured):
    claims = entra.verified_claims(_token())
    assert "ops@example.com" in entra.approver_names(claims)
    assert "11111111-2222-3333-4444-555555555555" in entra.approver_names(claims)
