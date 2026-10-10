"""Bind the host PROCESS to its live session id, so the stdio MCP proxy can
forward it as `X-Session-Id` (#747).

Every hook payload carries the session id; the MCP proxy's launch does not —
no host passes one to an MCP server it launches, and the id ROTATES
under a running proxy (`/clear`, a resume). Without it a tool call cannot read
or stamp this session's delivery ledger, so `state: unread` would mean "unread
by nobody" and a full fetch would never move a record to `held`.

Hooks and the proxy share exactly one thing: the host process that spawned
both. So a hook writes `<state>/host-sessions/<host key>` = its session id, and
the proxy reads the file for ITS host on every request (a rotation is picked up
on the next call). The key is the nearest ancestor that is not a launcher shell
or interpreter, plus that process's start time, so a recycled pid can never
inherit a dead session's id.

Fails open everywhere: no ancestry (Windows, a reparented detached child), an
unreadable store, a malformed id — the proxy then sends no header and the
server treats the call as session-less, exactly as before.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

# Launchers between the host and our Python: the hooks.json fallback one-liner,
# run.sh (it execs, but a platform shell may not), and the interpreter itself.
_LAUNCHERS = frozenset(
    {"sh", "bash", "dash", "zsh", "fish", "ksh", "env", "run.sh", "python", "python3", "py", "timeout"}
)
_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_MAX_DEPTH = 8


def _store_dir() -> str:
    base = os.environ.get("NEURONZAI_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".neuronzai")
    return os.path.join(base, "host-sessions")


def _proc_linux(pid: int):
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
        raw = handle.read()
    # comm is parenthesised and may itself contain spaces or ')'.
    comm = raw[raw.index("(") + 1 : raw.rindex(")")]
    fields = raw[raw.rindex(")") + 2 :].split()
    return int(fields[1]), comm, fields[19]


def _proc_ps(pid: int):
    out = subprocess.run(
        ["ps", "-o", "ppid=,lstart=,comm=", "-p", str(pid)],
        capture_output=True, text=True, timeout=2, check=False,
    ).stdout.split()
    if len(out) < 7:
        return None
    return int(out[0]), os.path.basename(" ".join(out[6:])), "-".join(out[1:6])


def _proc(pid: int):
    try:
        if os.path.isdir("/proc"):
            return _proc_linux(pid)
        if os.name == "posix":
            return _proc_ps(pid)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    return None


def host_key(start_pid: int | None = None) -> str | None:
    """`<pid>-<start>` of the nearest non-launcher ancestor, or None."""
    pid = os.getppid() if start_pid is None else start_pid
    for _ in range(_MAX_DEPTH):
        if pid <= 1:
            return None
        info = _proc(pid)
        if info is None:
            return None
        ppid, comm, start = info
        if os.path.basename(comm) not in _LAUNCHERS:
            return f"{pid}-{re.sub(r'[^A-Za-z0-9:-]', '', start)}"
        pid = ppid
    return None


def valid_session_id(session_id: str) -> bool:
    return bool(session_id) and "${" not in session_id and bool(_SESSION_ID.match(session_id))


def record(session_id: str, key: str | None = None) -> None:
    """Hook side: bind this host to `session_id`. Writes only on a change."""
    session_id = (session_id or "").strip()
    if not valid_session_id(session_id):
        return
    key = key or host_key()
    if not key:
        return
    path = os.path.join(_store_dir(), key)
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.read().strip() == session_id:
                return
    except OSError:
        pass
    try:
        os.makedirs(_store_dir(), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(session_id)
        os.replace(tmp, path)
    except OSError:
        return
    _prune(_store_dir())


_PRUNE_AFTER_SECONDS = 14 * 86_400


def _prune(directory: str) -> None:
    """Bindings of hosts long gone; one file per host process, so bounded."""
    try:
        cutoff = time.time() - _PRUNE_AFTER_SECONDS
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        return


def lookup(key: str | None) -> str | None:
    """Proxy side: the session id currently bound to this host, or None."""
    if not key:
        return None
    try:
        with open(os.path.join(_store_dir(), key), encoding="utf-8") as handle:
            session_id = handle.read().strip()
    except OSError:
        return None
    return session_id if valid_session_id(session_id) else None
