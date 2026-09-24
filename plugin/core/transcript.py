#!/usr/bin/env python3
# Shared, host-agnostic transcript READ helpers (#344).
#
# Detached capture and several lifecycle receipts read transcript material outside
# the per-prompt hot path. Recall cannot afford a whole-file read: it runs on
# UserPromptSubmit under a hard timeout, and one long session's transcript is tens
# of megabytes (measured: 15 MB after a day's work). This module adds a bounded TAIL
# read that goes through the SAME host parser
# (host.iter_transcript), so exactly one place in the plugin knows a host's
# transcript shape.
#
# The tail read hands the parser an already-positioned FILE DESCRIPTOR instead of a
# path — see tail_entries. That keeps the parser single-sourced: no second JSONL
# reader to drift from the adapters.
#
# Pure stdlib. Nothing here raises: every entry point answers "" / [] on any
# failure, because its only caller is an optimization on a path whose failure would
# cost the user their entire injected context.

import os

# Only the model's own prose can be what the user is replying to.
ASSISTANT_ROLE = "assistant"


def _int_env(name, default):
    try:
        return int(float(os.environ[name]))
    except (KeyError, ValueError):
        return default


# How much of the transcript's tail to parse. Measured over 19 real transcripts
# (238 KB - 15 MB): the line holding the last assistant text block started at most
# 31 KB from EOF, median 5.9 KB — the tail is dominated by big tool-result lines,
# not by prose. 256 KiB is ~8x the observed worst case and reads in ~5 ms on a
# 15 MB file, versus ~290 ms to parse the whole thing.
TAIL_BYTES = _int_env("MEMORY_CTX_TAIL_BYTES", 256 * 1024)


def _tail_start(path, max_bytes):
    """Byte offset of the first COMPLETE line inside the last `max_bytes` of `path`.

    0 when the whole file fits in the window. None when the window holds no line
    boundary at all (one line longer than the window) — there is no complete entry
    to parse, so the caller gives up rather than feeding the parser a fragment.

    Aligning on a newline is not cosmetic: a raw byte seek can land mid-codepoint,
    and the parser decodes strict UTF-8, so an unaligned window raises instead of
    skipping one malformed line.
    """
    size = os.path.getsize(path)
    if size <= max_bytes:
        return 0
    with open(path, "rb") as fh:
        fh.seek(size - max_bytes)
        window = fh.read(max_bytes)
    newline = window.find(b"\n")
    return None if newline < 0 else size - max_bytes + newline + 1


def tail_entries(host, path, max_bytes=None):
    """The host's NORMALIZED transcript entries for (at most) the last `max_bytes`.

    Reuses the adapter's parser by passing it an open file DESCRIPTOR positioned at
    a line boundary: `iter_transcript` opens whatever it is handed, and open()
    accepts an int fd and reads from its current offset. **Ownership of the fd
    transfers to the parser** — open()'s default closefd=True closes it when the
    iterator finishes — so this function never closes it itself: a double close can
    hit an unrelated descriptor, which is a far worse failure than the alternative
    (a parser that raises before opening leaks exactly one descriptor into a hook
    process that exits milliseconds later). The contract-matrix gate pins that every
    adapter accepts an fd. Raises nothing the callers below don't already catch.
    """
    start = _tail_start(path, TAIL_BYTES if max_bytes is None else max_bytes)
    if start is None:
        return []
    fd = os.open(path, os.O_RDONLY)
    if start:
        os.lseek(fd, start, os.SEEK_SET)
    return list(host.iter_transcript(fd))


def previous_assistant_text(host, path, max_chars, max_bytes=None):
    """The last thing the ASSISTANT said before this prompt, bounded to `max_chars`.

    This is the referent of an anaphoric prompt ("go ahead and write it", "do the
    second one"): the words the user is replying to. ONE turn — the most recent
    assistant entry that carried prose — never an accumulated window.

    Excluded for free by the host parser: `thinking` and `tool_use` blocks are not
    `text` blocks, and a `user` entry whose content is a `tool_result` is a TOOL
    turn that yields no assistant text, so it can never end the scan. Excluded
    here: host-synthesized entries (`meta`) and subagent traffic (`sidechain`).

    Deliberately NOT stripped: the `<system-reminder>` spans core/sweep.py cuts out.
    Harness guidance is wrapped inside USER turns, and our own injected blocks ride
    entries that carry no message at all — checked across 19 real transcripts, the
    only assistant-side occurrences of either were the model writing PROSE ABOUT
    them (this repo builds them). Stripping here would therefore only ever destroy
    genuine content.

    Kept from the TAIL when it must be cut — the end of a message is what the
    user's reply attaches to.

    Returns "" for every miss: no transcript, unreadable/absent file, a malformed
    or non-UTF-8 transcript, the first prompt of a session, or an assistant that has
    only run tools so far. The caller then behaves exactly as it did before this
    existed.
    """
    if not path or max_chars <= 0:
        return ""
    try:
        for entry in reversed(tail_entries(host, path, max_bytes)):
            if entry.get("meta") or entry.get("sidechain"):
                continue
            if entry.get("role") != ASSISTANT_ROLE:
                continue
            said = "\n".join(
                block for block in (str(text).strip() for text in entry.get("texts") or [])
                if block
            ).strip()
            if said:
                return said[-max_chars:]
    except Exception:
        return ""
    return ""
