#!/usr/bin/env python3
# Shared session -> profile OVERRIDE store for the Neuronz.ai hooks.
#
# `/switch-profile <name>` lets a user RE-SCOPE the CURRENT Claude Code session
# to a different profile mid-flight -- e.g. they launched Claude in the wrong
# directory and the cwd matched (or auto-created) the wrong profile. The override
# is SESSION-scoped (keyed by the Claude Code session id) and lives in a small
# local file so every hook subprocess can read it the moment it is set.
#
# It is DELIBERATELY NOT a (profile, cwd) anchor / profile_route: switching does
# NOT map the cwd to the new profile (no DB write, nothing persists past the
# session). Hooks with an active override send X-Profile=<name> and OMIT the cwd
# entirely (see neuronzai_cwd.session_scope), so the server takes the name as-is
# and writes no anchor and creates no route. A fresh session in the same
# directory resolves the cwd's real profile again. Keying by session id also
# means a `--resume` of the SAME session keeps its switched profile.
#
# Precedence in the hooks: this override (highest) > NEURONZAI_PROFILE env > cwd.
#
# The store lives under ${NEURONZAI_STATE_DIR:-~/.neuronzai}/session-profiles/,
# one <session_id>.json per re-scoped session. Pure stdlib; fails SAFE: a
# missing/unreadable/garbled store yields None so the hooks fall back to their
# normal env/cwd resolution and never block.

import json
import os
import time

# An un-substituted command template (a CLI that didn't expand ${CLAUDE_SESSION_ID})
# must be treated as "no session id", never as a literal key.
UNSUBSTITUTED_SESSION_ID = "${CLAUDE_SESSION_ID}"


def state_dir():
    base = os.environ.get("NEURONZAI_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".neuronzai"
    )
    return os.path.join(base, "session-profiles")


def _path(session_id):
    """Filesystem path of the override for `session_id`, or None when there is no
    usable session id. Keeps only filename-safe chars so a weird value can never
    escape the store directory."""
    sid = (session_id or "").strip()
    if not sid or sid == UNSUBSTITUTED_SESSION_ID:
        return None
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    if not safe:
        return None
    return os.path.join(state_dir(), safe + ".json")


def read_profile(session_id):
    """The override profile for this session, or None. Never raises."""
    path = _path(session_id)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    profile = str(data.get("profile") or "").strip()
    return profile or None


def write_profile(session_id, profile):
    """Set THIS session's override profile, atomically. Returns the path written.
    Raises ValueError on a missing/unsubstituted session id (the caller surfaces
    that) or an empty profile -- the switch is meaningless without both."""
    path = _path(session_id)
    if not path:
        raise ValueError("a real session id is required to scope the switch")
    name = (profile or "").strip()
    if not name:
        raise ValueError("a non-empty profile name is required")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = {"profile": name, "ts": int(time.time())}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(body, handle)
    os.replace(tmp, path)
    prune()
    return path


def clear_profile(session_id):
    """Remove THIS session's override (revert to env/cwd). True if one existed."""
    path = _path(session_id)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def prune(max_age_days=14):
    """Best-effort delete of override files older than max_age_days, so the store
    never grows without bound (session ids are unique and never recur). Silent on
    any error -- pruning is housekeeping, never load-bearing."""
    cutoff = time.time() - max_age_days * 86_400
    directory = state_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        full = os.path.join(directory, name)
        try:
            if os.path.getmtime(full) < cutoff:
                os.remove(full)
        except OSError:
            pass
