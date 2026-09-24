#!/usr/bin/env python3
# Envelope measurement (#494) — size the exact context handed to the host
# against that host's measured inline-injection limit.
#
# Every context lane budgets itself, but nothing budgets their assembled sum. A
# real 60,272-byte UserPromptSubmit payload therefore crossed the host boundary,
# spilled to a side file, and reached the model only as a short preview. This
# module supplies the missing end-to-end number, and — when the assembled context
# exceeds a host's MEASURED limit — the over-limit text the caller raises as a
# user-facing systemMessage (never model context), so a silent spill becomes loud
# without the measurement ever altering the delivery it observes.
#
# The hook hot path does no network I/O. Each measurement is one compact local
# spool append. The terminal hook atomically hands that spool to a detached child,
# which posts the session's records to POST /api/analytics/envelope. Network
# failure can lose observability, never delay or change the context emitted to the
# host.

import hashlib
import json
import os
import subprocess
import sys
import time
from typing import Optional

from core import api, profile as profile_mod

ENABLED = (os.environ.get("NEURONZAI_ANALYTICS") or "1").strip().lower() not in ("0", "false", "no")

_LIMIT_OVERRIDE_VAR = "NEURONZAI_INLINE_CONTEXT_LIMIT_CHARS"
_SPOOL_DIRECTORY = "context-envelope-pending"
_FLUSH_CHUNK = 500  # keep each POST at/under the server's per-request record cap
_SPOOL_MAX_AGE_SECONDS = 24 * 3600  # reap abandoned spool/claim files older than this


