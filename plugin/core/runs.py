#!/usr/bin/env python3
# SessionEnd handler (host-agnostic) — records ONE "run" action per
# neuronzai-DISTRIBUTED asset (skill or command) invocation in the session, so
# every skill/command execution leaves a traceable, auditable trail. This is the
# single lifecycle implementation dispatched by entry.py for every host.
#
# A run is a kind='run' action on the server, tied to the asset that produced it
# (POST /api/assets/<slug>/run); the server generates the run's outcome title from
# the ledger slice we send. This is the hook-driven half of the tasks->assets fold:
# tasks are gone; their traceability now rides on assets.
#
# How invocations are detected — from the host's NORMALIZED transcript entries
# (host.iter_transcript yields {"role", "texts", "tool_uses"} in file/append order,
# so the transcript's on-disk STRUCTURE stays behind the host, never in core):
#   - SKILL:   a tool_use named "Skill" with input {"skill": "<slug>"}.
#   - COMMAND: a user `texts` entry whose text starts with `/<slug>` (best-effort
#              heuristic; a user-typed slash command is a prompt expansion, not a
#              tool_use, so if a host renders it differently, command runs simply
#              aren't recorded and skills still are; never crashes).
# Tool calls that follow an invocation (until the next invocation) form that run's
# ledger slice. A call is consequential if it edits files, pushes/commits/deploys,
# or is an MCP call (mcp__*) — the outward work a job does; pure reads
# (Read/Grep/Glob/WebFetch/plain Bash) are ignored, so a read-only skill records no
# run (the SERVER also skips an empty ledger — the noise gate, no per-asset flag).
#
# Ownership gate: only assets neuronzai itself shipped are recorded — a skill whose
# host skill/command link is a symlink resolving INTO our per-profile store
# (<config_home>/neuronzai-assets/<profileKey>/{skills,commands}, or a supported
# legacy flat store) — the SAME gate the asset-sync
# materializes with. The config home is HOST-resolved (host.config_home()); core
# never hardcodes a path.
#
# Strictly best-effort, fire-and-forget: runs as the session ends, must NEVER block.
# Any failure (server down, timeout, auth, bad transcript) returns None with no
# output. Kill switch: NEURONZAI_RUNS=0 (or false/no) disables run recording.
# Pure stdlib.

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
from pathlib import Path

from core import api, profile as profile_mod, progress
from core.hostapi import Event, Host, Output
from core.assets import effective_target_dir

MAX_RUNS = 50  # cap network calls at session end; a real session invokes few assets
DETAIL_MAX_CHARS = 200
TIMEOUT_S = 4
SLASH_RE = re.compile(r"^\s*/([a-z0-9][a-z0-9-]*)\b")
RUN_ASSET_DIRS = {"skill": "skills", "command": "commands"}


def _enabled():
    # Kill switch: NEURONZAI_RUNS=0 (or false/no) disables run recording. Resolved
    # per call (not a module constant) so a test / env change is live.
    return (os.environ.get("NEURONZAI_RUNS") or "1").strip().lower() not in ("0", "false", "no")


def owned_kind(kind, slug, host, cwd=""):
    # An asset is OURS iff the link the materializer wrote is a symlink resolving INTO
    # one of our store roots for that kind — the exact ownership gate the asset-sync
    # uses. The LINK dir is host+cwd-specific (a project-scoped host puts it in a
    # repo-local dir), so resolve it through the SAME effective_target_dir the materializer
    # uses — never rebuild it as config_home/<kind> (wrong on a host that materializes
    # per-cwd). The STORE lives under config_home: neuronzai-assets/<profileKey>/
    # {skills,commands} (+ supported legacy flat stores).
    home = Path(host.config_home())
    target_dir = effective_target_dir(host, kind, cwd)
    if not target_dir:
        return None
    link = target_dir / (slug if host.asset_is_directory(kind) else f"{slug}.md")
    try:
        if not link.is_symlink():
            return None
        target = link.resolve()
        # Current layout is per profile:
        #   neuronzai-assets/<profileKey>/<skills|commands>/<item>
        # Keep accepting the pre-profile flat layout and the oldest skills-only
        # store during upgrades, while rejecting arbitrary paths merely placed
        # somewhere below neuronzai-assets.
        if kind == "skill":
            legacy = (home / "neuronzai-skills").resolve()
            if target == legacy or legacy in target.parents:
                return "skill"
        root = (home / "neuronzai-assets").resolve()
        rel = target.relative_to(root)
        parts = rel.parts
        for source_kind, directory in RUN_ASSET_DIRS.items():
            if parts and (parts[0] == directory or (len(parts) > 1 and parts[1] == directory)):
                return source_kind
        return None
    except ValueError:
        return None
    except OSError:
        return None


def is_owned(kind, slug, host, cwd=""):
    return owned_kind(kind, slug, host, cwd) is not None


