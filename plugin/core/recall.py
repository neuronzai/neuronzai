#!/usr/bin/env python3
# UserPromptSubmit handler (host-agnostic) — fetch the active RULES (the
# authoritative human-owned tier, re-surfaced EVERY turn) + RECALL (memories +
# actions + knowledge) relevant to the prompt and inject them as
# additionalContext, so the agent "already knows" without calling a tool. This is
# the single lifecycle implementation dispatched by entry.py for every host.
#
# GET /api/memory/recall?q=…&k=…&rules=1 (+ session/dedup/cwd) returns persona +
# the active topic, plus — for each of the three delivery lanes — a POINTER, never
# the stored content itself (#561):
#   rules  — `rulesPointer` (#512): a measured `total` and the server-composed
#            `instruction`, which already carries the exact read_rules(…) call to
#            make; names only — no rule text rides.
#   hits   — `hitsPointer` (#434): a measured count and the recall call to make.
#   voice  — `voicePointer` (#495): the layers, the claim count and the exact
#            get_resolved_voice call.
# This renders them into one additionalContext string; a pointer's rendering IS
# its instruction, fenced verbatim, because the server composes every word of it. Best-effort: any failure returns None (never blocks the prompt).
# Session-scoped dedup: forwards session + dedup=1 so the same item is injected AT
# MOST ONCE per session, and a turn with no NEW items injects nothing (novelty
# gate) — under the pointer nothing is injected, so nothing is deduped and the
# count is of what MATCHES, not of what is new. The rules lane has never had a
# novelty gate and still does not: the authoritative tier must stay in force at
# the moment of action, whether it rides as content or as a pointer.
# EXCEPTION — STATE-OF-WORK prompts ("did we…", "where are we", "what's left"):
# recall is widened and dedup is skipped so prior-work records re-surface, and an
# empty result injects an explicit "search before concluding it didn't happen"
# instruction (see depends_on_prior_work).
# ANAPHORIC prompts ("go ahead and write it", "do the second one") carry no
# retrievable content at all, so `ctx` rides along: the previous ASSISTANT message,
# which is what such a prompt refers to. The server fuses it into the query (#344);
# it is strictly a HINT — omitting it is always safe.
#
# Pure stdlib. The (profile, cwd) resolution + /switch-profile override live in
# core/profile; all HTTP goes through core/api.

import os
import re
import urllib.parse

from core import api, profile as profile_mod, sweepturn, topic_mode, transcript
from core.hostapi import Event, Host, Output


def _int_env(name, default):
    try:
        return int(float(os.environ[name]))
    except (KeyError, ValueError):
        return default


K = _int_env("MEMORY_INJECT_K", 5)
# Widened recall budget for STATE-OF-WORK prompts (see depends_on_prior_work). A
# fixed constant, deliberately NOT a server-config knob: it's a hook-local
# retrieval heuristic, not user-facing runtime configuration.
STATE_RECALL_K = 20

# The STATE-OF-WORK framing. It says three things — the stored records answer the
# question, they are NOT in context so they must be read, and "no record found" is
# NOT "it didn't happen" — and the last one is the whole reason this lane exists:
# a fresh session with an empty head will otherwise answer "no, we never did that"
# with total confidence. Telling it to reconcile against records that are not in
# its context would be an instruction it cannot follow, so the wording sends it to
# read them first (#561: the records never ride).
STATE_POINTER_HEADER = (
    "[The user is asking about PRIOR or ONGOING work — what was done, where it stands, or "
    "what's left. The stored records are the AUTHORITATIVE answer and they are NOT in your "
    "context: READ them before replying, then reconcile against them. Answering from what you "
    "remember of this session is how a fresh session says work never happened. Distinguish "
    "'no record found' from 'it didn't happen'.]"
)

# Conversational context budget (#344). Belt-and-braces: the server caps the fused
# text independently (recall_ctx_max_chars), so this only keeps the request small.
CTX_MAX_CHARS = _int_env("MEMORY_CTX_MAX_CHARS", 2000)
# Recall is a GET, so every param rides the request line. A prompt is already
# unbounded there; `ctx` must never be the straw that turns a working recall into a
# 431 and costs the user the whole injection. Measured on the ENCODED query, since
# accented / CJK text triples under percent-encoding.
CTX_QUERY_BUDGET_CHARS = 12_000


