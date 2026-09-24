#!/usr/bin/env python3
# CLI invoked by the /reload-assets command. Re-materializes THIS session's profile
# assets — refetching them unconditionally, rewriting the store, reconciling the
# links and sweeping foreign publications — and prints what that session will and
# will not see without restarting.
#
#   reload_assets.py --host <host> [--session-id <sid>] [--cwd <dir>]
#
# Normally this process never runs: the bundled PreToolUse interceptor recognizes
# the invocation, does the same work with the session id off the hook payload, and
# BLOCKS the call — because only a hook result can carry the host's in-session
# rescan directive. This helper is what happens when that hook is not wired (a host
# where the feature is off, a manual run from a shell), and it must still do the
# real work rather than print a promise: an asset store is repaired the same way
# either way, only the rescan differs.
#
# Loud (user-invoked, not a hook): it prints what it did. Pure stdlib.

import argparse
import os
import sys

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HOOKS_DIR)
sys.path.insert(0, HOOKS_DIR)
sys.path.insert(0, PLUGIN_ROOT)

from core import reload as reload_core  # noqa: E402
from core.hostapi import Event  # noqa: E402
from entry import _load_host, resolve_host_name  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Re-sync this session's Neuronz.ai assets.")
    parser.add_argument("--host", default="auto", help="host adapter name ('auto' resolves it)")
    parser.add_argument("--session-id", default="", help="session id, when the CLI expands one")
    parser.add_argument("--cwd", default="", help="session cwd (defaults to this process's)")
    args = parser.parse_args()

    # A wrong host is worse than no host: it would rewrite directories this harness
    # never reads and then print the OTHER host's rescan promise. 'auto' resolves
    # from what the running bridge exported, never from a hardcoded name.
    host_name = resolve_host_name(args.host)
    try:
        host = _load_host(host_name)
    except Exception:
        print(f"reload-assets: unknown host {host_name!r}.", file=sys.stderr)
        return 2

    session_id = (args.session_id or "").strip()
    # An unexpanded token (the CLI did not substitute it) is not a session id. Drop
    # it: the materialization is keyed by PROFILE, and the id only tags the request
    # for per-session availability stats — a literal "${...}" would poison those.
    if session_id.startswith("$"):
        session_id = ""

    event = Event(
        event="pre_tool",
        session_id=session_id,
        cwd=(args.cwd or os.getcwd()),
    )
    from core import assets

    result = assets.materialize(event, host, force=True)
    # cli_report, not report: stdout carries no rescan directive, and this helper only
    # runs when the interceptor did NOT fire — so it does not offer itself back.
    print(reload_core.cli_report(host, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
