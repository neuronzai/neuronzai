#!/usr/bin/env python3
# Stop handler (host-agnostic) — in-session memory self-consolidation. This is the
# single lifecycle implementation dispatched by entry.py for capable hosts.
#
# On each turn-end the agent is ABOUT to stop; this asks the server
# (GET /api/memory/selfsweep/check) whether enough un-swept session activity has
# accumulated to warrant ONE forced consolidation turn. When the server answers
# {block:true, reason:"<instruction>"} we inject that instruction as
# additionalContext so the in-session model runs a forced continuation and writes
# the session's durable facts itself — on the user's subscription, with NO backend
# sweep and NO GPU. Otherwise we return None and the agent stops normally.
#
# Loop guard: the payload's `stop_hook_active` is true on the Stop that fires at
# the END of a forced continuation. When set we return immediately with NO network
# call, so a forced turn is never re-blocked. The server also advances a
# per-session cursor whenever it blocks, so a later natural stop won't
# re-consolidate the same delta. (`stop_hook_active` is not a normalized Event
# field, so it is read from the raw payload escape hatch.)
#
# Strictly best-effort: a down server / auth / timeout yields None (agent stops
# normally) — a memory gate must never trap the user mid-session.

from core import api, profile as profile_mod, sweepturn
from core.hostapi import Event, Host, Output


def handle(event: Event, host: Host):
    """Return an Output (inject the forced-continuation instruction) or None
    (silent — the agent stops normally)."""
    # Some hosts accept Stop output only as a warning and cannot use injected
    # context to force another model turn. Do not emit a payload they reject.
    if not host.caps.continues_from_stop_context:
        return None

    # The Stop at the END of a forced continuation carries stop_hook_active=true —
    # never block twice in a row (the hard loop guard). A host that omits the flag
    # falls through to the network check, which the server-side cursor still guards.
    if event.raw.get("stop_hook_active"):
        # This IS the end of the forced turn, and the only end signal a turn that
        # ran to completion produces. Containment ends with it (#549) — leaving the
        # latch armed would block the user's own next call.
        sweepturn.disarm(event.session_id)
        return None

    if not event.session_id:
        return None

    # Profile resolution mirrors the other memory handlers, precedence:
    #   /switch-profile session override > NEURONZAI_PROFILE env > cwd.
    # Under an override resolve() returns cwd="" so no ?cwd= is sent (no anchor)
    # and the in-session self-consolidation writes the switched profile's facts.
    prof, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)
    params = {"session": event.session_id}
    if not prof and cwd:
        params["cwd"] = cwd

    data = api.get_json("/api/memory/selfsweep/check", params=params, profile=prof,
                        where="memory_selfsweep")

    # A server {block:true, reason:"<instruction>"} forces the consolidation turn;
    # the exact instruction text is handed back verbatim as additionalContext, with
    # a short user-facing status line co-emitted (the host renders both).
    if isinstance(data, dict) and data.get("block") and isinstance(data.get("reason"), str):
        # Arm containment BEFORE the turn starts: the next thing that happens is the
        # model re-entering the SAME conversation, and the PreToolUse gate is what
        # keeps it from resuming the work it finds there (core/sweepturn.py).
        sweepturn.arm(event.session_id, data["reason"])
        return Output(context=data["reason"], system_message="🧠 Updating memory…")
    return None