# State-of-work detection: prompts whose CORRECT answer depends on what has
# already been done in this profile — looking back ("did we / have we"), checking
# status ("where are we"), or planning forward from the work so far ("what's left
# / next steps / is it ready"). Tense is NOT the signal: "what's left to do" is
# grammatically future yet fully depends on the past. For these the agent must
# NOT answer from an empty session head, so we widen recall, skip session-dedup
# (re-surface prior-work records even if shown earlier), and — on a thin result —
# inject an explicit "search before you conclude it didn't happen" instruction.
# Deliberately GENEROUS (EN + FR): a false positive only costs a slightly wider
# recall; a false negative is caught by the SessionStart lineage stance.
_PRIOR_WORK_PATTERNS = (
    # looking back — existence / what-was-done
    r"\b(did|have|has|had)\s+(you|we|i|they)\b",
    r"\b(did|have)n'?t\s+(you|we|i)\b",
    r"\bwhat\s+(did|have|had)\s+(you|we|i)\b",
    r"\bdo\s+you\s+(have|recall|remember|know\s+of)\b",
    r"\bis\s+there\s+(a|any)\s+(record|note|history|log|trace)\b",
    r"\bwe(?:'ve|\s+have)?\s+(?:already\s+)?(done|tried|tested|shipped|built|fixed|deployed|merged|decided|written)\b",
    r"\balready\s+(done|tried|tested|shipped|built|fixed|deployed|merged|implemented|handled|covered)\b",
    r"\b(ever|previously|in\s+the\s+past|last\s+time)\b",
    # status — where things stand
    r"\bwhere\s+(are|do)\s+we\b",
    r"\bwhere\s+(does|is|are)\b.{0,40}\b(stand|at|up\s+to)\b",
    r"\bwhat'?s\s+the\s+(status|state)\b",
    r"\bhow\s+far\s+(along|are|have|is)\b",
    # planning — forward from the work so far
    r"\bwhat'?s\s+(left|missing|next|remaining)\b",
    r"\bwhat\s+(is|are)\s+(left|remaining|missing|the\s+next\s+steps?)\b",
    r"\bnext\s+steps?\b",
    r"\bwhat\s+(still\s+)?(needs|remains)\s+to\b",
    r"\bwhat\s+would\s+it\s+take\b",
    r"\bremaining\s+(work|tasks?|steps?|items?)\b",
    r"\bare\s+we\s+(ready|done|finished)\b",
    r"\bis\s+(it|this|that)\s+(ready|done|finished|complete)\b",
    r"\bto-?do\b",
    r"\b(pick\s+up\s+where|where\s+we\s+left\s+off)\b",
    r"\bcan\s+we\s+(ship|merge|release|deploy)\b",
    # French — looking back
    r"\b(est-ce\s+qu|qu'?est-ce\s+qu)(?:'|e\s)?(on|tu|j'?|nous|vous|il)\b",
    r"\b(a-t-on|avons-nous|as-tu|avait-on)\b",
    r"\bdéjà\s+(fait|testé|essayé|livré|corrigé|déployé|implémenté|vu|géré|traité)\b",
    r"\b(on|tu|nous)\s+a(?:vait|vons|i)?\s+(?:déjà\s+)?(fait|testé|essayé|livré|corrigé|déployé)\b",
    # French — status
    r"\boù\s+(on\s+en\s+est|en\s+est-on|en\s+sommes-nous|j'?en\s+suis)\b",
    r"\b(l'?état|le\s+statut)\s+(de|d'?|du|des)\b",
    # French — planning
    r"\b(qu'?est-ce\s+qu|ce\s+qu)(?:'|e\s)?il\s+(reste|manque)\b",
    r"\bque\s+reste-t-il\b",
    r"\bqu'?est-ce\s+qui\s+(reste|manque)\b",
    r"\bil\s+reste\s+(quoi|à\s+faire|encore)\b",
    r"\bprochaines?\s+étapes?\b",
    r"\bon\s+continue\b",
    r"\bfaut-il\s+encore\b",
)
_PRIOR_WORK_RE = re.compile("|".join(_PRIOR_WORK_PATTERNS), re.IGNORECASE)


def depends_on_prior_work(prompt):
    # Normalize a curly apostrophe to straight so French "qu'est-ce" etc. match.
    return bool(_PRIOR_WORK_RE.search(prompt.replace("’", "'")))


def add_conversation_context(params, transcript_path, host):
    """Attach `ctx` — the previous assistant message — to a recall request, or not.

    Not attached when there is nothing to attach (first prompt of a session, no
    transcript, an unreadable or malformed one, an assistant that has only run
    tools), nor when the encoded query would grow past CTX_QUERY_BUDGET_CHARS.
    Mutates `params` in place and returns nothing: the caller's request is
    byte-identical to the pre-#344 one whenever this declines.
    """
    try:
        said = transcript.previous_assistant_text(host, transcript_path, CTX_MAX_CHARS)
        if not said:
            return
        if len(urllib.parse.urlencode(dict(params, ctx=said))) > CTX_QUERY_BUDGET_CHARS:
            return
        params["ctx"] = said
    except Exception:
        # Belt-and-braces over core/transcript's own guard: an optimization may not
        # be able to take the injection down with it, whatever a host adapter does.
        params.pop("ctx", None)


