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

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

DEFAULT_TOKENSTORE = "~/.garminconnect"


def _prompt_mfa() -> str:
    return input("MFA code (check your email/authenticator app): ").strip()


def main() -> None:
    tokenstore = os.environ.get("GARMINTOKENS", DEFAULT_TOKENSTORE)
    print("Garmin Connect login")
    print(f"Tokens will be stored at {tokenstore}/garmin_tokens.json\n")

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
    print(f"✓ Tokens saved to {tokenstore}/garmin_tokens.json")
    print("\nYou're set — the MCP server will reuse these tokens automatically.")


if __name__ == "__main__":
    main()
