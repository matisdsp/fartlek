"""Interactive Garmin Connect login.

Authenticates with email/password (+ MFA if the account has it) and stores
OAuth tokens at the tokenstore (default ~/.garminconnect, override with
GARMINTOKENS). Everything else — the MCP server, the API — only ever reads
those tokens; this is the single place credentials are typed, and they are
never persisted.
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

DEFAULT_TOKENSTORE = "~/.garminconnect"


def _prompt_mfa() -> str:
    return input("MFA code (check your email/authenticator app): ").strip()


def _token_file(tokenstore: str) -> Path:
    p = Path(tokenstore).expanduser()
    return p if p.suffix == ".json" else p / "garmin_tokens.json"


def main() -> None:
    tokenstore = os.environ.get("GARMINTOKENS", DEFAULT_TOKENSTORE)
    token_file = _token_file(tokenstore)
    print("Garmin Connect login")
    print(f"Tokens will be stored at {token_file}\n")

    # The library resumes silently from an existing tokenstore, which would
    # ignore the credentials typed below — make replacement explicit instead.
    if token_file.exists():
        answer = input("Existing tokens found. Replace with a new login? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Keeping existing tokens. Nothing to do.")
            return
        token_file.unlink()

    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        sys.exit(1)

    client = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
    try:
        client.login(tokenstore=tokenstore)
    except GarminConnectTooManyRequestsError:
        print(
            "\nGarmin is rate-limiting login attempts. Wait a few minutes and retry.",
            file=sys.stderr,
        )
        sys.exit(1)
    except GarminConnectAuthenticationError as exc:
        print(f"\nAuthentication failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except GarminConnectConnectionError as exc:
        print(f"\nCould not reach Garmin: {exc}", file=sys.stderr)
        sys.exit(1)

    who = client.display_name or email
    print(f"\n✓ Logged in as {who}")
    print(f"✓ Tokens saved to {token_file}")
    print("\nYou're set — the MCP server will reuse these tokens automatically.")


if __name__ == "__main__":
    main()
