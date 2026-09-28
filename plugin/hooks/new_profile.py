#!/usr/bin/env python3
# CLI invoked by the /new-profile command. Creates a NEW, empty Neuronz.ai profile
# and routes THIS session's directory to it, so every later session started here
# resolves to it. It exists for the directory automatic creation never reaches:
# one inside another profile's recursive route, which resolves to that parent
# profile until it gets a route of its own. The new route is exact and is the
# longer prefix, so it wins over the parent's.
#
#   new_profile.py --name <name> [--recursive]
#
# The directory is the one the memory hooks send (core/profile.canonical_cwd: a
# linked worktree or a subdirectory of a repo maps to the main worktree root), so
# the route matches what every hook resolves. Moving the RUNNING session onto the
# new profile is the /switch-profile helper's job; the command runs it next.
#
# Loud (user-invoked, not a hook): it prints what it did or why the server
# refused. No session id is needed: creating a profile and a route is not
# session-scoped. Pure stdlib.

import argparse
import os
import sys

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HOOKS_DIR)
sys.path.insert(0, HOOKS_DIR)
sys.path.insert(0, PLUGIN_ROOT)
from core import api, profile as profile_mod  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Create a Neuronz.ai profile for this directory.")
    parser.add_argument("--name", default="", help="the new profile's name")
    parser.add_argument("--recursive", action="store_true", help="also cover every subdirectory")
    args = parser.parse_args()

    name = (args.name or "").strip()
    if not name:
        print("new-profile: a profile name is required.", file=sys.stderr)
        return 2

    directory = profile_mod.canonical_cwd(os.getcwd())
    result = api.post(
        "/api/profiles",
        body={"name": name, "dir": directory, "recursive": args.recursive},
        timeout=8,
        where="new_profile",
        report_errors=True,
    )
    if result is None:
        print("new-profile: could not reach Neuronz.ai; nothing was created.", file=sys.stderr)
        return 1
    if "_status" in result:
        reason = result.get("error")
        detail = reason if isinstance(reason, str) else f"the server answered {result['_status']}"
        print(f"new-profile: not created — {detail}.", file=sys.stderr)
        return 1

    covered = f"{directory} and every directory under it" if args.recursive else directory
    print(f'new-profile: created profile "{name}" and routed {covered} to it. New sessions started there use it.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
