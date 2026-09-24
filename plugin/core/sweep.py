#!/usr/bin/env python3
"""Flush the session tool ledger at lifecycle boundaries.

Fact capture belongs to the user's agent: the in-session self-sweep handles
meaningful stops and detached capture closes the remaining SessionEnd delta.
This handler only asks the server to turn each profile-owned PostToolUse receipt
ledger into its own auditable action. It never reads or uploads the conversation
transcript.

It is also the PRE-COMPACTION fire, which makes it the last moment some hosts
give us before a compaction — so on a host with no completed-compaction report
(caps.signals_completed_compaction False) it carries the delivery-history
invalidation too. See core/compaction for why that is the conservative lane and
why a host that DOES report completion must not use it.
"""

from core import api, compaction, profile as profile_mod
from core.hostapi import Event, Host


def handle(event: Event, host: Host):
    """Flush the session's profile-owned ledgers without model-visible output."""
    if event.event == "stop" and host.caps.has_session_end:
        return None
    if not event.session_id:
        return None

    if event.event == "pre_compact" and not host.caps.signals_completed_compaction:
        # This host will never tell us the compaction finished, so invalidate here or
        # never. Before the flush: a compaction that starts mid-request must not find
        # the session still being served its pre-compaction delivery state.
        compaction.invalidate(event)

    profile, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)
    params = {"cwd": cwd} if (not profile and cwd) else None
    extra = {api.SWEEP_PROTOCOL_HEADER: api.SWEEP_PROTOCOL_VERSION}
    if cwd:
        extra["X-Cwd"] = cwd
    api.post(
        "/api/memory/sweep",
        params=params,
        body={"session": event.session_id},
        profile=profile,
        session_id=event.session_id,
        timeout=15,
        where="memory_sweep",
        extra_headers=extra,
    )
    return None
