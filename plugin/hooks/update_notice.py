#!/usr/bin/env python3
# UserPromptSubmit hook — ONE-TIME per session "a newer plugin is installed" notice
# for Claude Code or omp.
#
# WHY THIS EXISTS: Claude Code auto-updates marketplace plugins at STARTUP, but a
# session that is ALREADY RUNNING keeps executing the plugin VERSION it launched
# with — its hooks.json commands were expanded (and its version dir pinned) at
# session start. So after `/plugin update` (or the background startup auto-update)
# lands a newer neuronzai build, the live session is silently a version behind
# until it is restarted, and nothing tells the user. This hook closes that gap:
# once per session, if the version PINNED on disk is strictly NEWER than the version
# THIS session is running, it surfaces a single heads-up so the user can restart /
# reload to pick it up. It fires ONLY on a strict UPGRADE — a downgrade (pin OLDER
# than the running version) or a dev/clone checkout whose plugin.json is AHEAD of the
# last-published pin stays SILENT (a "restart to pick it up" nudge there would be
# wrong or a no-op), and any pair that can't be compared cleanly (a non-numeric
# version component) also stays silent (fail-open: never a wrong notice).
#
# Running version: the host plugin root's native manifest -> version.
#   If that dir was GC-swept out from under the session (the same version-dir
#   sweep the run.sh launcher fallback guards against), fall back to the version
#   stamped on the CLAUDE_PLUGIN_ROOT path tail (…/neuronzai/neuronzai/<version>)
#   — same ^v?\d+\.\d+ tail detection statusline_install.stable_plugin_dir() uses.
#   A dev/clone checkout (…/packages/plugin, no version tail) yields "" -> silent.
# Installed pin: Claude Code reads installed_plugins.json; omp writes that SAME file
#   (and the same "<plugin>@<marketplace>" key) beside its plugin cache, so both go
#   through one reader.
#
# Delivery: a top-level "systemMessage" in the hook's stdout JSON — a universal
# hook-output field Claude Code renders straight to the USER (honored on
# UserPromptSubmit). This is a user-facing heads-up, so it deliberately does NOT
# go through hookSpecificOutput.additionalContext (which would spend the model's
# context window and risk the model reshaping or dropping the relay).
#
# Once per session: a flag file keyed by session_id under
# ${NEURONZAI_STATE_DIR:-~/.neuronzai}/update-notice/, mirroring the
# session-profiles store in neuronzai_session.py (same filename-safe key + age
# prune). Flag present -> stay silent. A session with no usable id can't be
# deduped, so it simply re-checks each prompt (notify-again beats never).
#
# Strictly best-effort / FAIL-OPEN: any error (unreadable JSON, missing keys, an
# unwritable state dir) exits 0 with no output and never blocks the prompt. Pure
# stdlib Python 3. The .claude/hooks/ and plugin/hooks/ copies MUST stay
# byte-identical.

import json
import os
import re
import sys
import time

HOME = os.environ.get("HOME") or os.path.expanduser("~")
# Honor CLAUDE_CONFIG_DIR so the pin is read from the config dir the RUNNING CLI
# actually uses (the claudine variant sets it to ~/.claudine); matches
# statusline_install.py / core/assets.py.
CLAUDE_DIR = (os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")).rstrip("/")
PLUGIN_ROOT = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").rstrip("/")

# Claude Code records a marketplace install keyed "<plugin>@<marketplace>"; for
# neuronzai both halves are "neuronzai".
INSTALL_KEY = "neuronzai@neuronzai"

# A version-stamped marketplace dir ends in …/<version> (e.g. 0.25.25 or v0.25.25);
# a dev/clone checkout (…/packages/plugin) has no such tail. Same shape as
# statusline_install.stable_plugin_dir().
_VERSION_TAIL = re.compile(r"^v?\d+\.\d+")

# omp installs a VERSIONED COPY under <plugins base>/cache/plugins/ and names the
# dir "<plugin>___<marketplace>___<version>", so its tail is not a bare version and
# the shared _VERSION_TAIL never matches it. It also sets CLAUDE_PLUGIN_ROOT (see
# the `oauth-login` feature note), which is why this host is detected from the dir
# NAME rather than from an env var of its own: the bridge runs this file as a
# `legacy_script` spec and passes no arguments.
_OMP_VERSION_DIR = re.compile(r"^.+___.+___v?(?P<version>\d+\.\d+.*)$")
IS_OMP = bool(_OMP_VERSION_DIR.match(os.path.basename(PLUGIN_ROOT))) if PLUGIN_ROOT else False

# An un-substituted command template (a CLI that didn't expand ${CLAUDE_SESSION_ID})
# is treated as "no session id", never as a literal key (mirrors neuronzai_session).
UNSUBSTITUTED_SESSION_ID = "${CLAUDE_SESSION_ID}"


def state_dir():
    base = os.environ.get("NEURONZAI_STATE_DIR") or os.path.join(HOME, ".neuronzai")
    return os.path.join(base, "update-notice")


def _flag_path(session_id):
    """Per-session flag path, or None when there is no usable session id. Keeps
    only filename-safe chars so a weird value can never escape the store dir
    (mirrors neuronzai_session._path)."""
    sid = (session_id or "").strip()
    if not sid or sid == UNSUBSTITUTED_SESSION_ID:
        return None
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    if not safe:
        return None
    return os.path.join(state_dir(), safe + ".json")


def _norm(version):
    """Compare running vs pinned on equal footing — plugin.json carries a bare
    '0.25.25', a swept path tail may carry 'v0.25.25'."""
    return (version or "").strip().lstrip("v")


def _parse_version(version):
    """A dotted numeric version -> a tuple of int components, or None when ANY
    component is non-numeric/empty (can't be compared cleanly). The leading 'v' was
    already stripped by _norm; a suffix like '5-beta' is deliberately treated as
    uncomparable (-> None) rather than guessed at."""
    parts = (version or "").split(".")
    out = []
    for part in parts:
        if not part.isdigit():
            return None
        out.append(int(part))
    return tuple(out)


def pin_is_strictly_newer(pinned, running):
    """True ONLY when the installed pin is a strictly NEWER version than the one this
    session runs — the sole case a "restart to pick it up" nudge is correct. A
    downgrade (pin older), an equal version, or a dev/clone checkout whose plugin.json
    is AHEAD of the last-published pin all return False. Compares numeric components
    left to right, zero-padding the shorter (0.26 vs 0.25.5 -> newer; 0.25 vs 0.25.5
    -> not); any uncomparable/non-numeric component -> False (never a wrong notice)."""
    pv = _parse_version(pinned)
    rv = _parse_version(running)
    if pv is None or rv is None:
        return False
    width = max(len(pv), len(rv))
    pv += (0,) * (width - len(pv))
    rv += (0,) * (width - len(rv))
    return pv > rv


def running_version():
    """The version THIS session is executing: native manifest first, else the version
    stamped on the CLAUDE_PLUGIN_ROOT tail. "" when neither resolves."""
    if not PLUGIN_ROOT:
        return ""
    try:
        with open(
            os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), "r", encoding="utf-8"
        ) as fh:
            version = str((json.load(fh) or {}).get("version") or "").strip()
        if version:
            return version
    except (OSError, ValueError):
        pass
    # Manifest unreadable (dir swept) -> the version dir name IS the version. On omp
    # the sweep is the NORMAL case, not an edge one: `omp plugin upgrade` REPLACES
    # the versioned copy, so a session that pinned the old root keeps running from a
    # directory that no longer exists and only the path still names its version.
    base = os.path.basename(PLUGIN_ROOT)
    omp_dir = _OMP_VERSION_DIR.match(base)
    if omp_dir:
        return omp_dir.group("version")
    if _VERSION_TAIL.match(base):
        return base
    return ""


