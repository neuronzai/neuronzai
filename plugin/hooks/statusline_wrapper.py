#!/usr/bin/env python3
# Topic-mode status line — the WRAPPER (#43). This file's SOURCE ships with the
# plugin at hooks/statusline_wrapper.py; statusline_install.py COPIES it to
# ~/.claude/.neuronzai/statusline.py the first time a session enters topic mode,
# and points the user's settings.json `statusLine.command` at the copy. It is
# SELF-CONTAINED (pure stdlib plus a copied auth.py — cwd canonicalization is
# inlined) so it keeps working across plugin updates, which is exactly
# what lets it SELF-HEAL.
#
# Two jobs, in order, every render:
#
#   1. SELF-HEAL. We recorded the plugin's (un-versioned) install dir at
#      ~/.claude/.neuronzai/plugin_dir. If that path no longer exists the plugin
#      was uninstalled — so we RESTORE the user's original status line (saved at
#      statusline_base.json: write it back, or REMOVE the statusLine key when the
#      user had none), delete ~/.claude/.neuronzai/ (self + sidecars), print the
#      original status line's output, and stop. A plugin UPDATE swaps only the
#      version dir under that parent, so it is NOT mistaken for an uninstall.
#
#   2. WRAP. Otherwise run the user's ORIGINAL status line first (feeding it the
#      SAME stdin), then fetch THIS session's active topic from
#      /api/topics/active-sessions (the PER-SESSION presence aggregate — the same
#      source the dashboard's "active now" view uses, matched on our own
#      session_id) and append ` · 🎯 <title>`. Base-only when no topic; topic-only
#      when the user had no status line; nothing when neither.
#
# Like #40's topic_statusline.py: strictly FAIL-OPEN (≤1s API timeout, prints
# nothing on any error / missing login / missing session), and the topic lookup is
# CACHED on disk per session (NEURONZAI_STATUSLINE_TTL, default 8s) so the
# frequently re-rendered status line does not hit the API on every redraw. Topic
# titles are stripped of control bytes (terminal-escape hardening) and clamped.
# Reads the same saved OAuth grant as the hooks, with legacy environment
# credentials and NEURONZAI_URL / NEURONZAI_PROFILE as compatibility overrides.

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_AUTH_DIR = os.path.normpath(os.path.join(SELF_DIR, "..", "core"))
if os.path.isfile(os.path.join(SELF_DIR, "auth.py")):
    sys.path.insert(0, SELF_DIR)
elif os.path.isfile(os.path.join(PLUGIN_AUTH_DIR, "auth.py")):
    sys.path.insert(0, PLUGIN_AUTH_DIR)
try:
    import auth as plugin_auth
except ImportError:
    plugin_auth = None

# Only used when the copied credential reader is missing; mirrors core/auth.py and is
# restamped with it by `marketplace_tree.py` for the production marketplace.
DEFAULT_BASE_URL = "https://app.neuronz.ai"
NEURONZAI_URL = (
    plugin_auth.base_url()
    if plugin_auth
    else (os.environ.get("NEURONZAI_URL") or DEFAULT_BASE_URL).rstrip("/")
)
PROFILE = (os.environ.get("NEURONZAI_PROFILE") or "").strip()

TIMEOUT = 1.0  # seconds — a status line must never block the terminal
BASE_TIMEOUT = 5.0  # the wrapped status line is the user's own — bounded, not unbounded
TITLE_MAX = 48  # clamp the badge so it stays compact across re-renders

# This wrapper always lives at ~/.claude/.neuronzai/statusline.py, so its own
# location anchors every path: its dir holds the sidecars (the saved base + the
# plugin-dir marker), and the PARENT is ~/.claude where settings.json lives.
CLAUDE_DIR = os.path.dirname(SELF_DIR)
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
BASE_FILE = os.path.join(SELF_DIR, "statusline_base.json")
PLUGIN_DIR_FILE = os.path.join(SELF_DIR, "plugin_dir")


def _int_env(name, default):
    try:
        return int(float(os.environ[name]))
    except (KeyError, ValueError):
        return default


# Seconds a fetched topic is reused before we touch the API again. The status line
# re-renders many times a second; this is what keeps it off the network.
TTL = max(2, _int_env("NEURONZAI_STATUSLINE_TTL", 8))


# --- cwd -> profile-key resolution (inlined from neuronzai_cwd.py so the wrapper
#     has no plugin dependency and survives uninstall) ---------------------------


def raw_cwd(payload):
    # Host-STATED values only — the payload, then the host's own env var. No
    # process-cwd leg (#383): the wrapper's own directory is our guess, not
    # something Claude Code reported. Mirrors neuronzai_cwd.raw_cwd exactly.
    return str(
        payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ""
    ).strip()


def canonical_cwd(cwd):
    """Map `cwd` to its git MAIN-worktree root so a linked worktree (and any subdir
    of one) resolves to the repo's own profile — matching the hooks. Returns `cwd`
    unchanged on a non-git dir, missing/old git, or any error; never raises."""
    if not cwd or not os.path.isdir(cwd):
        return cwd
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd
    common = proc.stdout.strip()
    if proc.returncode != 0 or not common:
        return cwd
    root = os.path.dirname(common.rstrip("/"))
    return root or cwd


def session_cwd(payload):
    return canonical_cwd(raw_cwd(payload))


# --- sidecar state -------------------------------------------------------------


