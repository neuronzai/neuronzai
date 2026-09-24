#!/usr/bin/env python3
"""Small persistent cursors for append-only per-session fallbacks."""

import json
import os
import tempfile
import time
from collections import Counter


def _path(session_id, family):
    base = os.environ.get("NEURONZAI_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".neuronzai"
    )
    safe_session = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    safe_family = "".join(c for c in (family or "") if c.isalnum() or c in "-_")
    if not safe_session or not safe_family:
        return None
    return os.path.join(base, "progress", f"{safe_family}.{safe_session}.json")


def _prune(directory, max_age_days=14):
    """Best-effort cleanup for cursors belonging to long-gone sessions."""
    cutoff = time.time() - max_age_days * 86_400
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


def unseen(session_id, family, items):
    """Return (new_items, current_counts) against the last committed multiset.

    Transcripts append, so counts only grow. If a transcript is replaced or
    truncated, negative deltas are ignored and the current counts become the new
    checkpoint after a successful caller side effect.
    """
    path = _path(session_id, family)
    previous = Counter()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh).get("counts") or {}
            previous.update({str(k): max(0, int(v)) for k, v in raw.items()})
        except (OSError, TypeError, ValueError):
            pass

    current = Counter(str(item) for item in items if str(item))
    consumed = Counter()
    delta = []
    for item in items:
        item = str(item)
        if not item:
            continue
        consumed[item] += 1
        if consumed[item] > previous[item]:
            delta.append(item)
    return delta, dict(current)


def commit(session_id, family, counts):
    path = _path(session_id, family)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".progress-", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"counts": counts}, fh, sort_keys=True)
        os.replace(tmp, path)
        _prune(os.path.dirname(path))
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, UnboundLocalError):
            pass
