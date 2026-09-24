#!/usr/bin/env python3
# PostToolUse hook — AUTO-INSTALL the topic-mode status line, WRAPPING whatever the
# user already has (#43).
#
# WHY THIS EXISTS: a Claude Code plugin CANNOT declare the main `statusLine` (a
# plugin's settings.json supports only agent/subagentStatusLine, and there is no
# install lifecycle hook), so #40's topic_statusline.py had to be wired BY HAND.
# This hook closes that gap. The FIRST time a session enters topic mode (the
# enter_topic tool) we install a SELF-CONTAINED wrapper into the CLI's settings.json
# (CLAUDE_CONFIG_DIR if set — the claudine variant's ~/.claudine — else ~/.claude)
# that runs THEIR existing status line first and appends ` · 🎯 <topic>` — nobody
# loses their bar.
#
# Trigger: PostToolUse on enter_topic ONLY (never plain SessionStart), so a user
# who never touches topic mode never has their settings.json modified. Idempotent
# via a stable marker (the wrapper path). Opt out with
# NEURONZAI_STATUSLINE_AUTOINSTALL=0.
#
# What it writes (all under the CLI config dir — CLAUDE_CONFIG_DIR or ~/.claude — none
# inside the version-stamped plugin dir, so the wrapper survives a plugin uninstall
# and can self-heal — see statusline_wrapper.py):
#   .neuronzai/statusline.py        — a copy of statusline_wrapper.py (the new bar)
#   .neuronzai/auth.py              — the shared OAuth credential reader
#   .neuronzai/statusline_base.json — the user's ORIGINAL statusLine (or null)
#   .neuronzai/plugin_dir           — the plugin's UN-VERSIONED install dir
#   settings.json.neuronzai.bak     — one-time pristine backup (safety net)
#   settings.json:statusLine        — re-pointed at the wrapper (other keys kept)
#
# Strictly best-effort / FAIL-OPEN: any error exits 0 and never blocks the tool. An
# INVALID settings.json is left untouched (we never risk corrupting it). settings.json
# is rewritten LAST, so a crash mid-install never points the bar at a missing wrapper.
# Wired by hooks/hooks.json (PostToolUse, alongside topic_mode_sync) and resolved via
# ${CLAUDE_PLUGIN_ROOT}. The .claude/hooks/ and plugin/hooks/ copies MUST stay
# byte-identical. Pure stdlib Python 3.

import hashlib
import json
import os
import re
import shutil
import sys

