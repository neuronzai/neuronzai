#!/usr/bin/env python3
# Stop-fallback DEBOUNCE for the end-of-session handlers on a host WITHOUT a distinct
# session-end event (host.caps.has_session_end is False). Topic 90ae46be.
#
# On such a host the end-of-session hooks (run recording, usage analytics, topic
# presence-end) are retargeted to the per-turn Stop event (the registry's
# fallback_event). Stop fires on EVERY turn and the transcript only GROWS between
# turns, so a content hash can never suppress the re-fire (issue #189, the sweep
# bound). Left unguarded these would POST every turn: DUPLICATE run actions, INFLATED
# adoption analytics, a presence row re-ended each turn — the server dedups none of
# these. This latch collapses the per-turn Stops into ONE fire per (session, family):
# the first Stop of a session fires and records a mark; every later Stop for that
# family matches the mark and is suppressed.
#
# The CALLER gates on the firing event (only the per-turn Stop fallback consults this,
# never a real session-end), so a host WITH a session-end event keeps byte-identical
# single-fire behavior — mirrors the sweep handler's `event.event == "stop"` guard.
#
# Trade-off (honest, and manifest-declared `degraded`): with no true end-of-session
# signal the guard fires on the FIRST Stop, so a long interactive session's later
# turns aren't captured; a one-shot `exec`-style run (a single Stop) is captured in
# full. Duplicates — which corrupt the ledger permanently — are strictly avoided. An
# optional wall-clock RE-ARM window (NEURONZAI_STOP_REARM_S, default 0 = never re-arm)
# lets a very long session fire again after the window; default keeps it once/session.
# Pure stdlib; never raises.

import json
import os
import time

# Default: never re-arm within a session (fire exactly once per session/family). A
# positive value turns this into a min-wall-clock throttle (fire at most once per that
# many seconds), for tuning / tests.
DEFAULT_REARM_S = 0.0


def _rearm_interval_s():
    # Resolved per call (not a module constant) so a test / env change is live.
    raw = os.environ.get("NEURONZAI_STOP_REARM_S")
    if raw is None or not str(raw).strip():
        return DEFAULT_REARM_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_REARM_S


def _marks_dir():
    base = os.environ.get("NEURONZAI_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".neuronzai")
    return os.path.join(base, "stop-marks")


def _mark_path(session_id, family):
    safe_sid = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    safe_fam = "".join(c for c in (family or "") if c.isalnum() or c in "-_")
    if not safe_sid or not safe_fam:
        return None
    return os.path.join(_marks_dir(), f"{safe_fam}.{safe_sid}.json")


def _prune(max_age_days=14):
    """Best-effort delete of stop marks older than max_age_days so the store never
    grows without bound (session ids are unique and never recur, so each leaves up to
    three permanent marks otherwise). Silent on any error — housekeeping, never
    load-bearing. Mirrors core.profile._prune for the sibling session-profiles store."""
    cutoff = time.time() - max_age_days * 86_400
    try:
        names = os.listdir(_marks_dir())
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        full = os.path.join(_marks_dir(), name)
        try:
            if os.path.getmtime(full) < cutoff:
                os.remove(full)
        except OSError:
            pass


def should_fire(session_id, family):
    """Return True at most ONCE per (session_id, family) — the first call for a session
    fires (records the wall-clock mark) and every later call is suppressed (returns
    False). Returns False when there is no session key to debounce on (better to skip
    than to POST every turn). With NEURONZAI_STOP_REARM_S set, re-arms after that many
    seconds instead of latching for the whole session. Never raises."""
    path = _mark_path(session_id, family)
    if not path:
        return False
    interval = _rearm_interval_s()
    now = time.time()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            last = float(json.load(fh).get("t") or 0)
        # A prior fire is on record: suppress unless a positive re-arm window elapsed.
        if interval <= 0 or (now - last) < interval:
            return False
    except (OSError, ValueError):
        pass  # no readable mark yet -> this is the first fire
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"t": now}, fh)
    except OSError:
        pass
    _prune()  # housekeeping: drop marks from long-gone sessions (never load-bearing)
    return True