# #569 — LAY OUT THE POINTER'S PULL-STATE GROUPS. The server decides what each
# group MEANS and says so in `instruction`, quoting the group's key; this only
# puts that group's records under its key, one per line, so the agent can act on
# a specific id. Nothing here is composed prose: the key is the server's own
# field name, the label and the turn are payload values, and a group the server
# did not send produces nothing at all — which is what keeps an older server (no
# groups, instruction only) rendering exactly as it did before.
#
# The unread group is the enumerated one, so its entries carry a label; the
# others are ids the agent already holds something for and are packed onto one
# line each.
#
# #569 slice 3 — the three STALE groups (`retired`, `disputed`, `archived`) are
# records this session was handed that have since left circulation. They match
# nothing, so they arrive with no relevance behind them; the server's own
# instruction says what each one means and what to do about it, and this only
# lays the ids out under the key it quoted. A server that sends none of them
# renders exactly as it did before, which is what keeps an older server working.
_POINTER_GROUP_KEYS = (
    "unread",
    "head",
    "held",
    "changed",
    "retired",
    "disputed",
    "archived",
)


def _record_relations(record: dict) -> str:
    """Payload relations carried BY an entry, never wording composed here."""
    parts: list[str] = []
    replaces = record.get("replaces")
    if isinstance(replaces, dict) and replaces.get("id"):
        turn = replaces.get("turn")
        seen = f", read at turn {turn}" if isinstance(turn, int) else ""
        parts.append(f"replaces {replaces['id']}{seen}")
    superseded_by = record.get("supersededBy")
    if isinstance(superseded_by, str) and superseded_by:
        parts.append(f"supersededBy {superseded_by}")
    return f" [{'; '.join(parts)}]" if parts else ""


def _pointer_group_lines(pointer: dict) -> str:
    lines: list[str] = []
    for key in _POINTER_GROUP_KEYS:
        group = pointer.get(key)
        if not isinstance(group, dict):
            continue
        records = group.get("records")
        if not isinstance(records, list) or not records:
            continue
        entries: list[str] = []
        for record in records:
            if not isinstance(record, dict) or not record.get("id"):
                continue
            ref = f"{record.get('type') or 'record'} {record['id']}"
            label = (record.get("label") or "").strip()
            turn = record.get("turn")
            relations = _record_relations(record)
            if label:
                entries.append(f"- {ref} — {label}{relations}")
            elif isinstance(turn, int):
                entries.append(f"{ref} (turn {turn}){relations}")
            else:
                entries.append(f"{ref}{relations}")
        if not entries:
            continue
        if entries[0].startswith("- "):
            lines.append(f"{key}:\n" + "\n".join(entries))
        else:
            lines.append(f"{key}: " + ", ".join(entries))
    return ("\n" + "\n".join(lines)) if lines else ""


# #512 — the OMITTED LANES notice. The prompt-lane allocator reports, per turn,
# which lanes it could not fit at any fidelity (`omittedLanes`), and an omission
# the agent cannot see is indistinguishable from a profile that holds nothing —
# the same failure mode the pointer lanes exist to close. So a reported omission
# is always NAMED, and where the lane has a tool that fetches it the exact call
# rides with the name so the agent can recover it in one step.
#
# Matched by SUBSTRING on the lowercased lane id, first entry winning, because the
# ids are the server's own lane names (`rulesPointer`, `voicePointer`, `voicePull`,
# `persona`, `persona.others`, `activeTopic`, `relatedTopics`, `hitsPointer`, …) and
# a rename there must cost the pull HINT at worst — never the name, and never the
# notice. A lane nothing here matches is still listed, with no call invented for it.
#
# The two ASK lanes are excluded FIRST and deliberately: `ruleAsk` and `conflictAsk`
# carry a QUESTION for the user — a rule proposal awaiting their yes, a set of
# contradicting facts awaiting their verdict — not stored content the agent can go
# and fetch. `ruleAsk` would otherwise match the rules entry below and tell the
# agent to read_rules for a rule that is not binding and is not there.
_ASK_LANE_SUFFIX = "ask"
_LANE_PULLS = (
    ("rule", "read_rules"),
    ("voice", "get_resolved_voice"),
    ("persona", "get_persona"),
    ("topic", "get_topic (or list_topics)"),
    ("hit", "recall"),
)


def _lane_pull(lane):
    low = lane.lower()
    if low.endswith(_ASK_LANE_SUFFIX):
        return ""
    for needle, call in _LANE_PULLS:
        if needle in low:
            return call
    return ""


