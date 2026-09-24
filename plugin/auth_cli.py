#!/usr/bin/env python3
"""User-facing Neuronz.ai OAuth login, status, and logout commands."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import auth  # noqa: E402


def _print_status(result: dict) -> None:
    if result.get("authenticated"):
        source = result.get("source")
        if source == "environment":
            print(result.get("message") or "Neuronz.ai is using an environment credential.")
        else:
            print(
                "Neuronz.ai is signed in with OAuth "
                f"({result.get('storage', 'secure local storage')})."
            )
        print(f"Server: {result.get('issuer')}")
        return
    if result.get("reauthentication_required"):
        print("The saved Neuronz.ai login has expired. Run the login workflow again.")
    else:
        print("Neuronz.ai is not signed in. Run the login workflow to connect it.")
    print(f"Server: {result.get('issuer')}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage the Neuronz.ai plugin login.")
    parser.add_argument("action", choices=("login", "status", "logout"), nargs="?", default="status")
    parser.add_argument("--json", action="store_true", help="print non-secret status as JSON")
    # Second half of a browserless login: the code copied off the sign-in page.
    # A separate invocation rather than a prompt, because this runs as a shell
    # command from the skill and so has no interactive stdin to read from.
    parser.add_argument(
        "--code",
        help="complete a browserless sign-in with the code shown on the sign-in page",
    )
    # Escape hatch when detection guesses wrong — a display is advertised but
    # cannot actually render (a dead X forward, a broken container display).
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="skip the browser entirely and sign in with a link plus a pasted code",
    )
    args = parser.parse_args(argv)

    try:
        if args.action == "login":
            if args.code:
                result = auth.login_complete(args.code)
            else:
                print("Starting Neuronz.ai sign-in…")
                result = auth.login(no_browser=args.no_browser)
        elif args.action == "logout":
            result = auth.logout()
        else:
            result = auth.status(verify=True)
    except auth.AuthError as error:
        print(f"Neuronz.ai authentication failed: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.action == "login":
        if result.get("pending"):
            # Not a failure — the first half of a browserless sign-in succeeded
            # and the user now finishes it from a device that has a browser.
            minutes = max(int(result.get("expires_in") or 600) // 60, 1)
            print("There is no browser on this machine, so finish the sign-in elsewhere.")
            print()
            print("1. Open this link on any device that can reach Neuronz.ai:")
            print(f"   {result.get('authorization_url')}")
            print("2. Approve the sign-in, then copy the code the page shows you.")
            print("3. Paste that code back here to finish.")
            print()
            print(f"The link is good for about {minutes} minutes.")
            print(f"Server: {result.get('issuer')}")
            return 0
        print("✓ Neuronz.ai sign-in complete. OAuth grant saved.")
        if result.get("message"):
            print(result["message"])
        print(f"Server: {result.get('issuer')}")
    elif args.action == "logout":
        if result.get("source") == "environment":
            print(result.get("message"))
            return 1
        print("✓ Neuronz.ai is signed out on this machine.")
        if result.get("removed") and not result.get("revoked"):
            print("The local grant was removed; the server was unreachable for revocation.")
    else:
        _print_status(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
