"""Human side of the gate: mint an approval token for one exact write call.

    APPROVAL_SECRET=... python -m app.approve restart_container_app name=solvent-app
    APPROVAL_SECRET=... python -m app.approve --approver ops@example.com restart_container_app name=x

The token carries who approved, so the audit log names a person rather than "a human".
"""

import json
import os
import subprocess
import sys

from app.policy import mint_token


def _coerce(v: str) -> object:
    """`3` must sign as the int the server's schema coerces to, not the str `"3"`, or the HMAC
    over canonical_json(args) never matches. json.loads gets us that for numbers/bools/null;
    anything that is not valid JSON (a name like `solvent-app`, or a leading-zero id like
    `0123`, which JSON also rejects as a number) falls back to the raw string, which is what
    a plain CLI arg already was.
    """
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def signed_in_user() -> str:
    """Whoever `az login` says you are. Exits rather than minting an unattributable token.

    Human-only by design: a service-principal login reports the app id here, which never
    appears in approver_names (that reads oid, not appid), so an SP-minted approval would be a
    guaranteed approver_mismatch. That is the intended failure, not a bug to route around.
    """
    try:
        r = subprocess.run(
            ["az", "account", "show", "--query", "user.name", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    raise SystemExit("could not determine the approver; pass --approver <identity>")


def entra_proof(audience: str) -> str:
    """An access token for the API, proving the approver is who the token says.

    Uses the operator's own `az login` session: the same identity `signed_in_user` reports, so
    the two halves of the approval cannot disagree.
    """
    r = subprocess.run(
        ["az", "account", "get-access-token", "--resource", audience,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit(f"could not get a token for {audience}; run `az login`")
    return r.stdout.strip()


def main(argv: list[str]) -> None:
    approver = ""
    if argv[:1] == ["--approver"]:
        approver, argv = argv[1], argv[2:]
    tool, *pairs = argv
    args = {k: _coerce(v) for k, v in (p.split("=", 1) for p in pairs)}
    token = mint_token(tool, args, os.environ["APPROVAL_SECRET"], approver or signed_in_user())
    audience = os.environ.get("ENTRA_AUDIENCE", "")
    print(f"{entra_proof(audience)}~{token}" if audience else token)


if __name__ == "__main__":
    main(sys.argv[1:])
