#!/usr/bin/env python3
# Shared cwd -> profile-key resolution for the Neuronz.ai hooks.
#
# A Claude Code session can run inside a git WORKTREE: the built-in worktree
# isolation puts these under the system temp dir (e.g. /tmp/<repo>-<n>), and a
# manual `git worktree add` puts them as siblings (e.g. ~/dev/<repo>-<feature>).
# The server keys a profile off the working directory by longest-prefix match
# over profile_routes, and a worktree path is NEVER a child of the main repo's
# path -- so it matches no route and the server auto-creates a throwaway profile
# per worktree, fragmenting the project's memory across ghost profiles.
#
# The fix lives here, CLIENT-side, because mapping a worktree back to its repo
# needs the worktree's local .git (a filesystem lookup the remote server cannot
# do): we rewrite the cwd to the MAIN worktree root via `git rev-parse
# --git-common-dir` before it is sent, so every worktree -- and every subdir of
# one -- resolves to the repo's own profile. Pure stdlib; fails SAFE: returns the
# input unchanged on a non-git dir, missing/old git, or any error, so resolution
# is never worse than the raw cwd.

import os
import subprocess


def raw_cwd(payload):
    """The session working dir as Claude Code REPORTS it: the hook stdin payload's
    `cwd`, then $CLAUDE_PROJECT_DIR. Stripped, "" when neither resolves.

    Both legs are values the HOST states. The process cwd was a third leg and is
    gone (#383): the hook process's own directory is a value WE picked, so it turns
    "the host told us nothing" into a confident claim about where the session is.
    The plugin's host adapters dropped the same leg for the same reason, and "" is
    the honest answer here — the one caller left (the topic status line) already
    omits X-Cwd for it rather than resolving some other directory's profile."""
    return str(
        payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ""
    ).strip()


def canonical_cwd(cwd):
    """Map `cwd` to its git MAIN-worktree root so every linked worktree (and
    every subdir of one) resolves to the same profile. `--path-format=absolute`
    makes `--git-common-dir` return the absolute path to the MAIN repo's `.git`
    even from a linked worktree or a deep subdir (where a plain --git-dir would
    point inside .git/worktrees/<id>); its parent is the canonical project dir.
    Returns `cwd` unchanged when it is empty, not a directory, not a git repo, or
    git is unavailable -- never raises."""
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
    # common is the absolute path to the main repo's .git; its parent is the main
    # worktree root (the canonical project dir the profile routes are keyed on).
    root = os.path.dirname(common.rstrip("/"))
    return root or cwd


def session_cwd(payload):
    """raw_cwd(payload) canonicalized to the git main-worktree root -- the value
    a hook should send as ?cwd= / X-Cwd for profile resolution."""
    return canonical_cwd(raw_cwd(payload))


def session_override(payload):
    """The /switch-profile profile override for THIS session, or None. Reads the
    session id from the hook payload and looks it up in the client-side override
    store (neuronzai_session, same hooks dir). Lazy import + fails SAFE (None) if
    that module/file is missing or unreadable, so a hook never breaks on it."""
    try:
        import neuronzai_session

        sid = str((payload or {}).get("session_id") or "").strip()
        return neuronzai_session.read_profile(sid)
    except Exception:
        return None


def session_scope(payload, env_profile=""):
    """The (profile, cwd) a hook should send for this request, honoring a
    /switch-profile session override. Precedence: session override >
    NEURONZAI_PROFILE env > cwd.
      - override active -> (override, "")   # X-Profile only; NO cwd, so NO
                                            #   (profile, cwd) anchor and NO route
                                            #   is ever written for the directory
      - else            -> (env_profile or "", session_cwd(payload))
    The cwd is deliberately EMPTY under an override because several hooks send
    X-Cwd as a durable (profile, cwd) anchor (#41); suppressing it keeps a
    /switch-profile purely session-scoped -- it re-scopes what the session
    writes/reads WITHOUT mapping the directory to the chosen profile."""
    override = session_override(payload)
    if override:
        return override, ""
    return (env_profile or "").strip(), session_cwd(payload)