def consequential(tool_name, tool_input):
    # Map a tool call to ONE ledger trigger, or (None, None) for a non-consequential
    # (read-only) call. Mirrors the capture taxonomy, widened with MCP calls so an
    # MCP-driven job (e.g. a Slack->Notion sweep) registers as real work.
    if tool_name == "Bash":
        command = str((tool_input or {}).get("command") or "")
        if re.search(r"\bgit\s+push\b", command):
            return "git_push", command
        if re.search(r"\bgit\s+commit\b", command):
            return "git_commit", command
        if re.search(r"\b(docker\s+push|kubectl\s+apply|helm\s+upgrade|terraform\s+apply)\b", command):
            return "deploy", command
        return None, None
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        path = str((tool_input or {}).get("file_path") or "")
        return ("file_edit", path) if path else (None, None)
    if tool_name.startswith("mcp__"):
        return tool_name, ""
    return None, None


def _walk(entries):
    # Yield, in transcript order, ("invoke", kind, slug) for each asset invocation
    # and ("tool", trigger, detail) for each consequential tool call, consuming the
    # host's NORMALIZED entries ({"role", "texts", "tool_uses"}). Tolerant of a
    # missing key on any entry; never raises.
    for entry in entries:
        entry = entry or {}
        if entry.get("role") == "user":
            for text in entry.get("texts") or []:
                m = SLASH_RE.match(str(text or ""))
                if m:
                    yield ("invoke", "command", m.group(1))
        for use in entry.get("tool_uses") or []:
            use = use or {}
            name = str(use.get("name") or "")
            if name == "Skill":
                slug = str((use.get("input") or {}).get("skill") or "").strip()
                if slug:
                    yield ("invoke", "skill", slug)
            else:
                trigger, detail = consequential(name, use.get("input") or {})
                if trigger:
                    yield ("tool", trigger, detail)


def collect_runs(entries):
    # Group consequential tool calls under the most recent asset invocation. Calls
    # before any invocation belong to no asset (the session sweep covers those).
    runs = []
    current = None
    for ev in _walk(entries):
        if ev[0] == "invoke":
            current = {"kind": ev[1], "slug": ev[2], "ledger": []}
            runs.append(current)
        elif ev[0] == "tool" and current is not None:
            current["ledger"].append(
                {"trigger": ev[1], "tool": ev[1], "detail": (ev[2] or "")[:DETAIL_MAX_CHARS]}
            )
    return runs


def _revision(run):
    """Privacy-preserving checkpoint for one invocation's growing ledger."""
    payload = json.dumps(
        {"runKey": run["runKey"], "ledger": run["ledger"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def handle(event: Event, host: Host) -> Output | None:
    """Record ONE run per owned skill/command invocation found in the transcript.
    Returns None (side-effect POST hook — no model-visible output). Best-effort: any
    failure is swallowed by core.api, so a down server never blocks session end."""
    # Shared installed hooks expose the Stop fallback to every host. Hosts with a
    # real SessionEnd retain the original once-at-end behavior.
    if event.event == "stop" and host.caps.has_session_end:
        return None
    if not _enabled():
        return None
    session_id = (event.session_id or "").strip()
    transcript_path = (event.transcript_path or "").strip()
    if not session_id or not transcript_path:
        return None

    # The invocation ordinal is stable because transcripts are append-only. It
    # becomes the server-side upsert key, allowing a per-turn Stop fallback to
    # refresh the same run as its ledger grows instead of inserting duplicates.
    runs = []
    for ordinal, run in enumerate(collect_runs(host.iter_transcript(transcript_path))):
        source_kind = owned_kind(run["kind"], run["slug"], host, event.cwd)
        if not run["ledger"] or not source_kind:
            continue
        runs.append({**run, "kind": source_kind,
                     "runKey": f"{source_kind}:{run['slug']}:{ordinal}"})
    runs = runs[:MAX_RUNS]
    if not runs:
        return None

    counts = None
    if event.event == "stop" and not host.caps.has_session_end:
        # A fallback Stop fires after every turn. Avoid even a network request when a
        # run's ledger is unchanged; a growing ledger yields a new revision and
        # refreshes the same server row via runKey. Commit only after every POST
        # succeeds so transient failures are retried on the next Stop.
        revisions = [_revision(run) for run in runs]
        unseen, counts = progress.unseen(session_id, "runs-record", revisions)
        pending = set(unseen)
        runs = [run for run, revision in zip(runs, revisions) if revision in pending]
        if not runs:
            return None

    # Precedence: /switch-profile session override > NEURONZAI_PROFILE env > cwd.
    # Under an override resolve() returns cwd="" so post sends no ?cwd= (no anchor)
    # and the run action is recorded against the switched profile.
    profile, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)
    params = {"cwd": cwd} if (not profile and cwd) else None

    succeeded = True
    for r in runs:
        # api.post is best-effort (never raises) and prints the auth-fail note to
        # stderr on 401/403 with the where= label. Stop after the first failure:
        # auth failures otherwise repeat the same doomed request + stderr warning
        # for every detected run (up to MAX_RUNS).
        path = f"/api/assets/{urllib.parse.quote(r['slug'])}/run"
        response = api.post(
            path,
            params=params,
            body={
                "status": "ok",
                "sessionId": session_id,
                "runKey": r["runKey"],
                "ledger": r["ledger"],
            },
            profile=profile,
            timeout=TIMEOUT_S,
            where="runs_record",
        )
        if response is None:
            succeeded = False
            break
    if counts is not None and succeeded:
        progress.commit(session_id, "runs-record", counts)
    return None
