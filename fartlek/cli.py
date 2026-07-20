"""`fartlek` CLI — auth, doctor, accounts, reset.

The only place Garmin credentials are ever typed is `fartlek auth`; they are
never persisted. Everything else reads the tokens it stores.
"""
from __future__ import annotations

import argparse
import getpass
import shutil
import sys
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from fartlek.paths import default_tokenstore, fartlek_home


def _token_file(tokenstore: Path) -> Path:
    return tokenstore if tokenstore.suffix == ".json" else tokenstore / "garmin_tokens.json"


def _prompt_mfa() -> str:
    return input("MFA code (check your email/authenticator app): ").strip()


def cmd_auth(_args: argparse.Namespace) -> int:
    tokenstore = default_tokenstore()
    token_file = _token_file(tokenstore)
    print("Garmin Connect login")
    print(f"Tokens will be stored at {token_file}\n")

    # The library resumes silently from an existing tokenstore, which would
    # ignore the credentials typed below — make replacement explicit instead.
    if token_file.exists():
        answer = input("Existing tokens found. Replace with a new login? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Keeping existing tokens. Nothing to do.")
            return 0
        token_file.unlink()

    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        return 1

    client = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
    try:
        client.login(tokenstore=str(tokenstore))
    except GarminConnectTooManyRequestsError:
        print(
            "\nGarmin is rate-limiting login attempts. Wait a few minutes and retry.",
            file=sys.stderr,
        )
        return 1
    except GarminConnectAuthenticationError as exc:
        print(f"\nAuthentication failed: {exc}", file=sys.stderr)
        return 1
    except GarminConnectConnectionError as exc:
        print(f"\nCould not reach Garmin: {exc}", file=sys.stderr)
        return 1

    who = client.display_name or email
    print(f"\n✓ Logged in as {who}")
    print(f"✓ Tokens saved to {token_file}")
    print("\nYou're set — the MCP server will reuse these tokens automatically.")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True
    tokenstore = default_tokenstore()
    token_file = _token_file(tokenstore)

    if token_file.exists():
        print(f"✓ tokens present at {token_file}")
        try:
            client = Garmin()
            client.login(tokenstore=str(tokenstore))
            print(f"✓ Garmin session OK (logged in as {client.display_name})")
        except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
            print(f"✗ Garmin session failed: {exc}")
            print("  → run `fartlek auth`")
            ok = False
    else:
        print(f"✗ no tokens at {token_file} — run `fartlek auth`")
        ok = False

    stores = sorted(fartlek_home().glob("*/store.db"))
    if stores:
        for s in stores:
            print(f"✓ store: {s.parent.name} ({s.stat().st_size // 1024} KB)")
    else:
        print("· no local store yet (created at first sync)")
    return 0 if ok else 1


def cmd_accounts(_args: argparse.Namespace) -> int:
    stores = sorted(fartlek_home().glob("*/store.db"))
    if not stores:
        print("No accounts yet — run `fartlek auth` then a first sync.")
        return 0
    for s in stores:
        print(s.parent.name)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from fartlek.health.adapters.garmin_connect import GarminConnectAdapter
    from fartlek.health.exceptions import GarminAuthError
    from fartlek.paths import account_dir, store_path
    from fartlek.store import Store
    from fartlek.sync.engine import SyncEngine

    adapter = GarminConnectAdapter(tokenstore=default_tokenstore())
    try:
        client = adapter.connect_sync()
    except GarminAuthError as exc:
        print(exc, file=sys.stderr)
        return 1
    name = client.display_name or "default"
    print(f"Syncing account {name} → {store_path(name)}")

    with Store(store_path(name)) as store:
        engine = SyncEngine(
            store,
            lambda path, **p: adapter.fetch_sync(client, path, **p),
            name,
            account_dir(name),
        )
        def _report(tier_name: str, result: dict) -> bool:
            if result.get("skipped"):
                print(f"✗ {tier_name}: {result['reason']}")
                return False
            print(f"✓ {tier_name}: {result.get('calls', '?')} calls")
            for err in result.get("errors") or []:
                print(f"  ⚠ {err}", file=sys.stderr)
            return True

        if not _report("tier0 (snapshot)", engine.tier0()):
            return 1
        if not _report("tier1 (180d history)", engine.tier1()):
            return 1
        if args.nights and not _report(
            f"tier2 ({args.nights} nights backfill)", engine.tier2(backfill_days=args.nights)
        ):
            return 1
        pmc = store.get_pmc(end_date=engine._today(), days=1)
        if pmc:
            p = pmc[-1]
            print(f"✓ PMC today: CTL {p['ctl']:.1f} · ATL {p['atl']:.1f} · TSB {p['tsb']:.1f}")
    print("Done.")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from fartlek.store import Store

    stores = sorted(fartlek_home().glob("*/store.db"))
    if not stores:
        print("No local store yet — run `fartlek sync` first.", file=sys.stderr)
        return 1
    import sqlite3

    out = Path(args.out or "fartlek-export")
    for db in stores:
        target = out / db.parent.name
        target.mkdir(parents=True, exist_ok=True)
        with Store(db) as s:
            written = s.export_csv(target)
        # sqlite backup API, not a file copy: a live WAL would make the
        # copied .db stale or torn while another process writes.
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(target / "store.db")
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        print(f"✓ {db.parent.name}: store.db + {len(written)} CSV files → {target}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    home = fartlek_home()
    if not home.exists():
        print("Nothing to reset.")
        return 0
    answer = input(f"Delete ALL Fartlek tokens and local data under {home}? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted.")
        return 0
    shutil.rmtree(home)
    print(f"✓ removed {home}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fartlek",
        description="Fartlek — a coach's morning report from your Garmin data, for any LLM via MCP.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth", help="log in to Garmin Connect (one-time; MFA supported)").set_defaults(func=cmd_auth)
    sub.add_parser("doctor", help="check tokens, Garmin connectivity and local store health").set_defaults(func=cmd_doctor)
    sub.add_parser("accounts", help="list local accounts").set_defaults(func=cmd_accounts)
    p_sync = sub.add_parser("sync", help="fetch Garmin data into the local store (tier 0 + 1)")
    p_sync.add_argument("--nights", type=int, default=0, help="also backfill N nights of sleep/HRV (tier 2)")
    p_sync.set_defaults(func=cmd_sync)
    p_export = sub.add_parser("export", help="export the local store (SQLite copy + CSV per table)")
    p_export.add_argument("out", nargs="?", help="output directory (default: ./fartlek-export)")
    p_export.set_defaults(func=cmd_export)
    sub.add_parser("reset", help="wipe all local tokens and data (asks confirmation)").set_defaults(func=cmd_reset)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
