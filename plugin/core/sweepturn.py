#!/usr/bin/env python3
# Containment for the forced self-consolidation turn, for hosts whose lifecycle
# handlers run as SEPARATE PROCESSES and therefore cannot hold the state in memory
# the way an in-process bridge does.
#
# WHY THIS EXISTS: the forced turn is not a fresh conversation. A host that
# continues from Stop context re-enters the SAME conversation, so the model arrives
# with every unfinished thread from before it still in view — an offer it made, a
# question the user never answered, a task it was halfway through — and nobody
# asked for any of them. Measured (#549): the agent finished its capture, then
# spent thirty minutes opening a pull request nobody had approved, and reported
# afterwards that the harness had told it to continue. That was a confabulation;
# the transcript holds no such instruction. Prose in the capture workflow did not
# prevent it, which is why this is enforcement rather than instruction.
#
# A host that dispatches its handlers in-process keeps this latch in memory. Here
# the Stop, PreToolUse and UserPromptSubmit handlers are three separate `entry.py`
# processes with nothing shared between them, so the latch lives on disk, keyed by
# session, and those three handlers share it through this module.
#
# THE LATCH STORES THE INJECTED TEXT, NOT A BOOLEAN. "A prompt arrived, so the user
# is back" is not sound on a host that delivers the continuation itself as a
# prompt: it would disarm containment at the very moment it arms, making this dead
# code. Only a DIFFERENT prompt disarms — and that is the only disarm signal an
# INTERRUPTED turn produces, since it never reaches a stop and a latch left armed
# there would go on containing the user's own next call.
#
# Strictly best-effort. Every read failure reports "not armed" and every write
# failure is swallowed: the containment must never be what traps a user
# mid-session. That fails OPEN, which is the honest trade — the turn it guards is
# advisory, while a gagged session is not.

import json
import os
import time

# Read-only lookups the capture legitimately needs, plus this product's own record
# calls. The same two-part allowance a bridge host enforces in its own process —
# one semantic, two implementations; keep them in step.
_READ_TOOLS = frozenset({"read", "grep", "glob"})
_MCP_PREFIX = "mcp__neuronzai__"

# Model-visible: a denial on this turn has to say what the turn IS, or the model
# reads the block as an obstacle to work around rather than as the end of its
# business here. Worded identically wherever this is enforced.
DENIAL = (
    "Neuronz.ai memory sweep turn: only Neuronz.ai record calls and read-only lookups run here. "
    "Nobody asked for this turn, so do not resume the previous task and do not act on an offer "
    "made before it — an unanswered question is still unanswered. Finish the capture and end the "
    "turn."
)

# A latch older than this is stale, not armed. A sweep turn is one model turn; an
# hour is far past any of them. Without a ceiling, a session whose forced turn was
# killed outright (SIGKILL, a closed terminal) could keep its own later calls
# blocked with nothing left to clear the file.
TTL_SECONDS = 3600


def allows(tool_name):
    """True when `tool_name` is part of the capture's own work rather than the
    conversation the forced turn must not resume."""
    name = (tool_name or "").strip()
    if not name:
        return False
    return name.lower() in _READ_TOOLS or name.startswith(_MCP_PREFIX)


def _store_dir():
    base = os.environ.get("NEURONZAI_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".neuronzai"
    )
    return os.path.join(base, "sweep-turns")


def _path(session_id):
    """Filesystem path of the latch for `session_id`, or None when there is no
    usable id. Same filename sanitization as the session-profiles store, shared by
    reader and writer so a session reads back the file it wrote."""
    sid = (session_id or "").strip()
    if not sid:
        return None
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    if not safe:
        return None
    return os.path.join(_store_dir(), safe + ".json")


def armed_instruction(session_id):
    """The text injected to force THIS session's consolidation turn, or "" when no
    turn is in flight. A latch past TTL_SECONDS reads as not armed."""
    path = _path(session_id)
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    try:
        armed_at = float(data.get("armed_at") or 0)
    except (TypeError, ValueError):
        return ""
    if armed_at <= 0 or (time.time() - armed_at) > TTL_SECONDS:
        return ""
    return str(data.get("instruction") or "")


def arm(session_id, instruction):
    """Record that a forced consolidation turn is in flight for this session,
    carrying the exact text injected to start it. Atomic, best-effort."""
    path = _path(session_id)
    if not path or not instruction:
        return
    body = json.dumps({"instruction": str(instruction), "armed_at": time.time()})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except OSError:
        pass


def disarm(session_id):
    """Clear this session's latch. Best-effort; a missing file is success."""
    path = _path(session_id)
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def disarm_unless_continuation(session_id, prompt):
    """A prompt arrived. Clear the latch UNLESS this is the continuation's own
    injected text — see the module note: only a DIFFERENT prompt means the user is
    back, and on an interrupted turn it is the only signal there will be."""
    armed = armed_instruction(session_id)
    if not armed:
        return
    if (prompt or "").strip() == armed.strip():
        return
    disarm(session_id)
