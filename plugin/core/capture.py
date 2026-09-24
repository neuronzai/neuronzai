#!/usr/bin/env python3
# PostToolUse handler (host-agnostic) — deterministic action capture. This is the
# single lifecycle implementation dispatched by entry.py for every host.
#
# An action by definition leaves a tool-call trail, and hooks see every tool call
# deterministically; so this handler appends each CONSEQUENTIAL call (git
# commit/push, deploys, file edits) to a profile-owned session ledger on the server
# (agent_kv, namespace "session-ledger", server-bound to profile + session id,
# via the atomic
# POST /api/kv/append). At the end-of-session sweep the server distills the ledger
# into one provenance='sweep' action per owned profile, with entries attached as evidence
# — UNLESS the agent logged deliberately, which this handler also detects (a
# log_action tool call is captured as trigger 'agent_logged' and stands the
# distillation down).
#
# PostToolUse fires only after a tool call SUCCEEDS, but for a shell command that
# means the tool ran — the command may still have exited non-zero, so entries
# record that a command RAN and the narration hedges accordingly. Non-consequential
# calls exit with NO network call at all — the common case must be near-free.
#
# This handler has a SIDE EFFECT only (append to the KV ledger) and returns None
# (no injected context). Kill switch: NEURONZAI_CAPTURE=0/false/off disables it.
# Pure stdlib.

import os
import re
from datetime import datetime, timezone

from core import api, profile as profile_mod
from core.hostapi import Event, Host, Output  # noqa: F401 (Output kept for signature parity)

LEDGER_NAMESPACE = "session-ledger"
LEDGER_TTL_SECONDS = 172_800  # 48h; refreshed on every append (server COALESCEs)
DETAIL_MAX_CHARS = 200


def capture_trigger(tool_name, tool_input, edited_path=""):
    """Map a tool call to ONE (trigger, detail) key, or (None, None) for the
    non-consequential common case. `tool_name` is the canonical (host-normalized)
    name: shell is "Bash", an MCP tool is "mcp__server__tool" (so the log_action
    substring match spans hosts). A file edit is signaled by a non-empty
    `edited_path` — the host adapter's normalized touched-file path — so this stays
    a plain presence check with no edit-tool vocabulary."""
    if tool_name == "Bash":
        command = str((tool_input or {}).get("command") or "")
        if re.search(r"\bgit\s+push\b", command):
            return "git_push", command
        if re.search(r"\bgit\s+commit\b", command):
            return "git_commit", command
        if re.search(r"\b(docker\s+push|kubectl\s+apply|helm\s+upgrade|terraform\s+apply)\b", command):
            return "deploy", command
        return None, None
    if edited_path:
        return "file_edit", edited_path
    if re.search(r"log_action", tool_name):
        return "agent_logged", tool_name
    return None, None


def handle(event: Event, host: Host):
    """Append one ledger entry for a consequential tool call; return None always
    (pure side effect — no injected context). Exits silently on the kill switch,
    a missing session id, or a non-consequential call (the last with no network
    call at all)."""
    if (os.environ.get("NEURONZAI_CAPTURE") or "").strip().lower() in ("0", "false", "off"):
        return None

    session_id = (event.session_id or "").strip()
    if not session_id:
        return None

    trigger, detail = capture_trigger(event.tool_name, event.tool_input or {}, event.edited_path)
    if not trigger:
        return None  # non-consequential — no network call

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": event.tool_name,
        "trigger": trigger,
        "detail": (detail or "")[:DETAIL_MAX_CHARS],
    }

    profile, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)
    extra = {api.SWEEP_PROTOCOL_HEADER: api.SWEEP_PROTOCOL_VERSION}
    if cwd:
        extra["X-Cwd"] = cwd
    api.post(
        "/api/kv/append",
        body={
            "namespace": LEDGER_NAMESPACE,
            "key": session_id,
            "item": entry,
            "ttlSeconds": LEDGER_TTL_SECONDS,
        },
        profile=profile,
        session_id=session_id,
        where="memory_capture",
        extra_headers=extra,
    )
    return None