# `recallFloorOverBudget` is the CAUSE line, not another absence: it says the pushed
# recall records were trimmed to their guaranteed minimum, that minimum was KEPT,
# and it alone is larger than the whole per-prompt cap — so every other lane had
# nothing left to be allocated and is named in `omittedLanes`. It never means recall
# content is missing beyond the ordinary trim, and it can only ride beside pushed
# `hits` (a pointer has no exempt floor). Rendered even with no lanes named, because
# a blown cap the agent cannot see is a payload it will read as complete.
def omitted_lanes_block(omitted, floor_over_budget=False):
    """The visible notice for lanes the budget dropped. "" when there is nothing to say."""
    lines = []
    for lane in omitted if isinstance(omitted, list) else ():
        if not isinstance(lane, str) or not lane.strip():
            continue
        name = lane.strip()
        call = _lane_pull(name)
        lines.append(f"- {name} — pull it with {call}" if call else f"- {name}")
    cause = (
        "The recall records in this payload were kept at their guaranteed minimum, and that "
        "minimum alone is larger than this turn's whole context budget — so there was nothing "
        "left to allocate to anything else.\n"
        if floor_over_budget
        else ""
    )
    if not lines and not cause:
        return ""
    body = cause + "\n".join(lines) if lines else cause.rstrip("\n")
    return (
        "<omitted-context>\n"
        "[These standing-context lanes did NOT fit this turn's context budget and were OMITTED. "
        "Their absence here is NOT evidence that nothing is stored for them. If one bears on what "
        "you are about to do, PULL it with the call named beside it before acting; a lane with no "
        "call beside it cannot be fetched — treat it as unknown rather than as empty.]\n"
        + body
        + "\n</omitted-context>"
    )


