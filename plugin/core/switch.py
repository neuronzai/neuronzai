#!/usr/bin/env python3
# PreToolUse handler — apply a /switch-profile SESSION override on a host that does
# NOT substitute its session id into a command the model runs (topic 90ae46be).
#
# /switch-profile re-scopes the CURRENT session to another Neuronz.ai profile for
# the rest of the session only — SESSION-scoped and ephemeral, mapping NOTHING to
# the directory (no (profile, cwd) anchor, no profile_route). The write is just the
# per-session override file that core/profile.read_override consumes; every push
# hook (recall / gate / sweep / runs / assets …) already honors it.
#
# WHY A HOOK. The override is keyed by the session id, and the writer needs it. On a
# host that expands a session-id token in a command the model runs, the command's
# CLI helper self-scopes and this hook is NOT wired (registry `skip_if_cap`). On a
# host that does not — the session id arrives ONLY on the hook payload (the Event) —
# the command still tells the model to run the same helper, and THIS PreToolUse hook
# intercepts that call: it reads the session id from the Event, parses the target
# profile from the command's flags, writes the override, and BLOCKS the helper (it
# would otherwise run without a usable session id and fail confusingly). Capability-
# keyed, host-name-clean: nothing here names a host.
#
# Pure stdlib. Reuses core/profile.py for the store (same file the reader consumes).

from core import cmdline, profile as profile_mod
from core.hostapi import Event, Host, Output

# The command-side helper the /switch-profile command tells the model to run. We
# match on its basename so any absolute/relative invocation is recognized.
HELPER = "switch_profile.py"


def _parse_command(command):
    """Return the intent of a genuine switch-profile helper INVOCATION, or None when the
    command is not one (so the tool is left to run untouched):
        ('switch', '<profile>') | ('reset', '')
    Only a real invocation (the helper is the exec target — see cmdline.invokes_helper)
    is intercepted; a command that merely mentions switch_profile.py as an argument is
    left alone. Reset ONLY on an explicit `--reset` (never inferred from a missing
    --profile, which would silently clear the override on an argless invocation).
    Tolerant of quoting; a session-id flag (unexpanded or not) is ignored — the id comes
    from the Event."""
    tokens = cmdline.helper_invocation(command, HELPER)
    if tokens is None:
        return None

    profile = ""
    reset = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--reset":
            reset = True
        elif tok == "--profile":
            if i + 1 < len(tokens):
                profile = tokens[i + 1].strip()
                i += 1
        elif tok.startswith("--profile="):
            profile = tok.split("=", 1)[1].strip()
        i += 1

    if reset:
        return ("reset", "")
    if profile:
        return ("switch", profile)
    return None  # a genuine invocation but no actionable flag (e.g. --help) — leave it


def handle(event: Event, host: Host):
    # The shared installable hook bundle includes this interceptor for the host
    # that needs it. A host that substitutes its session id runs the helper itself.
    if host.caps.substitutes_session_id:
        return None
    tool_input = event.tool_input or {}
    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    parsed = _parse_command(command)
    if parsed is None:
        return None  # not a switch-profile invocation — let the tool run

    action, profile = parsed
    session_id = (event.session_id or "").strip()
    if not session_id:
        msg = ("switch-profile: no session id on the hook payload — the switch "
               "cannot be scoped to a session, so it was not applied.")
        return Output(deny=True, deny_reason=msg, system_message=msg)

    live = bool(host.caps.live_reload_asset_kinds)
    if action == "reset":
        existed = profile_mod.clear_override(session_id)
        if existed:
            # Reconcile the directory/env profile immediately, and say what THIS
            # host will actually re-scan (see core/reload) rather than a generic
            # "hosts with live asset reload", which is true nowhere in particular.
            from core import assets, reload as reload_core
            assets.handle(event, host)
            msg = ("switch-profile: reset — this session reverts to its directory/env "
                   "profile. Its assets were reconciled on disk. "
                   + reload_core.rescan_sentence(host))
        else:
            msg = "switch-profile: no override was set; nothing to reset."
        # The rescan sentence PROMISES a live re-scan, so this result must carry the
        # directive that delivers it — and only when something was actually written.
        return Output(deny=True, deny_reason=msg, system_message=msg,
                      reload_assets=live and existed)

    try:
        profile_mod.write_override(session_id, profile)
    except ValueError as exc:
        msg = f"switch-profile: {exc}; not applied."
        return Output(deny=True, deny_reason=msg, system_message=msg)

    # The profile route has changed now, not at the next SessionStart. Materialize
    # its assets in the same hook so a host-native rescan can pick them up as soon
    # as this intercepted helper call completes.
    from core import assets, reload as reload_core
    assets.handle(event, host)

    # The caveat is kind-specific AND host-specific, and must NOT be glossed as
    # "live reload exposes them": a kind this host does not re-scan is reconciled
    # on disk but NOT invocable in this process, and saying otherwise sends the
    # user hunting for a /command that isn't there yet. core/reload owns the one
    # wording, from the host's own capabilities, so /switch-profile and
    # /reload-assets can never disagree about what just happened.
    msg = (
        f'switch-profile: this session is now scoped to profile "{profile}". '
        "No cwd->profile anchor/route was created; a fresh session in this "
        "directory resolves normally. Thread profile=\"" + profile + "\" on your "
        "own neuronzai MCP tool calls for the rest of this session. Profile assets "
        "were reconciled on disk immediately. " + reload_core.rescan_sentence(host)
    )
    return Output(deny=True, deny_reason=msg, system_message=msg, context=msg,
                  reload_assets=live)
