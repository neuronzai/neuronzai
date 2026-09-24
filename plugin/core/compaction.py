#!/usr/bin/env python3
"""Invalidate the server's per-session DELIVERY HISTORY after a compaction.

A compaction summarizes away whatever standing context sits in the window — the
rules, the persona, the records already injected — while the session id survives
it. Every cadence the server keeps for that session is therefore wrong the moment
it completes: the rules pointer's fetched-state names rules this session no longer
holds, the pre-tool gate cadence assumes full texts that are gone, and the recall
seen-map suppresses hits nothing in context mentions any more. POST
/api/memory/compacted is the one signal that clears all of it (and rotates the
pointer's delivery token, so a token minted before the compaction can never
populate the new generation), letting the next prompt re-deliver from scratch.

WHICH lifecycle moment fires it is a CAPABILITY question, and the two answers are
not interchangeable:

  * A host that reports a COMPLETED compaction (caps.signals_completed_compaction)
    fires it from that report and NEVER from its pre-compaction hook. A
    pre-compaction hook is a REQUEST, not an outcome — on at least one host it can
    be canceled outright — and clearing there would rotate the token and re-push
    everything for a compaction that never happened, in a window where the agent's
    context is still perfectly intact.
  * A host whose only compaction signal is that pre-compaction hook has no later
    moment to use, so it fires there and accepts that a canceled compaction costs
    ONE redundant full re-delivery. That is the conservative direction on purpose:
    clearing slightly too early costs a turn of duplicated context, while never
    clearing leaves an agent convinced it still holds rules the compaction erased.

Repeated signals leave held-state empty, but each rotates the delivery generation.
Hosts reporting completion use that event alone to avoid invalidating fresh reads.

Pure stdlib, best-effort, never raises: a compaction must not fail because the
server is unreachable.
"""

from core import api, profile as profile_mod


def invalidate(event):
    """POST /api/memory/compacted for this session. Best-effort, returns nothing."""
    session_id = str(event.session_id or "").strip()
    if not session_id:
        # The server keys the one-shot flag on the session id; with no session
        # there is nothing to key it to and nothing to invalidate.
        return
    # Same resolution every other handler uses, so the delivery state cleared is
    # the one the next recall reads: a /switch-profile session resolves to its
    # OVERRIDE profile (and sends no cwd, so nothing maps the directory to it),
    # and only a session without an explicit profile falls back to the cwd leg.
    profile, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)
    params = {"session": session_id}
    if not profile and cwd:
        params["cwd"] = cwd
    api.post(
        "/api/memory/compacted",
        params=params,
        profile=profile,
        session_id=session_id,
        where="memory_compacted",
    )