def handle(event: Event, host: Host):
    """Return an Output (inject rules/recall — as content or as pointers) or None."""
    prompt = str(event.prompt or "").strip()
    if not prompt:
        return None

    # #549 — the user is back at the keyboard, so a forced consolidation turn is
    # over whether or not it ever reached a stop. An INTERRUPTED one never does,
    # and its latch would otherwise go on containing the user's own next call. The
    # text comparison is what makes this safe on a host that delivers the
    # continuation as a prompt: only a DIFFERENT prompt disarms.
    sweepturn.disarm_unless_continuation(event.session_id, prompt)

    # State-of-work prompts get a WIDER recall budget — the relevant facts/actions
    # are often several, and we want them all above the fold (see depends_on_prior_work).
    is_state = depends_on_prior_work(prompt)
    params = {"q": prompt, "k": str(STATE_RECALL_K if is_state else K)}
    # Always request the authoritative RULES tier so it re-surfaces every turn (not
    # only at SessionStart) — the recall endpoint's ?rules=1. What comes back is the
    # POINTER on a current server and the push buckets on an older one; the tier is
    # in force either way, and neither shape is guaranteed to carry any rule TEXT.
    params["rules"] = "1"
    # Session-scoped dedup OPT-IN. This is the auto-inject path: forward the
    # session id the host passes on stdin + dedup=1 so the recall endpoint
    # excludes items already injected this session (a new session starts fresh;
    # the seen-set auto-cleans via agent_kv TTL). The on-demand recall / search
    # tools NEVER send these, so they hit the pure, un-deduped path.
    # A pointer whose `total` is 0 (no NEW items this turn) injects nothing.
    session_id = str(event.session_id or "").strip()
    # Forward the session id ALWAYS: the active-topic banner is resolved STRICTLY
    # from THIS session's presence (getPresenceTopic — #148/#156; no session id →
    # no banner; the shared (profile, cwd) anchor is never consulted). `dedup=1`
    # is a SEPARATE opt-in and stays state-aware: a normal prompt dedups (inject each
    # item at most once); a STATE-OF-WORK prompt SKIPS dedup so prior-work records
    # RE-SURFACE even if shown earlier — the whole point is to re-confront the agent
    # with them when asked what was done / what's left. The server gates dedup on
    # `dedup && session`, so sending session WITHOUT dedup leaves the un-deduped path
    # untouched while still scoping the topic read to this session.
    if session_id:
        params["session"] = session_id
    if session_id and not is_state:
        params["dedup"] = "1"
    # Forward the resolved profile so recall is scoped to the same profile the
    # SessionStart bootstrap used. An explicit NEURONZAI_PROFILE wins via X-Profile;
    # otherwise pass the session's cwd — canonicalized to the git main-worktree
    # root (see profile.canonical_cwd) so every worktree of a repo resolves to the
    # repo's own profile instead of auto-creating a throwaway one.
    # Precedence: /switch-profile session override > NEURONZAI_PROFILE env > cwd.
    # Send cwd ALWAYS (not only when no profile): X-Profile wins for profile
    # scoping, and cwd rides along for the server paths keyed on the working dir.
    # The topic banner itself is per-session now (#156) — cwd no longer drives it.
    # Under a /switch-profile override, `cwd` is "" and recall is scoped to the
    # switched profile.
    override = profile_mod.read_override(event.session_id)
    profile, cwd = profile_mod.resolve(
        event.session_id, event.cwd, api.ENV_PROFILE, override=override
    )
    if cwd:
        params["cwd"] = cwd

    # #376 — THE SAME TWO OBSERVABLES THE PRE-TOOL GATE SENDS, for the same reason
    # and under the same names. `cwd` and `branch` are visible to BOTH lanes, so
    # neither is ever auto-dropped here the way a tool-only target is; a leaf whose
    # value simply did not arrive resolves to UNKNOWN and becomes a residual the
    # agent is asked to settle. That is the right behaviour for a target the agent
    # genuinely knows — and the wrong bill to pay every single turn. Withhold them
    # and EVERY rule carrying a branch leaf turns into a per-prompt question about a
    # branch nothing in the turn was about, which is precisely the clutter #376
    # exists to remove. Sending them DECIDES those leaves instead of asking.
    #
    # `workdir`, not `cwd`, for the observable — identical split to the gate. `cwd`
    # above is the profile-ROUTING leg and goes silent under a /switch-profile
    # override (resolve() returns cwd="" so no directory is ever mapped to the
    # switched profile), which would leave the cwd target unobservable in exactly
    # the sessions a user re-scoped by hand.
    #
    # Free, again: canonical_cwd shells out to git, and resolve() already paid for
    # that call in every branch that returns a cwd. git_branch spawns nothing at all
    # — it reads .git/HEAD. Both fail to "" and are then omitted; neither is ever
    # guessed, because an absent param is a well-defined state and a wrong one is
    # not.
    workdir = cwd or profile_mod.canonical_cwd(event.cwd)
    if workdir:
        params["workdir"] = workdir
    # Off event.cwd, never `workdir`: canonical_cwd resolves a linked worktree to the
    # MAIN worktree's root, which is checked out on a DIFFERENT branch.
    branch = profile_mod.git_branch(event.cwd)
    if branch:
        params["branch"] = branch

    # The SESSION model this prompt is running under — same omit-when-absent contract
    # as branch/workdir. The adapter already lower-cased the host's own id; no
    # provider prefix is added or stripped. Absent → param omitted, which the server
    # reads as unobservable so a model-scoped rule/register leaf does not ride.
    if event.model:
        params["model"] = event.model

    # CONVERSATIONAL CONTEXT (#344) — the previous ASSISTANT message. An anaphoric
    # prompt ("go ahead and write it") has no retrievable content of its own; the
    # thing it points at is what the assistant just said. Read from the TAIL of the
    # transcript (never the whole file — this is the per-prompt critical path) and
    # capped, keeping the END of the message, which is what a reply attaches to.
    # Every failure path yields "" and the request goes out exactly as it did before
    # this existed — a missing hint costs relevance, a raised exception would cost
    # the user their entire injected context.
    add_conversation_context(params, event.transcript_path, host)

    # Unified RECALL (memories + actions + knowledge) — the deduped endpoint.
    # All HTTP + auth-failure logging goes through core/api (fail-open → None).
    data = api.get_json("/api/memory/recall", params=params, profile=profile,
                        where="memory_inject")
    if data is None:
        return None

    # #512/#561 — THE RULES POINTER, the only shape this lane has. The server
    # sends `rulesPointer` — a measured `total` plus the `instruction` it
    # composed, which already carries the exact read_rules(…) call to make (the
    # profile, the named rules, and the delivery token that scopes the read to
    # THIS session's generation) — names only, no rule text. Everything
    # visible is server-composed, so the whole rendering is the instruction
    # inside a fence: nothing here writes prose about rules, because a second
    # author is what makes a pointer and its pull drift apart.
    #
    # `total: 0` is silence: the server has already said nothing is in force.
    #
    # NO novelty/dedup gate on this lane: the authoritative tier must stay in
    # force at the moment of action.
    rule_blocks = []
    rules_pointer = data.get("rulesPointer")
    if isinstance(rules_pointer, dict):
        rules_instruction = rules_pointer.get("instruction")
        if (
            rules_pointer.get("total") != 0
            and isinstance(rules_instruction, str)
            and rules_instruction.strip()
        ):
            rule_blocks = ["<rules-pointer>\n" + rules_instruction + "\n</rules-pointer>"]

    rule_block = "\n\n".join(rule_blocks)

    # Lanes the prompt-lane allocator could not fit at any fidelity this turn.
    # Reported, never swallowed (see omitted_lanes_block).
    omitted_block = omitted_lanes_block(
        data.get("omittedLanes"), data.get("recallFloorOverBudget") is True
    )

    # Persona v2 / "social map" (#60/#62/#437) — STANDING context: the server
    # returns { self: {subjectId, traits} | None, others: [{subjectId,
    # displayName, kind, traitCount, traits, instruction?}, ...] }. There is no
    # `summary` any more (#437): traits are the source of truth. SELF rides in
    # full at SessionStart and is re-asserted here only on a cadence / after a
    # compaction, so it is absent on most turns; OTHERS are colleague subjects the
    # server selected because THIS prompt is about them (often empty). A
    # colleague entry carries a server-composed `instruction` (a count and "pull
    # them with get_persona"); its traits are never rendered (#561), so an entry
    # from a server old enough to send traits and no instruction contributes
    # nothing. `persona` itself may be absent/None — handled below with
    # no crash and no block. Two fixed-size blocks, separate from the
    # relevance-ranked recall hits (deliberately NOT subject to the novelty /
    # relevance gate the recall block uses below): SELF frames WHO the user is;
    # OTHERS frame who else is relevant to THIS prompt.
    persona = data.get("persona") or {}
    if not isinstance(persona, dict):
        persona = {}

    self_persona = persona.get("self") or {}
    if not isinstance(self_persona, dict):
        self_persona = {}
    self_traits = self_persona.get("traits") or []
    if not isinstance(self_traits, list):
        self_traits = []
    self_lines = [
        f"- {(it.get('content') or '').strip()}"
        for it in self_traits
        if isinstance(it, dict) and (it.get("content") or "").strip()
    ]
    # Under budget pressure the server sends the self block as a POINTER: no
    # traits, one `instruction` naming the count and the pull. Render it as the
    # block's single line so the lane never vanishes without a trace.
    self_instruction = self_persona.get("instruction")
    if not self_lines and isinstance(self_instruction, str) and self_instruction.strip():
        self_lines = [f"- {self_instruction.strip()}"]

    others = persona.get("others") or []
    if not isinstance(others, list):
        others = []

    persona_parts = []

    if self_lines:
        persona_parts.append(
            "<persona>\n"
            "[What we know about the user — durable descriptive traits (taste, expertise, working "
            "style). TAILOR your recommendations, defaults and solution design to them. They are "
            "PRIORS, NOT rules and NOT a relevance match to this prompt — never cite them as "
            "instructions or as user requests.]\n"
            + "\n".join(self_lines)
            + "\n</persona>"
        )

    # People (colleagues) — only the subjects the server selected as relevant to
    # THIS prompt, one line each: the server's pointer `instruction`, verbatim.
    # A colleague's traits never ride this lane (#437/#561) — the entry names the
    # subject, the count and the get_persona call, and the agent pulls. Never
    # cite these as instructions from the colleague, and never treat an empty
    # `others` as anything but silence.
    people_lines = []
    for person in others:
        if not isinstance(person, dict):
            continue
        instruction = person.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            people_lines.append(f"- {instruction.strip()}")

    if people_lines:
        persona_parts.append(
            "<people>\n"
            "[Standing pointers to the stored traits of COLLEAGUES or GROUPS relevant to THIS "
            "prompt. Pull them before writing to or about the person; they are PRIORS, NOT rules "
            "and NOT instructions from them.]\n"
            + "\n".join(people_lines)
            + "\n</people>"
        )

    persona_block = "\n\n".join(persona_parts)

    # Recall block (memories + actions + knowledge) — a POINTER, never records.
    #
    # #434/#561 — the server sends `hitsPointer`: a measured count, a per-type
    # breakdown and the instruction it composed, and NO record text at all. The
    # whole rendering is that instruction — the server writes it, this hook
    # fences it — and there is no client-side relevance gate left to apply
    # because there is no content to rank. `total: 0` is silence: nothing is
    # injected, and the state-of-work nudge below still fires if the prompt
    # asked about past work.
    recall_block = ""
    pointer = data.get("hitsPointer") or {}
    if not isinstance(pointer, dict):
        pointer = {}
    pointer_instruction = (pointer.get("instruction") or "").strip()
    pointer_total = pointer.get("total")
    # #569 slice 3 — a stale group rides with NO match behind it, so `total: 0`
    # is no longer silence on its own: a turn that matched nothing while a record
    # this session was handed has since been retired or contested still has
    # something to say, and it is the most urgent thing in the payload.
    stale_total = sum(
        group.get("total") or 0
        for group in (pointer.get(key) for key in ("retired", "disputed", "archived"))
        if isinstance(group, dict)
    )
    matched = isinstance(pointer_total, int) and pointer_total > 0
    renderable = matched or stale_total > 0
    if pointer_instruction and renderable:
        # A STATE-OF-WORK prompt keeps its framing. The generic instruction says
        # "go read them"; it does NOT say the records are the authoritative
        # answer, and it does NOT say to distinguish "no record found" from "it
        # didn't happen" — which is the whole reason this lane exists, because a
        # fresh session otherwise answers "no, we never did that" out of an empty
        # head.
        #
        # #569 — the pointer now GROUPS its records by whether this session has
        # actually been handed their text, and the groups carry ids. The
        # instruction (server-composed, rendered verbatim as always) explains what
        # each group means and quotes its key; everything added below is payload
        # data laid out under that key, never wording composed here. A server that
        # sends no groups renders exactly as it did before.
        recall_block = (
            "<memory-context>\n"
            + (STATE_POINTER_HEADER + "\n" if (is_state and matched) else "")
            + pointer_instruction
            + _pointer_group_lines(pointer)
            + "\n</memory-context>"
        )

    # Topic "mode" (#36) — re-surfaced EVERY prompt so the agent doesn't forget it's
    # working under a topic as context grows, or after a compaction drops the
    # SessionStart brief. One line (the full brief rode SessionStart); not deduped.
    # This is ALSO the badge's self-healing lane: the server answers from THIS
    # session's presence row, so a resumed or compacted session gets its badge back
    # on the first prompt, and a topic mode that ended elsewhere clears it. The
    # enter/exit sync (core/topic_mode) only makes that immediate.
    topic = data.get("activeTopic") or {}
    topic_block = ""
    # "" is the CLEAR: no presence row means this session is in no topic mode, which
    # is exactly when a badge left over from an ended one must come off the bar.
    topic_badge = ""
    if isinstance(topic, dict) and topic.get("id"):
        t_title = (topic.get("title") or topic.get("id") or "").strip()
        topic_badge = topic_mode.badge(t_title)
        topic_block = (
            "<active-topic>\n"
            f"[TOPIC MODE — you are working under the topic '{t_title}' (id {topic.get('id')}). "
            f"LINK new durable records to it as you create them — pass topicId={topic.get('id')} on "
            "fact_add / log_action / add_knowledge (the server writes the member edge in the same "
            "call) so the topic gathers this session's work; an unlinked record is orphaned. Route "
            "off-topic work elsewhere. Refresh its summary (update_topic summary=…) and call exit_topic "
            "when the session's work is done.]\n"
            "</active-topic>"
        )

    # State-of-work prompt that recalled NOTHING — the dangerous case. Unlike an
    # off-topic prompt (where an empty recall is EXPECTED), here empty is NOT
    # evidence the work didn't happen, so inject an explicit search instruction
    # rather than staying silent and letting the agent answer "no" from an empty
    # session head.
    # #434 — a MATCHED pointer already says "you hold a fragment, go search", and
    # stacking a second search instruction next to it teaches the agent to skim
    # both. The nudge is for the case that has not changed — a state-of-work
    # prompt that matched NOTHING.
    # #569 — so the test is `matched`, not "did anything render". A stale-only
    # block rides on records that matched nothing and says nothing about where
    # the answer is; letting it suppress the nudge would have made a withdrawn
    # record silently cancel the recovery guidance, and pairing it with the
    # state header would have told the agent to go read records the very next
    # sentence says do not exist.
    state_instruction_block = ""
    if is_state and not matched:
        state_instruction_block = (
            "<memory-context>\n"
            "[The user is asking about prior or ongoing work, and automatic recall surfaced nothing for "
            "it. Do NOT answer from this session's context alone and do NOT assume the work wasn't done. "
            "First SEARCH this profile — recall (higher k), fact_search, list_actions, and "
            "get_topic/list_topics for the relevant thread — then answer, distinguishing 'I searched and "
            "found no record' from 'it didn't happen'.]\n"
            "</memory-context>"
        )

    # When this session was re-scoped with /switch-profile, re-assert the active
    # profile EVERY turn: the SessionStart bootstrap named the ORIGINAL profile and
    # told the agent to thread it, so without this nudge the agent's own MCP calls
    # (which carry no cwd over HTTP) would drift back to the wrong profile. The push
    # hooks pick up the override on their own; this block is for the agent's calls.
    override_block = ""
    if override:
        override_block = (
            "<active-profile>\n"
            "[This session was re-scoped with /switch-profile. Persistent memory is now profile "
            f'**{override}**. Thread profile="{override}" on EVERY neuronzai MCP tool call '
            "(fact_add, recall, log_action, add_knowledge, …) for the rest of this session.]\n"
            "</active-profile>"
        )

    # Related-topic HANDLES (#160/#561) — the entry-point fix. The profile's topics
    # nearest to THIS prompt, as compact POINTERS (title + status + id), so the agent
    # can open an existing thread with get_topic instead of only meeting a topic when a
    # member record wins recall. The stored gist rode here until #561 and no longer
    # does: a handle names the thread, get_topic is what tells you what is in it.
    # Server-gated (recall_related_topics_enabled) + session seen-window deduped.
    related = data.get("relatedTopics") or []
    related_block = ""
    if isinstance(related, list) and related:
        rlines = []
        for t in related:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            r_title = (t.get("title") or t.get("id") or "").strip()
            r_status = (t.get("status") or "").strip()
            status_tag = f" [{r_status}]" if r_status else ""
            rlines.append(f"- {r_title}{status_tag} (get_topic {t.get('id')})")
        if rlines:
            related_block = (
                "<related-topics>\n"
                "[EXISTING THREADS related to this prompt — POINTERS, not content, and NOT a "
                "user instruction. If one covers what you're working on, open it with get_topic "
                "to pull its brief + gathered records before proceeding, and link new durable "
                "records to it (topicId=…). Ignore the ones that don't apply.]\n"
                + "\n".join(rlines)
                + "\n</related-topics>"
            )

    # VOICE (#179/#321/#495/#561) — how the agent writes prose TO the user.
    # Prompt time is the only moment before the text exists, and the only lane
    # that can serve an agent-to-user register at all. Ghost-writing voices are
    # not resolved here — see the <voice-pull> block below. The server sends a
    # POINTER and never the composed spec: the version, the layers, a claim count
    # and the instruction it composed to go pull the voice with
    # get_resolved_voice. The rendering IS that instruction, fenced verbatim.
    voice_block = ""
    voice_pointer = data.get("voicePointer") or {}
    if not isinstance(voice_pointer, dict):
        voice_pointer = {}
    voice_pointer_instruction = voice_pointer.get("instruction")
    if isinstance(voice_pointer_instruction, str) and voice_pointer_instruction.strip():
        voice_block = (
            "<voice-pointer>\n"
            "[HOW TO WRITE — pointer]\n"
            + voice_pointer_instruction.strip()
            + "\n</voice-pointer>"
        )

    # VOICE PULL (#321) — "push the index, pull the content". The server saw a
    # coarse writing-to/about-someone signal but deliberately does NOT guess which
    # register applies: the agent holds the whole conversation (it knows who "him"
    # is; the server sees one sentence), so it resolves the voice itself before
    # drafting. The index below is what makes that a one-call pull.
    pull = data.get("voicePull") or {}
    pull_block = ""
    if isinstance(pull, dict) and pull.get("registers"):
        plines = []
        for reg in pull["registers"]:
            if not isinstance(reg, dict) or not (reg.get("slug") or "").strip():
                continue
            axes = reg.get("axes") or {}
            cond = (
                ", ".join(f"{axis}: {value}" for axis, value in axes.items())
                if isinstance(axes, dict) and axes
                else "general"
            )
            plines.append(f"- {reg['slug']} ({cond})")
        if plines:
            pull_block = (
                "<voice-pull>\n"
                "[This prompt looks like it involves writing TO or ABOUT someone. BEFORE drafting "
                "any message on the user's behalf, resolve the applicable voice: call "
                "get_resolved_voice with the audience/act/situation as YOU judge them from the "
                "conversation — you know who a pronoun refers to and what the situation is; the "
                "server does not. The user's registers:]\n"
                + "\n".join(plines)
                + "\n</voice-pull>"
            )

    # RULE ASK (#178) — a pending proposal that is relevant to THIS prompt. Asked
    # in the flow, once per session, at the moment the rule would have applied.
    ask = data.get("ruleAsk") or {}
    ask_block = ""
    if isinstance(ask, dict) and (ask.get("question") or "").strip():
        rule = ask.get("rule") or {}
        rule_id = rule.get("id") if isinstance(rule, dict) else None
        ask_block = (
            "<rule-ask>\n"
            "[A pending rule proposal is relevant to this prompt. Put this question to the user "
            "IN YOUR FLOW — one line, not an interruption, and only if it fits what you are already "
            "saying. When they answer, call resolve_rule_ask(id="
            + f'"{rule_id}"'
            + ", outcome=accepted|declined|deflected). No answer, or a change of subject, is "
            "`deflected` — never guess `declined`.]\n"
            + (ask.get("question") or "").strip()
            + "\n</rule-ask>"
        )

    # CONFLICT ASK (#308) — a GROUP of stored facts that contradict each other,
    # one of which is relevant to THIS prompt. Every member is currently invisible
    # to recall, so this is not a nicety: it is the only path back for facts the
    # store has already stopped answering with.
    conflict = data.get("conflictAsk") or {}
    conflict_block = ""
    if isinstance(conflict, dict) and (conflict.get("question") or "").strip():
        group_id = conflict.get("groupId")
        members = conflict.get("members") or []
        member_lines = "\n".join(
            f"- {m.get('id')}: {(m.get('content') or '').strip()}"
            for m in members
            if isinstance(m, dict)
        )
        conflict_block = (
            "<conflict-ask>\n"
            "[These stored facts contradict each other, and one of them is relevant to this "
            "prompt. ALL of them are excluded from recall until this is settled. Put the question "
            "to the user IN YOUR FLOW — one line, not an interruption. When they answer, call "
            "resolve_conflict(group=" + f'"{group_id}"' + ", outcome=...): `promoted` + winner=<id> "
            "if one is true; `retired_all` if none is; `corrected` + correction=\"<the truth>\" if "
            "none is and they tell you what is. No answer, or a change of subject, is `deflected` "
            "— which is NOT a resolution: the question comes back later, so never guess an "
            "outcome to close it.]\n"
            + member_lines
            + "\n"
            + (conflict.get("question") or "").strip()
            + "\n</conflict-ask>"
        )

    # Nothing to inject (no override, rules, persona, topic, recall, voice, omission
    # notice, or state nudge) → silent no-op. The omission notice sits directly under
    # the rules because it is read for the same reason: what is NOT here this turn.
    parts = [
        section
        for section in (
            override_block,
            rule_block,
            omitted_block,
            persona_block,
            voice_block,
            pull_block,
            ask_block,
            conflict_block,
            topic_block,
            recall_block,
            related_block,
            state_instruction_block,
        )
        if section
    ]
    # The badge rides even when there is nothing to INJECT: a prompt that recalled
    # nothing still carries the truth about this session's topic mode. With no badge
    # either, the handler stays a complete no-op — a bar can only hold a stale badge
    # if this session entered a topic, and that session's prompts carry the pointer
    # blocks that bring the clear with them.
    if not parts:
        return Output(status_badge=topic_badge) if topic_badge else None

    return Output(context="\n\n".join(parts), status_badge=topic_badge)
