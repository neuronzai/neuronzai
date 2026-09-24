#!/usr/bin/env python3
# Transcript -> EVIDENCE payload for the detached capture run.
#
# This renders transcript evidence for the detached AGENT that will act on it, and
# an agent needs what the prose alone never says: the
# model writes "let me check" and the TOOL RESULT is what actually settled it.
# Keeping results is what lets the capture run mark a fact `verified` from evidence
# rather than `verified` from a guess.
#
# Keeping results also re-opens a self-ingestion door. Our own injected blocks — the
# operating card, per-prompt recall, rule gates, persisted-output previews — are
# echoed back INSIDE tool results, so admitting results re-admits our instructions
# as if they were session content, which is the self-ingestion bug from the other
# direction. Everything here therefore passes through _strip_scaffolding, prose and
# results alike.
#
# Measured over the 12 largest real transcripts (197 MB raw): the payload is 10% of
# the file. Two of the twelve still contain one of our markers, and both times it is
# inside a tool INPUT — a shell command that greps for the literal string, in a
# session that was building this machinery. Tool inputs are deliberately NOT
# stripped: an input is something the agent typed, so it is always content, and
# cutting it would destroy real commands to remove nothing.
#
# Pure stdlib. Host-agnostic: every transcript shape is behind host.iter_transcript.

import json
import re

# Only a human turn or the model's own words are session content.
CONVERSATION_ROLES = ("user", "assistant")

# Injected blocks that arrive as a properly closed tag pair.
SYSTEM_SPAN = re.compile(
    r"<(system-reminder|active-rules|active-topic|memory-context|persona|persisted-output)>"
    r".*?</\1>", re.DOTALL)

# The same blocks when the text was itself truncated first: a persisted-output
# preview cuts at ~2KB, so the opening tag survives and the closing one does not,
# and the paired pattern above can never match it. Drop from the orphaned opener to
# the end of the text — everything after it is the tail of an injected block.
UNCLOSED_SPAN = re.compile(
    r"<(system-reminder|active-rules|active-topic|memory-context|persona|persisted-output)>"
    r"(?!.*</\1>).*", re.DOTALL)

# Hook payloads echoed as plain text with no enclosing tag at all. Each alternative
# is a marker we emit ourselves; everything from it to the end of its block goes.
#
# The opener is `[^\n]*?`, NOT `.*?`. It exists to take the marker's own LINE from
# its start (a marker can sit mid-line), and `[^\n]` is what confines it to that
# line. With a dot there — `(?ms)^.*?` — the leftmost `^` is position 0 and the lazy
# run crosses newlines to reach the marker, so a single marker anywhere deletes
# EVERYTHING before it. That silently ate real evidence in the case this module is
# built for: `"command output\n\n### RULES DIGEST\n- rule\n\ndone"` kept only `done`.
# It is also self-inflicted — a Read or Grep over this repo's own docs returns text
# containing these very markers. The tagged-span patterns never had the bug, which
# is what made the two strippers disagree.
BARE_SCAFFOLD = re.compile(
    r"(?ms)^[^\n]*?(?:PINNED authoritative rules|### RULES DIGEST|### REPO BRIEF"
    r"|\[TOPIC MODE|Persistent recall —|operating card omitted"
    r"|Output too large \(\d+(?:\.\d+)?KB\)\. Full output saved to).*?(?:\n\n|\Z)")

# Per-item bounds. RESULT_CAP is set where it is because 93% of real tool results
# are shorter than it, so the common case rides whole and only the pathological
# ones (a full file read, a 10k-line log) are cut.
RESULT_CAP = 5000
INPUT_CAP = 400      # enough to know what was asked of the tool
PROSE_CAP = 20000    # one turn must not drown out the session

# Whole-payload bound. The window is far larger than this, but the payload is ONE
# user message and the run has to finish before the machine sleeps. Past the cap we
# keep the TAIL: a session's conclusions are at its end.
PAYLOAD_CAP = 400_000

# Below this there is nothing durable to find, and a run costs the same whether or
# not it finds anything.
MIN_PAYLOAD_CHARS = 2000

# ---- The crop ---------------------------------------------------------------
#
# In-session capture and this run are the same job at two moments: a checkpoint
# while the work is live, and the closer once it is over. Without a crop the closer
# re-reads everything the checkpoints already took — a session whose checkpoints
# fired at message 92 of 100 pays to re-read 92 messages to find 8 messages' worth
# of new material.
#
# The transcript is self-describing about its own capture history: every in-session
# write carries a `sweepId` in the tool input, so a stamped tool call IS the receipt
# that the material up to that point has already been consolidated. Nothing has to
# be tracked on the side, which matters because side-state and the transcript drift
# the moment a session is resumed, branched or replayed.
#
# What makes this safe is the COMPLETION test. A stamped call proves a write
# happened, not that the capture finished — a user who hits escape mid-capture
# leaves a half-written region that a naive reader would mark as fully captured, and
# the material in it would then be dropped by BOTH lanes and lost for good. So a
# marker only counts when nothing interrupted it before the conversation moved on.
# Erring here is asymmetric: re-reading a captured region costs tokens, losing an
# uncaptured one costs the record.
STAMP_KEY = "sweepId"

