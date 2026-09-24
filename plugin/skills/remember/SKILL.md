---
name: remember
description: "Explicit-only Neuronz.ai workflow; use only when the user invokes the remember workflow. Save a durable fact to persistent Neuronz.ai memory for this profile"
---

# Neuronz.ai remember workflow

Save what follows as ONE durable fact in persistent memory for the current
profile, using the `fact_add` tool from the `neuronzai` MCP server.

The fact to remember (verbatim from the user, may be empty): $ARGUMENTS

## What to do

1. **Decide the content.**
   - If `$ARGUMENTS` is non-empty, that text IS the fact — store it as-is
     (lightly cleaned into a single self-contained sentence).
   - If `$ARGUMENTS` is empty, distill ONE durable, atomic fact worth keeping
     next session from the recent conversation. If nothing is clearly worth
     remembering, say so and stop — do not invent a fact.

2. **Curate, don't dump.** One atomic, self-contained fact per call. Split
   multiple facts into multiple `fact_add` calls. Phrase it so a future
   session understands it with no other context. Never store secrets/tokens or
   transient task state (that belongs in `kv_*`, not memory).

3. **Pick the `kind`:** `fact` (default) | `persona` | `entity` |
   `instruction` | `glossary` | `other`. Note: a *rule* the user wants
   enforced as a RULE belongs in the rules tier, not here — ask them
   ("want me to create a rule? this profile or all of them?") and on
   their explicit yes use `create_rule` (or `propose_rule` if they
   defer). A descriptive observation about a person's tastes, expertise, or
   working style uses `kind:"persona"` (and `subject` for a colleague); phrase
   it as an observation, never as a command.

4. **Avoid duplicates.** If unsure whether this is already stored, run
   `fact_search` first; skip the add if it already exists, or mention the
   near-duplicate.

5. **Call `fact_add`** with `{ content, kind?, status? }`. Use `status:"verified"`
   only for established claims; otherwise use `unverified` or `plan`. The session's resolved
   profile is applied automatically (the server scopes it) — only pass an
   explicit `profile` if the user names a different one.

6. **Confirm** in one line: what you stored, its `kind`, and the profile.
