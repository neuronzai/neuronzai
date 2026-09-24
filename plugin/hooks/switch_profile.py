#!/usr/bin/env python3
# CLI invoked by the /switch-profile command. Writes (or clears) the SESSION
# profile override that the memory hooks read, so the rest of THIS session is
# scoped to a chosen profile WITHOUT creating a (profile, cwd) anchor or route.
# The override is session-scoped and ephemeral (see neuronzai_session.py):
# nothing is mapped to the directory and nothing persists past the session.
#
#   switch_profile.py --session-id <sid> --profile <name>   # switch
#   switch_profile.py --session-id <sid> --reset            # revert to cwd/env
#
# Loud (user-invoked, not a hook): it prints what it did or why it couldn't. Pure
# stdlib. The session id comes from ${CLAUDE_SESSION_ID} substituted by the CLI;
# without one the switch cannot be scoped, so it fails clearly rather than writing
# a garbage-keyed override.

import argparse
import os
import sys

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HOOKS_DIR)
sys.path.insert(0, HOOKS_DIR)
sys.path.insert(0, PLUGIN_ROOT)
import neuronzai_session as ns  # noqa: E402
from core import api, profile as profile_mod  # noqa: E402


def _restore_local(session_id, previous):
    if previous:
        ns.write_profile(session_id, previous)
    else:
        ns.clear_profile(session_id)


def _host_name(explicit):
    """Resolve which harness is running this CLI.

    The old default was a literal host name, and a default here is not a guess that
    degrades — it decides which DIRECTORIES get written and which rescan sentence
    is printed, so a wrong one repairs a directory the running harness never reads
    and then reports success. Resolution lives in entry so the hook path and the
    CLI path cannot answer this differently; an import failure in a partial
    checkout leaves the caller's own value, since refusing a switch over it would
    be worse."""
    try:
        from entry import resolve_host_name

        return resolve_host_name(explicit)
    except Exception:
        return explicit


def _activate_server(session_id, profile="", cwd=""):
    result = api.post(
        "/api/memory/session-profile",
        params={"cwd": cwd} if cwd else None,
        body={"session": session_id},
        profile=profile,
        session_id=session_id,
        timeout=8,
        where="switch_profile",
    )
    expected = profile or None
    return (
        isinstance(result, dict)
        and result.get("ok") is True
        and (expected is None or result.get("activeProfile") == expected)
    )


def _rematerialize(host_name, session_id):
    """Re-materialize the now-current profile's assets and return the sentences that
    say what happened and what this session will and will not see — or None when the
    re-materialization could not be attempted at all.

    A switch that leaves the OLD profile's assets mounted is the defect #530 opens
    with, and on the host where the CLI actually runs there is no hook to do it:
    the PreToolUse applier is skipped exactly where the session id expands into the
    command (core/switch). So the CLI owns this leg. Printed output carries no rescan
    directive — only a hook result can — so it reports every kind as NOT re-scanned
    and names the command that can pick the live ones up in place.

    None is NOT the same as an empty rescan sentence, and the caller must not
    collapse them: claiming assets were reconciled when the attempt never ran is
    the exact class of lie the reload report exists to prevent."""
    # One try over the imports too: this file is MIRRORED into a development copy
    # whose core/ holds only part of the package, so an import here can legitimately
    # fail in a checkout while the switch itself is fine. A switch that already
    # landed server-side must never be reported as failed over its assets.
    try:
        from core import assets, reload as reload_core
        from core.hostapi import Event
        from entry import _load_host

        host = _load_host(host_name)
        result = assets.materialize(
            Event(event="pre_tool", session_id=session_id, cwd=os.getcwd()), host, force=True
        )
        return reload_core.cli_report(host, result, offer_reload=True)
    except Exception:
        return None


# What the CLI says when the re-materialization could not even be attempted. The
# switch itself already landed, so this reports the assets alone, and it names the
# command that retries rather than leaving the user with a dead end.
ASSETS_UNRECONCILED = (
    "Its assets could NOT be reconciled, so this session may still be mounting the "
    "previous profile's set — run /reload-assets to retry."
)


def main():
    parser = argparse.ArgumentParser(description="Switch this session's Neuronz.ai profile.")
    parser.add_argument("--session-id", default="", help="Claude Code session id (${CLAUDE_SESSION_ID})")
    parser.add_argument("--profile", default="", help="target profile name")
    parser.add_argument("--reset", action="store_true", help="clear the override; revert to cwd/env")
    parser.add_argument("--host", default="auto", help="host adapter name ('auto' resolves it)")
    args = parser.parse_args()

    session_id = (args.session_id or "").strip()
    if not session_id or session_id == ns.UNSUBSTITUTED_SESSION_ID:
        print(
            "switch-profile: no session id available — cannot scope the switch. "
            "Your CLI may not substitute ${CLAUDE_SESSION_ID}.",
            file=sys.stderr,
        )
        return 2

    profile = (args.profile or "").strip()
    previous = ns.read_profile(session_id)
    if args.reset or not profile:
        # Resolve the normal profile WITHOUT consulting the override we are about
        # to clear. The authenticated server switch lands before the helper
        # reports success, so a headerless lifecycle hook can never guess.
        env_profile = api.ENV_PROFILE
        cwd = "" if env_profile else profile_mod.canonical_cwd(os.getcwd())
        existed = ns.clear_profile(session_id)
        if not _activate_server(session_id, profile=env_profile, cwd=cwd):
            _restore_local(session_id, previous)
            print(
                "switch-profile: server rejected or could not register the reset; "
                "the previous session profile is unchanged.",
                file=sys.stderr,
            )
            return 1
        assets = _rematerialize(_host_name(args.host), session_id) if existed else ""
        print(
            ("switch-profile: reset — this session reverts to its directory/env profile. "
             + (ASSETS_UNRECONCILED if assets is None else assets)).strip()
            if existed
            else "switch-profile: no override was set; nothing to reset."
        )
        return 0

    ns.write_profile(session_id, profile)
    if not _activate_server(session_id, profile=profile):
        _restore_local(session_id, previous)
        print(
            "switch-profile: server rejected or could not register the switch; "
            "the previous session profile is unchanged.",
            file=sys.stderr,
        )
        return 1
    assets = _rematerialize(_host_name(args.host), session_id)
    print(
        f'switch-profile: this session is now scoped to profile "{profile}". '
        "No cwd→profile anchor/route was created; a fresh session in this directory "
        "resolves normally. " + (ASSETS_UNRECONCILED if assets is None else assets)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
