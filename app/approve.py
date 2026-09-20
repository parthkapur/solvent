"""Human side of the gate: mint an approval token for one exact write call.

    APPROVAL_SECRET=... python -m app.approve restart_container_app name=solvent-app
"""

import os
import sys

from app.policy import mint_token


def main(argv: list[str]) -> None:
    tool, *pairs = argv
    args = dict(p.split("=", 1) for p in pairs)
    print(mint_token(tool, args, os.environ["APPROVAL_SECRET"]))


if __name__ == "__main__":
    main(sys.argv[1:])