AUTOINSTALL = (os.environ.get("NEURONZAI_STATUSLINE_AUTOINSTALL") or "1").strip()

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("HOME") or os.path.expanduser("~")
# Honor CLAUDE_CONFIG_DIR so we install into the config dir the RUNNING CLI actually
# reads: the `claudine` variant sets it to ~/.claudine, and hardcoding ~/.claude
# there silently NO-OPS the bar — the install lands in a dir the CLI never loads, so
# statusLine stays null and the topic badge never renders. Matches core/assets.py /
# core/runs.py, which already resolve the config dir.
CLAUDE_DIR = (os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")).rstrip("/")
NEUR_DIR = os.path.join(CLAUDE_DIR, ".neuronzai")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
BACKUP = os.path.join(CLAUDE_DIR, "settings.json.neuronzai.bak")
WRAPPER_SRC = os.path.join(HOOK_DIR, "statusline_wrapper.py")
WRAPPER_DST = os.path.join(NEUR_DIR, "statusline.py")
AUTH_SRC = os.path.join(os.path.dirname(HOOK_DIR), "core", "auth.py")
AUTH_DST = os.path.join(NEUR_DIR, "auth.py")
BASE_FILE = os.path.join(NEUR_DIR, "statusline_base.json")
PLUGIN_DIR_FILE = os.path.join(NEUR_DIR, "plugin_dir")

# The status-line command we install. ${CLAUDE_CONFIG_DIR:-$HOME/.claude} is
# shell-expanded when Claude Code runs it, so the bar resolves to the SAME config dir
# the CLI reads (the claudine variant's ~/.claudine, else ~/.claude) — install dir and
# running CLI stay in agreement. The wrapper path is STABLE across plugin updates (it
# lives outside the version-stamped plugin dir). The path substring is also our
# IDEMPOTENCY MARKER: if statusLine.command already references it, it's installed -> no-op.
WRAPPER_MARKER = ".neuronzai/statusline.py"
WRAPPER_COMMAND = 'python3 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.neuronzai/statusline.py"'


def stable_plugin_dir():
    """${CLAUDE_PLUGIN_ROOT} with a trailing VERSION component stripped.

    A marketplace install is version-stamped (…/neuronzai/neuronzai/<version>);
    recording the un-versioned PARENT (…/neuronzai/neuronzai) means a plugin UPDATE
    (which swaps only the version dir) does NOT look like an uninstall to the
    wrapper's self-heal — only a real uninstall removes the parent. A dev/clone
    install (…/packages/plugin, no version tail) is recorded as-is (a clone is
    never uninstalled out from under the user)."""
    root = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").rstrip("/")
    if not root:
        return ""
    parent, base = os.path.split(root)
    if parent and re.match(r"^v?\d+\.\d+", base):
        return parent
    return root


def load_settings():
    """(settings, ok). Missing file -> ({}, True) (we create it). Invalid JSON ->
    (None, False) so the caller BAILS rather than overwriting an unparseable file."""
    try:
        with open(SETTINGS, "r", encoding="utf-8") as fh:
            return json.load(fh), True
    except FileNotFoundError:
        return {}, True
    except (ValueError, OSError):
        return None, False


def already_installed(settings):
    cmd = str(((settings or {}).get("statusLine") or {}).get("command") or "")
    return WRAPPER_MARKER in cmd


def write_json(path, obj):
    """Atomic pretty-printed write (temp + os.replace) — a crash mid-write never
    leaves a half-written file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def install(settings):
    base = settings.get("statusLine")  # the user's original (dict) or None
    os.makedirs(NEUR_DIR, exist_ok=True)
    # Copy the SELF-CONTAINED wrapper out of the (version-stamped) plugin dir so it
    # outlives a plugin uninstall. Do this — plus the sidecar state — BEFORE we
    # touch settings.json, so the bar is never pointed at a wrapper that isn't there.
    shutil.copyfile(WRAPPER_SRC, WRAPPER_DST)
    if os.path.isfile(AUTH_SRC):
        shutil.copyfile(AUTH_SRC, AUTH_DST)
    write_json(BASE_FILE, base)  # null when the user had no status line
    with open(PLUGIN_DIR_FILE, "w", encoding="utf-8") as fh:
        fh.write(stable_plugin_dir() + "\n")
    # One-time pristine backup, the safety net behind the self-heal.
    if os.path.exists(SETTINGS) and not os.path.exists(BACKUP):
        shutil.copyfile(SETTINGS, BACKUP)
    # Finally, re-point the status line at the wrapper (minimal change — every other
    # key in settings.json is preserved).
    settings["statusLine"] = {"type": "command", "command": WRAPPER_COMMAND, "padding": 0}
    write_json(SETTINGS, settings)


def _sha256(path):
    """Hex sha256 of a file's bytes, or "" when it can't be read."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def refresh_wrapper():
    """Our wrapper is ALREADY the status line. Keep the installed copy current with
    the SHIPPED one across plugin UPDATES: install is otherwise gated on the marker
    STRING (already_installed), so a content change to statusline_wrapper.py would
    never reach an existing install — the wrapper would stay frozen at whatever
    version first installed it. Re-copy only the wrapper and its credential reader
    when their bytes differ from the shipped sources (restoring either if missing),
    never touching settings.json / statusline_base.json / plugin_dir. Best-effort:
    a failure just retries on the next enter_topic."""
    try:
        if not os.path.exists(WRAPPER_SRC):
            return  # no shipped source to copy from (shouldn't happen if this ran)
        os.makedirs(NEUR_DIR, exist_ok=True)
        if _sha256(WRAPPER_SRC) != _sha256(WRAPPER_DST):
            shutil.copyfile(WRAPPER_SRC, WRAPPER_DST)
        if os.path.isfile(AUTH_SRC) and _sha256(AUTH_SRC) != _sha256(AUTH_DST):
            shutil.copyfile(AUTH_SRC, AUTH_DST)
    except OSError:
        pass  # never block the tool over a refresh


def main():
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    tool_name = str(payload.get("tool_name") or "")
    if not tool_name.endswith("enter_topic"):
        return  # install ONLY on first use of topic mode, never on SessionStart
    if AUTOINSTALL == "0":
        return  # opt-out
    settings, ok = load_settings()
    if not ok:
        return  # invalid JSON -> never risk corrupting the user's settings.json
    if already_installed(settings):
        refresh_wrapper()  # propagate wrapper updates to an existing install
        return
    install(settings)


try:
    main()
except Exception:
    pass  # best-effort: a hook must never block the tool that triggered it
sys.exit(0)
