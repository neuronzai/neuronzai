#!/usr/bin/env python3
# Host-agnostic profile/cwd resolution (topic 90ae46be).
#
# The (profile, cwd) a hook sends for a request, honoring a `/switch-profile`
# SESSION override. Precedence: session override > NEURONZAI_PROFILE env > cwd.
#   - override active -> (override, "")   # X-Profile only; NO cwd, so the server
#                                          #   writes NO (profile, cwd) anchor/route
#   - else            -> (env_profile, canonical_cwd(cwd))
# The cwd is canonicalized to its git MAIN-worktree root so every linked worktree
# (and subdir) resolves to the same profile. This is the host-agnostic half of the
# legacy hooks/neuronzai_cwd.session_scope; the HOST resolves the raw cwd (its own
# project-dir env) and puts it on the Event, and the session-override store is keyed
# purely on the session id the host parses from its payload. Pure stdlib, fails SAFE.

import json
import os
import subprocess
import time


_UNREAD = object()


def canonical_cwd(cwd):
    """Map `cwd` to its git main-worktree root. `--git-common-dir` (absolute)
    points at the MAIN repo's .git even from a linked worktree or deep subdir; its
    parent is the canonical project dir the server keys profile routes on. Returns
    `cwd` unchanged when empty / not a dir / not a git repo / git missing."""
    if not cwd or not os.path.isdir(cwd):
        return cwd
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd
    common = proc.stdout.strip()
    if proc.returncode != 0 or not common:
        return cwd
    return os.path.dirname(common.rstrip("/")) or cwd


def head_commit(cwd):
    """The repo's current git HEAD sha, or "" when there isn't one (#18).

    The server has no clone, so this is the only place the CURRENT state of the
    repo can be observed. SessionStart sends it so a stored repo brief written at
    the same sha is recognized as provably fresh instead of merely old, and the
    end-of-session sweep sends it so the facts it mints record which state of the
    code they were true of.

    Same contract as canonical_cwd above: 2s timeout, and every failure (no git,
    not a repo, an empty repo with no commit yet) returns "" rather than raising —
    a missing sha only degrades the hint to age-only, and must never be able to
    break a lifecycle hook.
    """
    if not cwd or not os.path.isdir(cwd):
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 else ""


# HEAD is read as a FILE, not asked for with `git rev-parse --abbrev-ref HEAD`.
# canonical_cwd and head_commit above shell out, which is affordable on the
# once-per-session hooks that call them; the PreToolUse gate fires before EVERY
# tool call, hundreds of times a session, in the path that BLOCKS the agent. A
# process spawn per call is not a cost that path can carry, and the answer is one
# short line in one small text file with a stable documented format.
_HEAD_REF_PREFIX = "ref: refs/heads/"

# HEAD and a `.git` pointer file each hold a single short line. Reading a bounded
# prefix means a corrupt, huge or hostile file at that path cannot be pulled into
# memory whole on the hook that blocks before every tool call.
_GIT_FILE_MAX_BYTES = 4096

# How far up the tree to look for a `.git`. Deep enough for any real checkout, and
# bounded so a pathological path can never spin here.
_GIT_DIR_MAX_DEPTH = 64


