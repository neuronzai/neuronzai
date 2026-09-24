#!/usr/bin/env python3
# SessionEnd handler (host-agnostic) — usage-analytics capture for the skills
# NEURONZAI itself distributes. This is the single lifecycle implementation
# dispatched by entry.py for every host.
#
# Scans the session transcript for `Skill` invocations, keeps only the ones we
# shipped (never the user's own skills), and batches them to
# POST /api/analytics/events. This is the numerator of the BI "adoption ratio"
# (the denominator is the per-session `skill_available` events the server records
# at /api/skills/materialize). Metadata only — the slug and the session id, never
# prompt text or tool args.
#
# The transcript STRUCTURE is read through host.iter_transcript(), which yields
# normalized {role, texts, tool_uses:[{name, input}]} entries — so this handler
# never opens or parses the raw transcript file itself. Ownership is decided from
# host.config_home() + the neuronzai store subdirs (host-agnostic: whatever the
# host's config home, we symlink into <config_home>/skills and our store lives at
# <config_home>/neuronzai-assets/<profileKey>/skills), never a hardcoded config dir.
#
# Strictly best-effort, fire-and-forget: runs as the session ends, so it must
# NEVER block. api.post never raises; a down server / timeout returns None silently
# and an auth failure prints to stderr. Kill switch: NEURONZAI_ANALYTICS=0.

import os
from pathlib import Path
from typing import Optional

from core import api, profile as profile_mod, progress
from core.hostapi import Event, Host, Output
from core.assets import effective_target_dir

# Kill switch: NEURONZAI_ANALYTICS=0 (or false/no) disables usage capture.
ENABLED = (os.environ.get("NEURONZAI_ANALYTICS") or "1").strip().lower() not in ("0", "false", "no")

MAX_EVENTS = 500  # the server caps the ingest batch at 500 events


def is_neuronzai_skill(slug, host, cwd=""):
    """True iff the skill link the materializer wrote is a symlink resolving INTO one of
    our skill-store roots — the per-profile neuronzai-assets/<profileKey>/skills layout
    or the legacy flat stores (kept for sessions whose symlinks predate the rename) —
    the exact ownership gate the materialize hook writes with. The LINK dir is
    host+cwd-specific (a project-scoped host puts it in a repo-local dir), so resolve it
    through the SAME effective_target_dir the materializer uses rather than rebuilding
    config_home/skills. Scopes capture to OUR distributed skills only. Never raises."""
    home = Path(host.config_home())
    target_dir = effective_target_dir(host, "skill", cwd)
    if not target_dir:
        return False
    link = target_dir / slug
    try:
        if not link.is_symlink():
            return False
        target = link.resolve()
        legacy = (home / "neuronzai-skills").resolve()
        if target == legacy or legacy in target.parents:
            return True
        root = (home / "neuronzai-assets").resolve()
        parts = target.relative_to(root).parts
        return bool(parts and (parts[0] == "skills" or (len(parts) > 1 and parts[1] == "skills")))
    except ValueError:
        return False
    except OSError:
        return False


def _iter_skill_slugs(event: Event, host: Host):
    """Yield the slug of every `Skill` tool_use in the session transcript, via the
    host's normalized iterator (host.iter_transcript is tolerant of malformed lines
    and never raises)."""
    if not event.transcript_path:
        return
    for entry in host.iter_transcript(event.transcript_path):
        for tool_use in entry.get("tool_uses") or []:
            if tool_use.get("name") != "Skill":
                continue
            slug = str((tool_use.get("input") or {}).get("skill") or "").strip()
            if slug:
                yield slug


def handle(event: Event, host: Host) -> Optional[Output]:
    """Collect this session's OWN-skill invocations and POST them as adoption
    analytics. Returns None (side-effect only, no model-visible output)."""
    if event.event == "stop" and host.caps.has_session_end:
        return None
    if not ENABLED:
        return None
    session_id = event.session_id
    # We need BOTH: the session id is the BI key (distinct-session denominator) and
    # the transcript is where the invocations live.
    if not session_id or not event.transcript_path:
        return None

    slugs = []
    for slug in _iter_skill_slugs(event, host):
        if not is_neuronzai_skill(slug, host, event.cwd):
            continue  # only OUR distributed skills — never the user's own
        slugs.append(slug)
        if len(slugs) >= MAX_EVENTS:
            break
    if not slugs:
        return None  # nothing of ours ran this session

    counts = None
    if event.event == "stop" and not host.caps.has_session_end:
        # The transcript grows at every turn. Send only invocation-count deltas so
        # later skills are not lost and earlier ones are not double-counted.
        slugs, counts = progress.unseen(session_id, "skills-usage", slugs)
        if not slugs:
            return None
    events = [{"sessionId": session_id, "slug": slug} for slug in slugs]

    # Profile resolution mirrors the other hooks: /switch-profile session override >
    # NEURONZAI_PROFILE env > cwd. Under an override resolve() returns cwd="" (no
    # anchor/route), so the ?cwd= leg only fires when there's no explicit profile.
    profile, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)
    params = {"cwd": cwd} if (not profile and cwd) else None
    response = api.post("/api/analytics/events", params=params, body={"events": events},
                        profile=profile, where="skills_usage")
    if counts is not None and response is not None:
        progress.commit(session_id, "skills-usage", counts)
    return None
