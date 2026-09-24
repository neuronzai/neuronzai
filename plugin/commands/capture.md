---
description: Distill THIS session into the right Neuronz.ai records (memory / knowledge / action), routed automatically. Topic-aware. Run it when the user asks to checkpoint the session, AND whenever a Neuronz.ai lifecycle instruction (the end-of-session self-sweep) tells you to consolidate — in that case run SILENTLY, which means the turn ends with ZERO output. Optional argument scopes what to capture (default — the whole conversation).
model-invocable: true
---

# /neuronzai:capture

Consolidate what this session produced into persistent Neuronz.ai records for the current profile, routing every piece to the RIGHT tier automatically. This is the ONE consolidation contract: the user runs it to checkpoint a session ("update neuronzai with what we did and learned"), and the end-of-session self-sweep points at it instead of restating the rules.

Scope to capture from (verbatim from the caller, may be empty): $ARGUMENTS

## 0. Pick the mode FIRST — loud or silent

Who asked decides how much you say. Settle this before anything else.

- **The USER asked** (they typed the command, or asked you in their own turn to capture / checkpoint / update memory) → **LOUD**: do §1's scope line and §5's report.
- **NOBODY asked — you reached for this yourself**, because a Neuronz.ai lifecycle instruction told you to consolidate before stopping → **SILENT**, and that is the DEFAULT for every non-user trigger. There is no one waiting on a report, and narrating a background sweep mid-conversation reads as the agent answering a machine.

### Flags

The self-sweep invokes this with flags, because the line it is invoked from is
RENDERED to the user and every character of it is screen they did not ask for. A
human typing the command usually passes none.

| Short | Long | Means |
|---|---|---|
| `-q` | `--silent` | SILENT mode, whoever asked — the turn ends with ZERO output (§6) |
| | `--loud` | LOUD mode, whoever asked |
| `-l <code>` | `--lang=<code>` | write records in this ISO 639-1 language (§3.1) |
| `-s <token>` | `--sweep=<token>` | the sweep ticket — echo it on every write |

**Parse and STRIP every flag first. What remains — and only what remains — is the
scope.** A bare `-q -s 4f3a9c21` is NOT a scope; read it as flags and capture the
whole conversation. Strip a flag you do not recognise too, rather than mistaking
it for a slice of the session; guessing a scope from machinery is how a whole
session gets consolidated as if it were one narrow topic.

Two flags carry meaning past this section:

- **`-s <token>`** — pass that exact value as `sweepId` on EVERY `fact_add` /
  `fact_update` this turn. That is how the sweep is recorded as done; without it
  the same events are handed back to you next time. Never invent one, and never
  carry one into a later turn.
- **`-l <code>`** — write the records in that language (§3.1). No `-l` means
  English. This matters even when it looks redundant: the server stamps the
  language on the row from the profile's setting without reading what you wrote,
  so records authored in the wrong language are stored mislabelled, and nothing
  downstream notices.

In SILENT mode:

- Emit NO prose — not a preamble, not a summary, not "done". Tool calls only, then stop. Skip §1's scope line and ALL of §5. **§6 is where a silent turn ENDS; read it before you finish, because that is the step this mode actually loses.**
- **SILENT means no NARRATION, not no QUESTIONS.** If you hit something only the user can settle, ask it — via the memory block in §4b, and nothing else.
- A rule the user stated still goes to `propose_rule`, never `create_rule` (§3.4): a rule needs a deliberate yes in the dashboard queue, not a yes extracted from someone mid-task.
- The `-s` ticket and `-l` language above still apply — they are how a silent sweep is recorded and what language it writes in. After a compaction the SessionStart operating card may be gone from your context, so the flags are the only copy you have.

## Detached silent mode

When the invocation prompt says **DETACHED SILENT MODE**, the user is no longer
present and the cleaned transcript bundled in that prompt is the whole scope.
Apply these overrides to the workflow below:

- Skip the scope announcement and emit no final report or other prose. Use tool
  calls only, then stop. §6 governs how the turn ends and binds here too — with
  the user gone there is not even a reader for a closing line.
- Do not inspect the repository or fetch missing session content. The supplied
  cleaned transcript is the complete evidence boundary.
- Pass the supplied `profile` on every Neuronz.ai tool call and write in the
  supplied record language.
- When the prompt supplies a `sweepId`, pass that exact value on every `fact_add`
  and `fact_update`; never invent or substitute another one.
- The user cannot approve a rule here. Use `propose_rule`, never `create_rule`,
  because no human is present to confirm it.

## 1. Decide the scope

- If `$ARGUMENTS` is empty → capture from the WHOLE conversation.
- If `$ARGUMENTS` names a slice ("the auth refactor", "since the last deploy", "the websocket debugging") → restrict to that slice.
- LOUD mode only: say in ONE line which scope you're capturing before you write, so the user can correct you.

## 2. Link to topics (active AND/OR detected)

Call `current_topic` first — but link to topics in BOTH cases:

