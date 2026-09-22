"""Human side of the gate: mint an approval token for one exact write call.

    APPROVAL_SECRET=... python -m app.approve restart_container_app name=solvent-app
    APPROVAL_SECRET=... python -m app.approve --approver ops@example.com restart_container_app name=x

The token carries who approved, so the audit log names a person rather than "a human".
"""

import os
import subprocess
import sys

from app.policy import mint_token


def signed_in_user() -> str:
    """Whoever `az login` says you are. Exits rather than minting an unattributable token."""
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


def main(argv: list[str]) -> None:
    approver = ""
    if argv[:1] == ["--approver"]:
        approver, argv = argv[1], argv[2:]
    tool, *pairs = argv
    args = dict(p.split("=", 1) for p in pairs)
    print(mint_token(tool, args, os.environ["APPROVAL_SECRET"], approver or signed_in_user()))


if __name__ == "__main__":
    main(sys.argv[1:])