def read_base():
    """The user's original statusLine object (a dict), or None when they had none
    (or the sidecar is missing/corrupt — in which case un-wrapping is the safe
    default)."""
    try:
        with open(BASE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def read_plugin_dir():
    try:
        with open(PLUGIN_DIR_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def plugin_uninstalled():
    """True only when we recorded a plugin dir AND it is now gone. A missing/empty
    marker returns False — we never tear down the user's status line just because
    we cannot read our own bookkeeping."""
    recorded = read_plugin_dir()
    return bool(recorded) and not os.path.exists(recorded)


# --- settings.json I/O ---------------------------------------------------------


def load_settings():
    """(settings, ok). Missing file -> ({}, True). Invalid JSON -> (None, False),
    so a restore never corrupts a settings.json it cannot parse."""
    try:
        with open(SETTINGS, "r", encoding="utf-8") as fh:
            return json.load(fh), True
    except FileNotFoundError:
        return {}, True
    except (ValueError, OSError):
        return None, False


def write_settings(settings):
    """Atomic pretty-printed write (temp + os.replace)."""
    tmp = f"{SETTINGS}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, SETTINGS)


# --- the wrapped (user's original) status line ---------------------------------


def run_base(raw, base):
    """Run the user's original statusLine command in a shell, feeding it the SAME
    stdin Claude Code gave us, and return its stdout (trailing newline stripped).
    "" when there is no base command or it errors/times out (fail-open)."""
    cmd = (base or {}).get("command") if isinstance(base, dict) else None
    if not cmd:
        return ""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            input=raw or "",
            capture_output=True,
            text=True,
            timeout=BASE_TIMEOUT,
        )
        return proc.stdout.rstrip("\n")
    except Exception:
        return ""


# --- the topic badge (cached, like #40) ----------------------------------------


def cache_path(session_id):
    key = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"neuronzai-statusline-{key}.json")


def read_cache(path):
    """The cached badge if still fresh (possibly ""), else None (= refetch)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.load(fh)
        if (time.time() - float(entry["ts"])) < TTL:
            return str(entry.get("text") or "")
    except Exception:
        pass
    return None


def write_cache(path, text):
    try:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "text": text}, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def sanitize_title(title):
    """Strip control bytes — blocks terminal-escape / OSC-hyperlink injection via a
    topic title — and clamp to a compact width."""
    clean = "".join(
        ch for ch in title if ch >= " " and ch != "\x7f" and not ("\x80" <= ch <= "\x9f")
    ).strip()
    if len(clean) > TITLE_MAX:
        clean = clean[: TITLE_MAX - 1].rstrip() + "…"
    return clean


def topic_badge(active):
    """`🎯 <title>` for an active-topic dict, or "" when nothing is active."""
    if not isinstance(active, dict):
        return ""
    title = sanitize_title(str(active.get("title") or ""))
    if not title:
        return ""
    return f"\U0001f3af {title}"


def fetch(session_id, payload):
    """THIS session's active topic, or None. Reads the PER-SESSION presence
    aggregate (GET /api/topics/active-sessions — the exact source the dashboard's
    "active now" view uses) and matches our OWN session_id, so the badge reflects
    only a topic THIS session entered.

    Was GET /api/topics/mode, which resolves the (profile, cwd) ANCHOR — durable
    and SHARED by every session in the repo (#41) — so the badge bled onto sibling
    sessions that never entered topic mode while the dashboard (presence) stayed
    correct. Matching presence here makes the badge agree with the dashboard."""
    headers = {}
    token = (
        plugin_auth.cached_access_token()
        if plugin_auth
        else (
            os.environ.get("NEURONZAI_API_KEY")
            or os.environ.get("NEURONZAI_TOKEN")
            or os.environ.get("AGENT_TOKEN")
            or ""
        )
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if PROFILE:
        headers["X-Profile"] = PROFILE
    else:
        cwd = session_cwd(payload)
        if cwd:
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


def cached_topic(payload):
    """The topic badge for this session, served from the per-session disk cache
    when fresh; otherwise fetched once, rendered, and cached (even "")."""
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return ""  # the badge is per-session — nothing to match without a session id
    path = cache_path(session_id)
    cached = read_cache(path)
    if cached is not None:
        return cached
    try:
        text = topic_badge(fetch(session_id, payload))
    except Exception:
        text = ""  # fail-open: render nothing and back off for the TTL
    write_cache(path, text)
    return text


# --- self-heal -----------------------------------------------------------------


def self_heal(raw):
    """Restore the user's original status line and remove ourselves, then print the
    original status line's output. Best-effort: an unreadable settings.json is left
    untouched (we still tear ourselves down so we stop wrapping)."""
    base = read_base()  # read BEFORE we delete SELF_DIR
    settings, ok = load_settings()
    if ok and settings is not None:
        if isinstance(base, dict):
            settings["statusLine"] = base
        else:
            settings.pop("statusLine", None)
        try:
            write_settings(settings)
        except Exception:
            pass
    shutil.rmtree(SELF_DIR, ignore_errors=True)
    out = run_base(raw, base)
    if out:
        sys.stdout.write(out)


# --- compose -------------------------------------------------------------------


def compose(base_text, topic):
    if base_text and topic:
        return f"{base_text} · {topic}"
    return base_text or topic or ""


def main():
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}

    if plugin_uninstalled():
        self_heal(raw)
        return

    base = read_base()
    base_text = run_base(raw, base)
    line = compose(base_text, cached_topic(payload))
    if line:
        sys.stdout.write(line)


try:
    main()
except Exception:
    pass  # best-effort: a status line must never block or error the terminal
sys.exit(0)