- **If a topic IS active** → pass `topicId=<the active topic's id>` on every `fact_add` / `log_action` / `add_knowledge` so the server files the record as a `member` in the same write (use `link` for anything already written).
- **Whether or not a topic is active** → also check each record against the profile's EXISTING topics (`search` over topics, or `list_topics`) and file it under ANY it's genuinely about. Membership is MANY-TO-MANY (not exclusive): pass `topicId=[id1, id2, …]` (an array) to link several at once. This is what makes a record resurface when the human later works under any of those topics — an unlinked record is orphaned, invisible to the topic and to topic-scoped recall.

Use judgment on relevance (don't force-fit an off-topic record), but do NOT skip linking just because you're not "in" a topic. At the end, REFRESH the active topic's `summary` (`update_topic`) in full prose, FOR the human, from what this session contributed. Do NOT create a new topic (topics are human-created only).

## 3. Decide what deserves recording at all, THEN route it

Apply this bar BEFORE the routing below. Routing decides where a record goes; this
decides whether it should exist. Get this wrong and the tier no longer matters.

Neuronz.ai exists so that an agent learns from its work and becomes:

1. **More efficient** — it does not repeat errors, does not re-investigate code or
   policy it has already worked out, does not re-litigate what earlier work settled.
2. **More adapted to its user** — it knows their preferences, their way of talking
   and working, and their quirks.
3. **Socially capable** — right tone for the situation; it knows the people and
   groups around the user and their relationship to them.

**A record that serves none of the three is noise, and noise is worse than nothing** —
it competes for a place in the recall a real record needed.

**The test:** something you know or suspect to be true, about something of
importance, that must be recorded because forgetting it would mean re-investigating
it or repeating a mistake.

- A mistake made in THIS session, and the lesson that prevents repeating it, **IS**
  such a fact. Do not discard it as "session-specific" — it is the single highest
  value thing a session produces.
- Something whose value **expires with the session** is not: a command's exit
  status, a test's duration, a CI result, the current state of a branch or a PR.
  "CI came back green in 160 seconds" costs nothing to forget.
- A durable claim you are recording ANYWAY should be written so it stays true —
  strip the transient half rather than dropping the whole record.

Then route each piece that passes:

1. **A fact you learned / now know** → `fact_add`, one atomic self-contained fact per call. FIRST `fact_search` to dedup — add only what's new, `fact_update` to correct an existing fact, skip duplicates. Set its epistemic `status` to `verified`, `unverified`, or `plan` from the evidence instead of presenting an unverified claim as established. Write caveman-`full`, in the profile's record language (the caller names it if it passed one; otherwise the SessionStart operating card does — English unless the profile is set otherwise), whatever language this session runs in: drop function words/filler; keep code, identifiers, numbers, exact errors and any text you quote verbatim in their source language. NEVER store secrets/tokens or transient state (that's `kv_*`, not memory).
2. **A reference doc worth keeping whole** (a runbook, architecture/topology, a decision record you produced) → `add_knowledge`, passing structured `derivedFacts` candidates (`content`, optional `kind`/`status`, and your ADD/UPDATE/NOOP/CONFLICT `decision` after `fact_search`) so the server links them in the same write. On a body update pass the complete current set, possibly `[]`; omitted old doc-only atoms retire. A single fact is NOT a doc — use `fact_add` for that.
3. **What you DID** (a PR, a fix, a deploy, a debug session) → `log_action` with a SUBSTANTIVE summary: what you did, WHY, and what you checked or ruled out. (Asset runs are auto-recorded at SessionEnd; a deliberate `log_action` here captures the why and stands the auto-capture down for the session.)
4. **A durable RULE the user stated** ("always / never …") → do NOT mint it silently. ASK: show the draft rule text + a short title, get an explicit yes, ask the scope ("this profile or all of them?"), then `create_rule`. In SILENT mode there is nobody to ask, so it is ALWAYS `propose_rule` — never `create_rule`.
5. **A change to how a repo here works** (a new repo, a changed layout, a new convention or gotcha this session established) → refresh that repo's card with `upsert_repo_brief` (caveman, within the cap). The brief is pushed WHOLE into context at SessionStart, so a stale one misleads every future session in that repo.

## 4. Do NOT

- Do NOT treat the topic summary as a place to "save what you learned" — it's a human-facing recap, of zero agent-recall value.
- Do NOT invent facts to fill tiers. If the scope holds nothing durable, say so and stop.
- Do NOT record the machinery you were handed. Your context carries the SessionStart operating card, the per-prompt recall payload, the active-rules and topic banners, and your own tool output. None of that is session content — it is the instructions you were given and the plumbing that delivered them. "Rules are authoritative and override facts", "a stored atom is called a FACT", "recall fuses facts/actions/knowledge each prompt" all read like durable truths, and storing them teaches the profile nothing it did not already tell you. A fact must come from the WORK or the USER, never from the frame around them.

## 4b. When you must ask — the memory block

Some things only the user can settle, and staying quiet about them is worse than
interrupting: a contradiction resolved by guessing archives a true fact, and the
supersede is invisible from recall afterwards. So SILENT mode still asks — it just
asks in ONE clearly-marked place and never in ordinary prose.

**Ask ONLY for these. Everything else you decide yourself.**

- A write came back **CONFLICT**, or you found an existing record that contradicts
  what this session established, and the conversation does NOT settle which is true.
- You are about to **supersede or overwrite** an existing record and are genuinely
  unsure the old one is wrong (a wrong UPDATE drops the old fact out of recall with
  nothing signalling to go looking).

Do NOT ask which tier something belongs in, whether a borderline item is worth
saving (if you are unsure it is durable, do not save it), or to confirm a judgment
you are able to make. A block that fires on every sweep gets ignored, and then the
real conflicts get ignored with it.

**How to ask.** ONE block, at the very END of the turn, after the writes that were
never in question — those land regardless. Batch every question into that single
block; at most **three**. If you have more, ask the three most consequential and
leave the rest unwritten rather than flooding. Then STOP: do not wait for an answer
and do not block on it. The user replies whenever they get to it.

Emit it EXACTLY in this shape — the rules and the heading are what make it findable
when someone scrolls back through a long conversation looking for something else,
and the "not about our conversation" line is what stops it being read as a reply to
the work in progress:

```
---

### 🧠 Neuronz.ai memory — a question about what to save, not about our conversation

1. I recorded that <the new claim>, but memory already holds <the existing claim>
   (fact `<id>`). Which is right — or are both true in different contexts?

---
```

Keep each question to what you were about to do, what contradicts it, and the
concrete choice. Never let this block carry a status report — that is §5, and §5
does not run in SILENT mode.

## 5. Report back — LOUD mode only

SILENT mode skips this section entirely: write the records, say nothing, stop —
and §6, not this section, is where a silent turn ends.

Tell the user, in plain prose (NOT caveman), a short per-tier list of what you created or updated: facts (n), knowledge (n, + derived facts), actions (n), and any rule you proposed/created or topic summary you refreshed. Keep it scannable so they can verify and correct.

Records scope to the current profile automatically (the server resolves it). Pass `cwd` = your SessionStart project directory if you need to force the right profile.

## 6. How the turn ENDS — SILENT mode ends HERE, not at §5

This section is last because the step it governs is last, and because a rule stated
at the top of a document loses to the habit that fires at the end of a turn. §0
already told you to stay quiet; this tells you what quiet MEANS mechanically, which
is the part that keeps getting lost.

**The final tool result IS the end. Close the turn with an EMPTY message** — no word,
no punctuation, no emoji, no fence. The harness wants a final assistant message to
close its loop, and a BLANK one is the CORRECT, COMPLETE output here. It is not a
malformed turn, not an omission, not something to fill. That is the whole failure
mode: an empty slot at the end of real work pulls a sentence into it, and "emit no
prose" is too abstract to stop the pull at the moment it happens.

**Nobody is being addressed.** The user did not ask for this turn — a lifecycle hook
did — and the host already printed its own status line while you worked, so they
know a sweep ran. A closing line from you is not courtesy: it is a second copy of a
notice they already have, dropped into a conversation that was about something else.

**Every one of these is a VIOLATION, not a compromise.** One line is not "keeping it
brief":

- `Captured.`
- `Done — 3 facts, 1 action.`
- `Memory updated.` / `Saved.` / `✅`
- `Captured.` followed by a sentence recapping what you saved — the most common one,
  and still a violation.
- ANY acknowledgement that you ran, however short, in any language.

**The ONE thing this turn may emit is §4b's memory block**, and only for a record
conflict you genuinely cannot settle from the conversation. No such conflict means no
output at all. And a memory block is a QUESTION, never a report — never let it carry
a line about what you saved.

**The turn ends at the last record write — it does not go back to the work.** This
is the other half of the same failure, and the more expensive one. On the hosts that
grant this turn it is not a fresh turn: the host appends the instruction to the
conversation and keeps the SAME agent loop running, so you come back with every
unfinished thread still in view under a system prompt that tells you never to stop
while work remains. Finishing the capture then feels like a hand-off back into the
task. It is not one.

- Do NOT resume what the session was doing, however obviously unfinished. The
  lifecycle hook asked for records, and that is the whole scope of the turn.
- Do NOT treat anything you said BEFORE the sweep as approved. An offer you made
  ("I'll land the blunt version unless you want the long one"), a question you asked,
  a plan you proposed — an unanswered question is still unanswered, and this turn is
  not the answer. The user has not spoken since.
- Do NOT start anything new you noticed while reading the session back. If it matters
  it is a record, not an action.
- No tool calls beyond the capture itself: no shell, no edits, no subagents, no
  tickets, no reviews. Reading the session and writing the records is all of it.

A host MAY enforce this by blocking anything outside the capture's own calls; a block
that says so is not an obstacle to route around, it is this section arriving late.