def _read_git_file(path):
    """A bounded read of a git metadata file. "" on any failure — never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(_GIT_FILE_MAX_BYTES)
    except (OSError, ValueError):
        return ""


def _git_dir(cwd):
    """The git directory governing `cwd`, found by walking up. "" when there is none.

    A LINKED WORKTREE's `.git` is a FILE holding `gitdir: <path>` that points at
    `<main>/.git/worktrees/<name>` — which is where THAT worktree's own HEAD lives.
    Following the pointer is the whole point of this function: the main worktree's
    HEAD names a different branch, so resolving through it would report the wrong
    branch for every session running in a worktree — and agents run in worktrees.
    """
    current = os.path.abspath(cwd)
    for _ in range(_GIT_DIR_MAX_DEPTH):
        candidate = os.path.join(current, ".git")
        if os.path.isdir(candidate):
            return candidate
        if os.path.isfile(candidate):
            pointer = _read_git_file(candidate).strip()
            if not pointer.startswith("gitdir:"):
                return ""
            target = pointer[len("gitdir:"):].strip()
            if not target:
                return ""
            # A relative pointer is relative to the directory holding the `.git`.
            return target if os.path.isabs(target) else os.path.join(current, target)
        parent = os.path.dirname(current)
        if parent == current:
            return ""
        current = parent
    return ""


def git_branch(cwd):
    """The branch checked out in `cwd`'s worktree, or "" when there is not one (#376).

    An observable the server has no other way to see: it holds no clone, so a rule
    that binds to a branch ("never on staging") can only fire if the client says
    which branch this is. Resolved from the RAW cwd, deliberately — not from
    canonical_cwd's main-worktree root, whose HEAD is a DIFFERENT branch whenever
    the session runs in a linked worktree.

    Every failure returns "" rather than raising, and "" is a correct answer here,
    not a swallowed error: the server treats an absent target as UNOBSERVABLE and
    drops that rule off the lane, which is the right degradation. No git binary, no
    network, no subprocess — see _HEAD_REF_PREFIX for why. Not cached either: a
    session that switches branch mid-flight is exactly when a branch-bound rule
    matters most, and a per-session cache would report the branch it started on.
    """
    if not cwd or not os.path.isdir(cwd):
        return ""
    try:
        git_dir = _git_dir(cwd)
        if not git_dir:
            return ""
        head = _read_git_file(os.path.join(git_dir, "HEAD")).strip()
    except OSError:
        return ""
    if not head.startswith(_HEAD_REF_PREFIX):
        # A DETACHED HEAD holds a bare sha, and a mid-rebase or mid-bisect HEAD can
        # hold other shapes still. None of them is a branch, so nothing is claimed.
        return ""
    return head[len(_HEAD_REF_PREFIX):].strip()


def _override_store_dir():
    base = os.environ.get("NEURONZAI_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".neuronzai"
    )
    return os.path.join(base, "session-profiles")


def _override_path(session_id):
    """Filesystem path of the override file for `session_id`, or None when there is
    no usable session id. Keeps only filename-safe chars so a weird value can never
    escape the store dir. The SAME sanitization the reader and writer share, so a
    file written for a session is the file read back for it."""
    sid = (session_id or "").strip()
    if not sid:
        return None
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    if not safe:
        return None
    return os.path.join(_override_store_dir(), safe + ".json")


def read_override(session_id):
    """The /switch-profile override profile for this session, or None. Keyed on the
    session id the host provides; filename-sanitized so a weird id can't escape the
    store dir. Never raises."""
    path = _override_path(session_id)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return (str(data.get("profile") or "").strip()) or None


def write_override(session_id, profile):
    """Set THIS session's /switch-profile override, atomically. Returns the path
    written. Raises ValueError on a missing session id or an empty profile — the
    switch is meaningless without both. The consumer is read_override (same store,
    same sanitization). Host-agnostic: the session id is whatever the host parsed
    from its hook payload."""
    path = _override_path(session_id)
    if not path:
        raise ValueError("a real session id is required to scope the switch")
    name = (profile or "").strip()
    if not name:
        raise ValueError("a non-empty profile name is required")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"profile": name, "ts": int(time.time())}, handle)
    os.replace(tmp, path)
    _prune()
    return path


def clear_override(session_id):
    """Remove THIS session's override (revert to env/cwd). True if one existed."""
    path = _override_path(session_id)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _prune(max_age_days=14):
    """Best-effort delete of override files older than max_age_days so the store
    never grows without bound (session ids are unique and never recur). Silent on
    any error — pruning is housekeeping, never load-bearing."""
    cutoff = time.time() - max_age_days * 86_400
    directory = _override_store_dir()
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


def resolve(session_id, cwd, env_profile="", override=_UNREAD):
    """(profile, cwd) for this request. Override > env_profile > cwd.

    A caller that also renders the override may pass its already-read value
    (including None) so one hook invocation never reads the state file twice.
    """
    if override is _UNREAD:
        override = read_override(session_id)
    if override:
        return override, ""
    return (env_profile or "").strip(), canonical_cwd(cwd)
