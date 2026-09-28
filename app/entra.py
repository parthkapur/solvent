"""Entra token verification — the only place a JWT is checked.

Both the caller guard on /mcp and the approver proof inside an approval come through here, so
there is one verification path to audit rather than two that can drift apart.
"""

import logging
import os
from typing import Any

import jwt
from jwt import PyJWKClient

_JWKS: PyJWKClient | None = None
_log = logging.getLogger(__name__)


def _client() -> PyJWKClient | None:
    """The tenant's signing keys, or None when Entra is not configured.

    ponytail: PyJWKClient's own in-process cache, refetched every 5 minutes (its default
    `lifespan`) or on any unknown `kid`, not once per cold start. A shared cache only matters
    if this ever scales out past max_replicas = 1.
    """
    global _JWKS
    tenant = os.environ.get("ENTRA_TENANT_ID", "")
    if not tenant:
        return None
    if _JWKS is None:
        _JWKS = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
            timeout=5,  # bound the damage of a slow IdP; see server.py's guard for the rest
        )
    return _JWKS


def configured() -> bool:
    """True once a tenant and an audience are both set. Unset means the JWT path is off."""
    return bool(os.environ.get("ENTRA_TENANT_ID") and os.environ.get("ENTRA_AUDIENCE"))


def verified_claims(token: str) -> dict[str, Any] | None:
    """Claims if the token verifies for this tenant and audience, else None.

    Never raises: the token comes from whoever is calling, so a malformed one must be a denial
    and not a 500.
    """
    client = _client()
    if client is None or not token or not configured():
        return None
    tenant = os.environ["ENTRA_TENANT_ID"]
    try:
        return jwt.decode(
            token,
            client.get_signing_key_from_jwt(token).key,
            algorithms=["RS256"],
            audience=os.environ["ENTRA_AUDIENCE"],
            # accessTokenAcceptedVersion defaults to null on an app registration, which means
            # v1 tokens - `az account get-access-token` (what app/approve.py runs) produces one,
            # with iss https://sts.windows.net/{tenant}/ rather than the v2 form. Both are
            # tenant-scoped, so accepting either weakens nothing.
            issuer=[
                f"https://login.microsoftonline.com/{tenant}/v2.0",
                f"https://sts.windows.net/{tenant}/",
            ],
        )
    except Exception as e:  # noqa: BLE001 - any verification failure is the same answer: no
        # Never the token or claim contents - just enough to diagnose a misconfigured tenant,
        # audience or issuer from the class of failure alone.
        _log.debug("token rejected: %s", type(e).__name__)
        return None


def caller_name(claims: dict[str, Any]) -> str:
    """Who to name in the audit log. oid is immutable; the username can be renamed."""
    return str(claims.get("oid") or claims.get("preferred_username") or claims.get("upn") or "")


def approver_names(claims: dict[str, Any]) -> set[str]:
    """Every identity this token may legitimately be called by.

    The approver in an approval is typed by a human (`az account show` gives an email), while the
    immutable identifier is the oid. Accepting either avoids forcing the operator to paste a GUID.
    """
    return {
        str(claims[k])
        for k in ("oid", "preferred_username", "upn", "email")
        if claims.get(k)
    }
