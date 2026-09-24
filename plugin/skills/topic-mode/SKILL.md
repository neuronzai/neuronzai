---
name: topic-mode
description: >-
  Enter Neuronz.ai topic mode for a deliberate work session. Finds and loads
  the topic, makes it active for this session, scopes durable records under it,
  and covers checking or exiting the topic with a refreshed summary.
---

# Topic mode

A **topic** in Neuronz.ai has two faces: a noun (a long-lived thread that gathers
related work) and a verb — **working on it**. Topic "mode" is the verb: a
work-session lifecycle you ENTER for a topic, work under, then EXIT.

Mode is **per session**: each concurrent agent session in a profile holds its
OWN active topic, so two sessions in the same repo never clobber each other. The
pointer is kept in sync by the plugin (a PostToolUse hook on enter/exit; Claude
Code and omp also exit on their native terminal lifecycle), so you only
call the MCP tools below.

## When to reach for this
- The human **deliberately** says they want to work on a subject: "let's work on
  the websocket epic", "enter topic mode on <X>", "switch to <topic>", "I'm on
  the Sentry migration now".
- NOT on every passing mention of a subject — only when they're framing a work
  session around it. Topic mode is a mindfulness signal, not an auto-filer.

## Enter
1. **Find the topic.** Search first — `search` (BM25 over topics + knowledge) or
   `list_topics` — for an existing topic that fits. Topics are **human-owned**:
   if none exists, ask the human before creating one; only `create_topic` when
   they clearly want to track this subject.
2. **`enter_topic(id)`** — loads the topic's BRIEF (summary + linked PRs/tickets
   + tags) and makes it this session's active topic. Pass `profile` (or `cwd`) so
   it scopes to the right profile, exactly as for other memory tools.
3. Read the brief. Pull members on demand with `get_topic` / `expand` — the brief
   is intentionally light.

## While in a topic
- **Link every new durable record to this topic as you create it** — this is what
  fills the topic, and it's what lets recall surface the work when you (or another
  session) come back to it. The cheap, reliable way: pass `topicId=<this topic's
  id>` on `fact_add` / `log_action` / `add_knowledge`, and the server writes the
  `member` edge in the SAME call (use `link` for something already written).
  Default to linking; skip only a record that's genuinely off-topic. An UNLINKED
  record is invisible to the topic — that's exactly how a feature's work gets
  orphaned.
- **A record can belong to MULTIPLE topics** — membership isn't exclusive. If a
  fact is also genuinely about another existing topic, link it there too (pass
  `topicId` an array, or add a `link`); it then surfaces under whichever of those
  topics you're working under.
- `current_topic` returns what THIS session is working on, if you need to re-check.
- A SessionStart that says a topic is already active means you're **resuming** it —
  keep filing under it unless the human moves on.

## Exit (when the work session ends)
1. **`exit_topic`** — clears this session's active topic and returns the topic's
   brief.
2. **Refresh the summary** from what THIS session contributed:
   `update_topic(summary=…)`. The summary is the topic's one human-read narrative
   (USER-FACING, zero agent-recall value — write it FOR the human, never as a
   place to "save what you learned"; facts → `fact_add`, the work → `log_action`).
   Use real newlines; never literal `\n` / `\uXXXX` escapes.
3. To **switch** topics, just `enter_topic` the new one (no need to exit first).
   Session end auto-exits via the plugin, so a forgotten exit is fine — but
   refresh the summary while the session's contribution is fresh.

## Suggest closing a task topic that's done
A **`task`** topic has a finish line — it carries a status and rides the Kanban;
an **`info`** topic doesn't (data-gathering, no status), so this applies to `task`
topics ONLY. When a task topic's work looks complete, OFFER — one line, in the
human's flow — to mark it `done` via `update_topic(status="done")`. Never flip it
yourself; the human confirms. Whether a topic is "finished" is theirs to decide.

- **The clean signal is plans, not vibes.** A task topic hosts `plans`, each with
  its own `status` and a verifiable `goal`. When all of its plans are `done`
  (`list_plans(topicId=…)` shows none still `active`/`draft`), that's the moment to
  ask "looks like <topic> is finished — want me to mark it done?".
- **Best moments:** at `exit_topic` (you're already recapping the work), and right
  after you mark the topic's LAST plan `done`.
- **Epics:** a parent epic is only done when all its sub-topics are — `get_topic`
  returns its children; roll them up before offering to close the epic.
- Offer once. If they say not yet, leave it `working_on` and move on — don't nag.

## Notes
- One active topic **per session** at a time. The dashboard's Topics view shows,
  per topic, how many live sessions are in it (the "Active" column).
- Topics are queried, not auto-fed: nothing of a topic rides into recall. Pull it
  when relevant (`list_topics` / `get_topic` / `expand`).