# The transcript's own text for a turn the user cut short. This is a CONTENT
# signature, not a host branch — a host whose transcripts word it differently adds
# its spelling here and the completion test keeps working unchanged.
INTERRUPT_MARKS = ("[Request interrupted by user]",)

# Kept before the cut so pronouns still have referents: the delta opens mid-thread,
# and "fix that too" is unreadable without the turn that said what "that" is.
OVERLAP_USER_TURNS = 2

# A cropped payload opens mid-conversation, which looks like a truncated transcript
# unless it says otherwise. Told plainly, the reader stops trying to reconstruct the
# opening and stops re-recording what the earlier turns already produced.
CROP_NOTE = ("[the earlier turns of this session were already consolidated into "
             "memory while it ran; what follows is the remainder, opening a couple "
             "of turns early for context]")


def _strip_scaffolding(text):
    stripped = SYSTEM_SPAN.sub("", text or "")
    stripped = UNCLOSED_SPAN.sub("", stripped)
    stripped = BARE_SCAFFOLD.sub("", stripped)
    return stripped.strip()


def _clip(text, cap):
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap] + "\n…[+%d chars]" % (len(text) - cap)


def _clip_clean(text, cap):
    return _clip(_strip_scaffolding(text), cap)


def _is_conversation(entry):
    return (not entry.get("meta") and not entry.get("sidechain")
            and (entry.get("role") or "") in CONVERSATION_ROLES)


def _is_stamped(entry):
    """A memory write from an in-session capture — the receipt this run reads."""
    for call in entry.get("tool_uses") or []:
        payload = call.get("input")
        if isinstance(payload, dict) and STAMP_KEY in payload:
            return True
    return False


def _is_interrupt(entry):
    texts = "\n".join(str(text) for text in entry.get("texts") or [])
    return any(mark in texts for mark in INTERRUPT_MARKS)


def _is_user_turn(entry):
    """A human turn — not a tool result, which wears the same role."""
    return (entry.get("role") == "user"
            and any(str(text).strip() for text in entry.get("texts") or []))


def _scan(host, transcript_path):
    """One cheap pass: the three flags per conversation entry that crop needs.

    Deliberately NOT a materialized entry list. A large transcript is hundreds of
    megabytes and every string in it would be held at once; three booleans per entry
    costs the second read of the file instead, which a detached run can afford and a
    machine running low on memory cannot.
    """
    return [(_is_stamped(entry), _is_interrupt(entry), _is_user_turn(entry))
            for entry in host.iter_transcript(transcript_path)
            if _is_conversation(entry)]


def crop_start(flags):
    """Index of the first entry to keep. 0 means keep the whole session.

    A stamped write counts as a capture only if the conversation moved on from it
    without an interruption: scanning forward, an interrupt mark disqualifies it, and
    the next genuine user turn confirms it. An interrupt mark is itself a user turn,
    so it is tested FIRST — otherwise a cancelled capture would confirm itself.
    """
    def completed(index):
        for _stamped, interrupt, user in flags[index + 1:]:
            if interrupt:
                return False
            if user:
                return True
        return True                   # the session ended on it, uninterrupted

    marker = -1
    for index, (stamped, _interrupt, _user) in enumerate(flags):
        if stamped and completed(index):
            marker = index
    if marker < 0:
        return 0
    user_turns = [i for i, (_s, _i, user) in enumerate(flags[:marker + 1]) if user]
    if len(user_turns) <= OVERLAP_USER_TURNS:
        return 0                      # nothing left to gain; keep it whole
    return user_turns[-OVERLAP_USER_TURNS]


def render(host, transcript_path, crop=True):
    """The session's evidence as `role: text`, `→ tool(input)`, `← result` lines.

    With `crop` on (the default) the payload starts after the last COMPLETED
    in-session capture, so this run reads only what no other lane has taken. Pass
    `crop=False` for the whole session — what the caller wants when it is measuring
    the crop, or when the delta is not what it is after.

    Returns "" for anything unusable — an unreadable transcript, a session with no
    conversation in it — so the caller's own "too small to bother" check is the one
    place that decides whether a run happens.
    """
    start = crop_start(_scan(host, transcript_path)) if crop else 0

    lines, index = ([CROP_NOTE] if start else []), -1
    for entry in host.iter_transcript(transcript_path):
        if not _is_conversation(entry):
            continue
        index += 1
        if index < start:
            continue

        prose = _strip_scaffolding("\n".join(
            str(text) for text in entry.get("texts") or []))
        if prose:
            lines.append("%s: %s" % (entry.get("role") or "", _clip(prose, PROSE_CAP)))

        for call in entry.get("tool_uses") or []:
            shown = _clip(json.dumps(call.get("input") or {}, ensure_ascii=False), INPUT_CAP)
            lines.append("  → %s(%s)" % (call.get("name") or "?", shown))

        for result in entry.get("tool_results") or []:
            body = _clip_clean(str(result), RESULT_CAP)
            if body:
                lines.append("  ← %s" % body)

    return "\n".join(line for line in lines if line.strip())


def bound(payload):
    """Trim an over-long payload to its tail. Returns (payload, dropped_chars)."""
    if len(payload) <= PAYLOAD_CAP:
        return payload, 0
    return payload[-PAYLOAD_CAP:], len(payload) - PAYLOAD_CAP
