#!/usr/bin/env python3
# SessionEnd handler (host-agnostic) — end this session's topic-mode PRESENCE when
# the session ends. This is the single lifecycle implementation dispatched by
# entry.py for hosts with a native SessionEnd.
#
# Calls POST /api/topics/presence/end (with this session's X-Session-Id): the
# server SOFT-ENDS this session's presence row (expiresAt = now — the parallel
# SessionEnd sweep can still resolve the topic within its grace window) WITHOUT
# evicting any concurrent session's presence, then GCs the (profile, cwd) anchor
# once its topic has no live session left. Topic mode ends WITH the session
# (#156): the next session — including a resume, which mints a new session id —
# starts clean and re-enters explicitly.
#
# Best-effort: any failure (server down, timeout, no resolvable profile) returns
# None with no output and never blocks session end (api.post never raises). Pure
# stdlib.

from core import api, profile as profile_mod
from core.hostapi import Event, Host, Output  # noqa: F401 (Output kept for signature parity)


def handle(event: Event, host: Host):
    """POST this session's presence-end. Returns None always (pure side effect —
    no injected context). Exits silently when no profile/cwd resolves."""
    # Precedence: /switch-profile session override > NEURONZAI_PROFILE env > cwd.
    # Under an override cwd="" so we send X-Profile only (no anchor touched); the
    # presence-end is scoped to THIS session via X-Session-Id below regardless.
    prof, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)
    session_id = (event.session_id or "").strip()
    if not (prof or cwd):
        return None  # can't resolve a profile — nothing to exit

    # SessionEnd-ONLY — deliberately NO per-turn Stop fallback (see registry.py). Ending
    # presence GCs the (profile, cwd) anchor once no live presence remains, so firing on a
    # mid-session Stop would collapse topic mode (recall/current_topic read the anchor). On
    # a host without a session-end event this handler stays unwired and the session's
    # presence expires via its 24h TTL instead — unlike runs-record/skills-usage, which
    # only CAPTURE and are safe to fire once on the Stop fallback.

    # Header selection mirrors the legacy hook: X-Profile when a profile resolved,
    # ELSE X-Cwd — mutually exclusive here (XOR), unlike the enter/exit sync which
    # can send both.
    send_profile = ""
    extra = None
    if prof:
        send_profile = prof
    elif cwd:
        extra = {"X-Cwd": cwd}

    # Scope to THIS session's presence row so one session ending never clears a
    # sibling session's presence (and never touches the durable anchor). No session
    # id -> no X-Session-Id header (api.headers omits it), matching the legacy hook.
    api.post("/api/topics/presence/end", body={}, profile=send_profile,
             session_id=session_id, where="topic_exit", extra_headers=extra)
    return None
