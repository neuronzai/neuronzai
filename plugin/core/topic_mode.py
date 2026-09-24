#!/usr/bin/env python3
# PostToolUse handler (host-agnostic) — keep the PER-SESSION active-topic pointer
# in sync with the agent's enter_topic / exit_topic MCP calls. This is the single
# lifecycle implementation dispatched by entry.py for every host.
#
# WHY THIS EXISTS: a host's MCP transport may carry NO session id to the server, so
# an enter_topic that arrives over /mcp can't be scoped to a session server-side.
# A PostToolUse firing DOES carry the session id on its Event, so this handler
# re-issues the call against the server WITH the X-Session-Id header — that is what
# makes topic mode per-session (so two sessions in one repo don't clobber each
# other).
#
#   enter_topic: POST /api/topics/<id>/enter WITH X-Session-Id (+ X-Cwd) — writes
#     this session's PRESENCE row (the agent-facing active topic + the dashboard
#     "active now" marker, #156) + the repo-level (profile, cwd) anchor. #41.
#   exit_topic: POST /api/topics/exit WITH X-Session-Id (+ X-Cwd) — the explicit
#     "I'm done": clears the (profile, cwd) anchor + soft-ends this session's
#     presence.
#
# Best-effort: any failure returns None and never blocks (api.post never raises).
#
# It also carries the TOPIC BADGE for hosts that take one from a hook result
# (Output.status_badge). This is the enter/exit moment and it already holds the
# server's answer, so the badge costs no extra request. A host whose status line is
# a COMMAND IT POLLS is not on this path: there the badge is composed inside that
# command from the live presence aggregate, and this field is ignored.
# Pure stdlib.

from core import api, profile as profile_mod
from core.hostapi import Event, Host, Output

# Clamped for the same reason the polled status-line wrapper clamps (its own
# TITLE_MAX): a status surface is re-rendered constantly and a long title would push
# the rest of the bar off screen. The two composers are deliberate twins — that
# wrapper is copied into a config home the user owns and cannot import core.
TITLE_MAX = 48


def badge(title: str) -> str:
    """`🎯 <title>` for a topic title, or "" when there is nothing to show.

    Strips control bytes first: a topic title is user text rendered straight into a
    terminal, so an escape sequence in one would otherwise reach the TUI."""
    clean = "".join(
        ch for ch in title if ch >= " " and ch != "\x7f" and not ("\x80" <= ch <= "\x9f")
    ).strip()
    if not clean:
        return ""
    if len(clean) > TITLE_MAX:
        clean = clean[: TITLE_MAX - 1].rstrip() + "\u2026"
    return f"\U0001f3af {clean}"


def handle(event: Event, host: Host):
    """Re-issue the agent's enter_topic / exit_topic against the server WITH this
    session's X-Session-Id (+ X-Cwd), so topic mode is scoped per session. Returns
    an Output carrying only the topic badge — set on an enter the server confirmed,
    cleared on a confirmed exit, and None (leave the badge alone) whenever the call
    did not land, so the badge never claims a topic mode the server does not hold.
    Exits silently on a non-topic tool call, a missing session id, an unresolvable
    profile, or an enter with no topic id."""
    tool_name = event.tool_name or ""
    is_enter = tool_name.endswith("enter_topic")
    is_exit = tool_name.endswith("exit_topic")
    if not (is_enter or is_exit):
        return None

    session_id = (event.session_id or "").strip()
    if not session_id:
        return None  # no session to scope to — leave the pointer alone

    # Resolve the SAME (profile, cwd) the server middleware would, honoring the
    # enter/exit call's OWN scope args. Precedence (mirrors the server middleware +
    # the legacy scope_headers): session override > explicit profile=/cwd= tool arg
    # > NEURONZAI_PROFILE env > cwd. enter_topic/exit_topic UNIQUELY accept explicit
    # profile/cwd args (a topic can live in another profile), so — unlike the generic
    # resolver — this handler must prefer them; collapsing to resolve() silently
    # dropped that arg and re-scoped a cross-profile enter to the session's own cwd.
    # Under a /switch-profile override we scope to the switched profile and send NO
    # X-Cwd (no durable (profile, cwd) anchor) — the switch stays session-scoped; the
    # presence row is still scoped to THIS session via X-Session-Id below.
    tool_input = event.tool_input or {}
    override = profile_mod.read_override(event.session_id)
    if override:
        prof, cwd = override, ""
    else:
        prof = str(tool_input.get("profile") or "").strip() or (api.ENV_PROFILE or "").strip()
        cwd = str(tool_input.get("cwd") or "").strip() or profile_mod.canonical_cwd(event.cwd)
    if not (prof or cwd):
        return None  # couldn't resolve a profile
    if is_enter:
        topic_id = str(tool_input.get("id") or "").strip()
        if not topic_id:
            return None
        path = f"/api/topics/{topic_id}/enter"
    else:
        path = "/api/topics/exit"

    # Send X-Profile AND X-Cwd when both resolved (NOT mutually exclusive): X-Profile
    # scopes the profile, X-Cwd carries the working dir the (profile, cwd) anchor is
    # keyed on (#41). Under an override cwd="" so only X-Profile goes out.
    extra = {"X-Cwd": cwd} if cwd else None
    answer = api.post(path, body={}, profile=prof, session_id=session_id,
                      where="topic_mode_sync", extra_headers=extra)
    if answer is None:
        return None  # the call did not land — say nothing about the badge
    if not is_enter:
        return Output(status_badge="")
    active = answer.get("active") if isinstance(answer, dict) else None
    text = badge(str(active.get("title") or "")) if isinstance(active, dict) else ""
    return Output(status_badge=text) if text else None