def _pinned_from_installed_plugins(path):
    """Read the pinned version out of an `installed_plugins.json` — Claude Code's
    format, which omp writes VERBATIM (`plugins["<plugin>@<marketplace>"][0]`), so
    both hosts share one reader rather than two copies that can drift apart."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    entries = ((data or {}).get("plugins") or {}).get(INSTALL_KEY) or []
    if not isinstance(entries, list) or not entries:
        return ""
    first = entries[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("version") or "").strip()


def _omp_installed_version():
    """omp's pin file sits beside its plugin cache: the running root is
    <base>/cache/plugins/<dir>, so the pin is <base>/installed_plugins.json. Derived
    from the ROOT rather than hardcoding ~/.omp so a relocated omp state dir still
    resolves — and so a swept version dir (the normal upgrade case) does not matter,
    since only the path is read, never its contents."""
    base = os.path.dirname(os.path.dirname(os.path.dirname(PLUGIN_ROOT)))
    if not base:
        return ""
    return _pinned_from_installed_plugins(os.path.join(base, "installed_plugins.json"))


def installed_version():
    """The version PINNED on disk (what a fresh session would launch). "" when the
    pin file / key / entry is absent."""
    if IS_OMP:
        return _omp_installed_version()
    return _pinned_from_installed_plugins(
        os.path.join(CLAUDE_DIR, "plugins", "installed_plugins.json")
    )


def prune(max_age_days=14):
    """Best-effort delete of flag files older than max_age_days so the store never
    grows without bound (session ids are unique). Silent on any error (mirrors
    neuronzai_session.prune)."""
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


def mark_shown(flag_path, pinned, running):
    """Record that THIS session was notified, atomically (mirrors
    neuronzai_session.write_profile). Best-effort — a write failure just means the
    notice may re-fire next prompt, never a crash."""
    try:
        os.makedirs(os.path.dirname(flag_path), exist_ok=True)
        body = {"pinned": pinned, "running": running, "ts": int(time.time())}
        tmp = flag_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        os.replace(tmp, flag_path)
        prune()
    except OSError:
        pass


def main():
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    session_id = str(payload.get("session_id") or "").strip()

    # Once-per-session gate first: a recorded flag means THIS session was already
    # told, so we never re-read versions or re-notify.
    flag_path = _flag_path(session_id)
    if flag_path and os.path.exists(flag_path):
        return

    running = running_version()
    pinned = installed_version()
    # Notify ONLY when the pin is strictly NEWER than the running version (a restart
    # picks it up). Equal, a downgrade, a dev/clone checkout ahead of the pin, or an
    # uncomparable pair -> stay silent (never a wrong "restart to pick it up" nudge).
    if not running or not pinned or not pin_is_strictly_newer(_norm(pinned), _norm(running)):
        return

    tail = (
        # omp pins the version dir at session start and its /reload-plugins re-reads
        # discovery without repointing that root (see the `skills-hot-reload` note),
        # so only a restart actually moves this session onto the installed copy.
        "Restart omp to pick it up."
        if IS_OMP
        else "Restart the session (or /plugin update + /reload-plugins) to pick it up."
    )
    message = (
        f"neuronzai plugin {_norm(pinned)} is installed — this session is still running "
        f"{_norm(running)}. {tail}"
    )
    print(json.dumps({"systemMessage": message}, ensure_ascii=False))

    # Record AFTER emitting so the one-shot is spent only once the notice went out.
    if flag_path:
        mark_shown(flag_path, _norm(pinned), _norm(running))


try:
    main()
except Exception:
    pass  # best-effort: a hook must never block the prompt
sys.exit(0)
