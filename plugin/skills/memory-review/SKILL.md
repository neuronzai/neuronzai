---
name: memory-review
description: "Explicit-only Neuronz.ai workflow; use only when the user invokes the memory-review workflow. Review what Neuronz.ai memory holds for this profile (persona traits, rules, and recent facts)"
---

# Neuronz.ai memory-review workflow

Show the human a clear snapshot of what persistent memory currently holds for
the active profile, using the `neuronzai` MCP tools. Read-only by default —
do not add, edit, or delete anything unless the user explicitly asks.

Optional focus (a topic, kind, or the word `prefs`/`user`): $ARGUMENTS

## What to do

1. **Gather** with the read tools (session profile applies automatically):
   - `get_persona` with no name — the current profile's user persona and traits.
   - `read_rules` — this profile's RULES; note which are `active`
     (binding rules) vs `proposed` (awaiting human approval, NOT yet in force).
   - `fact_list` (newest first) for a browse of recent entries, or
     `fact_search` with `$ARGUMENTS` as the query when the user named a topic.
   - If `$ARGUMENTS` is `user` or `prefs`, focus on just that section.

2. **Present** a compact, scannable summary, grouped:
   - **PERSONA** (profile-scoped user traits) — bullet list.
   - **RULES** for the profile — split **Active** vs **Proposed**. Call
     out proposed ones explicitly: they only become binding once approved —
     either by the human in the Neuronz.ai UI queue, or re-confirmed by them
     in-chat (on their explicit yes, recreate via `create_rule` and
     reject the stale proposal).
   - **FACTS** — the recent or topic-matched entries, with their `kind` and `status`.
   - State the profile name and roughly how many entries exist.

3. **Flag issues**, don't auto-fix: obvious duplicates, stale/contradictory
   facts, or anything that looks wrong. Offer to act (delete via the UI,
   `create_rule` for a new rule the user confirms in-chat, or
   `propose_rule` when they defer) — but only on the user's explicit go.

4. **Precedence reminder** when relevant: RULES (active) override persona traits
   and facts on conflict; proposed rules are not yet rules.

5. If memory is empty or the server is unreachable, say so plainly. If the MCP
   server reports an authentication error, direct the user to the native
   Neuronz.ai login workflow; never ask them to paste a token.
