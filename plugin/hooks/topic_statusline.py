#!/usr/bin/env python3
# Status line command — render a compact "topic mode" badge so the HUMAN can see,
# persistently in the terminal, that THIS session is working under a Neuronz.ai
# topic (#40). The agent is already re-reminded every prompt (core/recall.py
# injects the active-topic line); this is the missing HUMAN-visible signal.
#
# Claude Code pipes the status-line payload (JSON) on stdin — we read `session_id`
# and `cwd` from it — and renders whatever we print on stdout as the status line.
# We call GET /api/topics/active-sessions (the per-session presence aggregate the
# dashboard uses) and match our OWN session_id and, when a topic is active for THIS
# session, print `🎯 <title>` (plus `· since HH:MM` from enteredAt); otherwise we
# print nothing. Output is plain text (no ANSI) so it composes cleanly when a user
# prepends/appends it to their own status line.
#
# The status line re-renders VERY frequently (after every assistant message,
# debounced ~300ms, plus any refreshInterval), so we MUST NOT hit the API on each
# render: the result is CACHED on disk per session for a few seconds
# (NEURONZAI_STATUSLINE_TTL, default 8s) — a cache hit prints with no network call.
#
# Strictly FAIL-OPEN: a short timeout (<=1s), and ANY error / timeout / missing
# env / missing session id prints NOTHING and exits 0 — the status line is never
# blocked and never shows a traceback. On error we cache an empty result so a down
# server is retried at most once per TTL, not on every render.
#
# This is NOT a hook: a status line is configured under the settings.json
# `statusLine` key (not `hooks`), and a Claude Code plugin CANNOT declare one — so
# the user wires it once by hand (see plugin/README.md → "Topic-mode status line").
# Reads the shared saved OAuth grant, with environment credentials and profile
# settings as compatibility overrides, exactly like the hooks. Pure stdlib
# Python 3 (urllib/json/os/sys) — clients need no Bun install.

import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from neuronzai_cwd import session_cwd, session_scope
from core import auth as plugin_auth

NEURONZAI_URL = plugin_auth.base_url()
PROFILE = (os.environ.get("NEURONZAI_PROFILE") or "").strip()

TIMEOUT = 1.0  # seconds — a status line must never block the terminal
TITLE_MAX = 48  # clamp the badge so it stays compact across re-renders


def _int_env(name, default):
    try:
        return int(float(os.environ[name]))
    except (KeyError, ValueError):
        return default


# Seconds a fetched result is reused before we touch the API again. The status
# line re-renders many times a second; this is what keeps it off the network.
TTL = max(2, _int_env("NEURONZAI_STATUSLINE_TTL", 8))


def cache_path(session_id):
    # Hash the session id into a safe, fixed filename in the system temp dir.
    key = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"neuronzai-statusline-{key}.json")


def read_cache(path):
    """The cached rendered text if still fresh (possibly ""), else None (= refetch)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.load(fh)
        if (time.time() - float(entry["ts"])) < TTL:
            return str(entry.get("text") or "")
    except Exception:
        pass  # missing / stale / corrupt -> treat as a miss
    return None


def write_cache(path, text):
    # Cache even "" (a no-topic session or a transient error) so we back off for
    # the TTL instead of re-fetching on every render. Atomic via a temp + replace
    # so a concurrent reader never sees a half-written file. Best-effort.
    try:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "text": text}, fh)
        os.replace(tmp, path)
    except Exception:
        pass  # a cache-write failure just means we re-fetch next render


def sanitize_title(title):
    """Strip control bytes — blocks terminal-escape / OSC-hyperlink injection via a
    topic title — and clamp to a compact width."""
    clean = "".join(
        ch for ch in title if ch >= " " and ch != "\x7f" and not ("\x80" <= ch <= "\x9f")
    ).strip()
    if len(clean) > TITLE_MAX:
        clean = clean[: TITLE_MAX - 1].rstrip() + "…"
    return clean


def render(active):
    """The badge for an active-topic object, or "" when nothing is active."""
    if not isinstance(active, dict):
        return ""
    title = sanitize_title(str(active.get("title") or ""))
    if not title:
        return ""
    badge = f"\U0001f3af {title}"
    entered = active.get("enteredAt")  # unix SECONDS on the ActiveTopic pointer
    try:
        if entered:
            badge += " · since " + time.strftime("%H:%M", time.localtime(float(entered)))
    except (TypeError, ValueError):
        pass
    return badge


def fetch(session_id, payload):
    """THIS session's active-topic dict (or None). Reads the PER-SESSION presence
    aggregate (GET /api/topics/active-sessions — the exact source the dashboard's
    "active now" view uses) and matches our OWN session_id, so the badge reflects
    only a topic THIS session entered. Was GET /api/topics/mode, which resolves the
    (profile, cwd) ANCHOR — shared by every session in the repo (#41) — so the badge
    bled onto sibling sessions that never entered topic mode."""
    headers = {}
    token = plugin_auth.cached_access_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Resolve the SAME profile the agent's enter_topic scoped to, with precedence:
    # /switch-profile session override > NEURONZAI_PROFILE env > cwd (X-Cwd
    # canonicalized to the git main-worktree root). Under an override the badge
    # reflects the switched profile and sends no X-Cwd — matching the other hooks.
    profile, cwd = session_scope(payload, PROFILE)
    if profile:
        headers["X-Profile"] = profile
    elif cwd:
        headers["X-Cwd"] = cwd

    req = urllib.request.Request(
        f"{NEURONZAI_URL}/api/topics/active-sessions", headers=headers, method="GET"
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        data = json.loads(res.read())
    if not isinstance(data, list):
        return None
    # Each entry is {topicId, title, sessions:[{sessionId, enteredAt}, ...]}. Show
    # the topic whose presence list contains THIS session — at most one.
    for topic in data:
        if not isinstance(topic, dict):
            continue
        for sess in topic.get("sessions") or []:
            if isinstance(sess, dict) and str(sess.get("sessionId") or "") == session_id:
                return {
                    "title": topic.get("title"),
                    "id": topic.get("topicId"),
                    "enteredAt": sess.get("enteredAt"),
                }
    return None


def main():
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw else {}
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return  # the badge is per-session — nothing to match without a session id

    path = cache_path(session_id)
    cached = read_cache(path)
    if cached is not None:
        if cached:
            sys.stdout.write(cached)
        return  # fresh cache (text or an empty back-off entry) — no network call

    # Cache miss: fetch once, render, and cache the result (even "") so a down
    # server / no-topic session is retried at most once per TTL.
    try:
        text = render(fetch(session_id, payload))
    except Exception:
        text = ""  # fail-open: render nothing and back off for the TTL
    write_cache(path, text)
    if text:
        sys.stdout.write(text)


try:
    main()
except Exception:
    pass  # best-effort: a status line must never block or error the terminal
sys.exit(0)