def limit_for(caps, env=None) -> int:
    """Return the positive per-field override, else the host's per-prompt injection budget."""
    env = os.environ if env is None else env
    raw = (env.get(_LIMIT_OVERRIDE_VAR) or "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return int(getattr(caps, "inline_context_limit_chars", 0) or 0)


def measure(context: Optional[str], limit_chars: int) -> Optional[dict]:
    """Size context in characters and bytes; a limit <= 0 disables the budget (no verdict)."""
    if not context or limit_chars <= 0:
        return None
    chars = len(context)
    return {
        "chars": chars,
        "bytes": len(context.encode("utf-8")),
        "limitChars": limit_chars,
        "over": chars > limit_chars,
        "overByChars": max(0, chars - limit_chars),
    }


def _state_directory() -> str:
    return os.environ.get("NEURONZAI_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".neuronzai"
    )


def _spool_path(session_id: str) -> str:
    safe_session = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return os.path.join(_state_directory(), _SPOOL_DIRECTORY, f"{safe_session}.jsonl")


def _append_record(session_id: str, record: dict) -> None:
    path = _spool_path(session_id)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    payload = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short context-envelope spool write")
    finally:
        os.close(descriptor)


def _detached_process_options() -> dict:
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def _plugin_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _restore_claim(claim_path: str, pending_path: str) -> None:
    try:
        with open(claim_path, "rb") as source:
            payload = source.read()
        descriptor = os.open(
            pending_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        os.remove(claim_path)
    except OSError:
        pass


def _flush_claim(claim_path: str, pending_path: str, session_id: str) -> None:
    records = []
    try:
        with open(claim_path, "r", encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
        if not records:
            os.remove(claim_path)
            return
        # Post in <=_FLUSH_CHUNK batches so a long session never exceeds the
        # server's per-request cap (which would 400 and, via _restore_claim, strand
        # the spool). The claim is dropped only once EVERY chunk is accepted; any
        # failure restores the whole claim for the next terminal event to retry.
        for start in range(0, len(records), _FLUSH_CHUNK):
            response = api.post(
                "/api/analytics/envelope",
                body={
                    "session": session_id,
                    "records": records[start : start + _FLUSH_CHUNK],
                },
                session_id=session_id,
                timeout=2,
                where="envelope",
            )
            if response is None:
                _restore_claim(claim_path, pending_path)
                return
        os.remove(claim_path)
        return
    except Exception:
        pass
    _restore_claim(claim_path, pending_path)


def _reap_stale_spools() -> None:
    """Delete abandoned spool + claim files so an offline or unauthenticated
    session cannot accrue one dead file per session forever. A transport failure
    restores the claim to ``<session>.jsonl``, but the terminal event that would
    re-flush it has already fired and nothing else reaps this directory. Runs on
    every terminal flush; only files older than the TTL are removed, so a
    concurrent session's in-flight claim is never touched. Fail-open."""
    directory = os.path.join(_state_directory(), _SPOOL_DIRECTORY)
    cutoff = time.time() - _SPOOL_MAX_AGE_SECONDS
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if not (entry.name.endswith(".jsonl") or entry.name.endswith(".sending")):
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
        except OSError:
            continue


def _flush_async(session_id: str) -> None:
    _reap_stale_spools()
    pending_path = _spool_path(session_id)
    claim_path = f"{pending_path}.{os.getpid()}.sending"
    try:
        os.replace(pending_path, claim_path)
    except FileNotFoundError:
        return
    except OSError:
        return

    child = [
        sys.executable,
        "-B",
        "-m",
        "core.envelope",
        "--flush",
        claim_path,
        pending_path,
        session_id,
    ]
    try:
        subprocess.Popen(
            child,
            cwd=_plugin_root(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_detached_process_options(),
        )
    except Exception:
        _restore_claim(claim_path, pending_path)


def over_limit_notice(sized: dict, host) -> str:
    """User-facing (never model-context) warning that the assembled context exceeds
    this host's inline-injection warning THRESHOLD — a frugality guard, NOT an
    enforced cap: nothing is trimmed, the delivery this observes is never altered.
    The CONSEQUENCE is worded from `host.caps.side_files_over_inline_limit`: a
    side-filing host shows the model only a truncated preview, so the overflow is
    silent DATA LOSS (#494); a host that delivers the context verbatim only wastes
    the model's context window (#519). `host` is REQUIRED, and an undeterminable
    capability errs toward the data-loss wording — a spillage warning must never
    reassure the user on a guess."""
    caps = getattr(host, "caps", None)
    # Undeterminable -> assume side-filing (data loss): never reassure on a guess.
    if getattr(caps, "side_files_over_inline_limit", True):
        consequence = "the model may receive only a truncated preview"
    else:
        consequence = "nothing is dropped, but it needlessly consumes the model's context window"
    return (
        f"⚠️ Neuronz.ai: this turn's assembled context is {sized['chars']} characters, "
        f"over this host's {sized['limitChars']}-character inline-injection warning "
        f"threshold by {sized['overByChars']} — {consequence}. Reduce the assembled "
        f"context in Workspace Settings (the per-prompt injection budget, or the "
        f"session-start chunk budget)."
    )


def report(canonical_event: str, output, host, event=None) -> Optional[dict]:
    """Spool one emission and detach the session spool at its terminal event.

    Returns the sized measurement when a context was measured (so the caller can
    raise an over-limit signal), else None. Fail-open: never raises, never blocks.
    """
    if not ENABLED:
        return None
    sized = None
    try:
        session_id = getattr(event, "session_id", "") or ""
        if not session_id:
            return None
        is_terminal = canonical_event == "session_end" or (
            canonical_event == "stop" and not host.caps.has_session_end
        )
        if output is not None:
            sized = measure(getattr(output, "context", None), limit_for(host.caps))
            if sized is not None:
                profile, cwd = profile_mod.resolve(
                    session_id,
                    getattr(event, "cwd", "") or "",
                    api.ENV_PROFILE,
                )
                _append_record(
                    session_id,
                    dict(
                        sized,
                        event=canonical_event,
                        host=host.name,
                        profile=profile,
                        cwd=cwd,
                    ),
                )
        if is_terminal:
            _flush_async(session_id)
    except Exception:
        return None
    return sized


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "--flush":
    _flush_claim(sys.argv[2], sys.argv[3], sys.argv[4])
