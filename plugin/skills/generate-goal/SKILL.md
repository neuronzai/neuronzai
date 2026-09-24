---
name: generate-goal
description: >-
  Generate a native Claude Code /goal completion condition (a verifiable
  finish-line "what"), written to the /goal best practices. STATELESS - it
  interrogates you until the end state is nailed down, produces the condition
  text, and stores nothing. It NEVER runs the condition through native /goal —
  that is yours to do. Reach for it while building a plan or drafting a one-off
  goal condition.
---

# Generate a goal

A **goal** is the "what": a single completion condition that says, precisely and
verifiably, what "done" looks like. It is the text you hand to the native
`/goal` command (code.claude.com/docs/en/goal), which then keeps working across
turns until a fast model judges the condition met.

This skill **interrogates, then writes** — and it stops there. It **stores
nothing** and **runs nothing**. In particular it never invokes native `/goal`
and never starts doing the work the goal describes; arming `/goal` is the user's
to do (see [Stop after you write it](#stop-after-you-write-it)). Two callers use
it:

- **Building a plan.** When you draft a plan, generate a goal for the plan's
  finish line and one for each step, then persist them with `add_plan` (`goal` on
  the plan, `goal` on each step). The stored goals are what the user later arms
  `/goal` with to execute the plan or run up to a step.
- **A one-off run.** When the user wants to drive `/goal` directly, generate the
  condition and hand it back for them to paste into `/goal`. Do not store it.

## Drill until it's nailed down

Do **not** jump to writing the condition. For a **single** goal (a one-off or a
plan's finish line), your first job is to interrogate the user until the end
state is unambiguous — every edge pinned, nothing guessed. A vague goal is the
root cause of a `/goal` that loops forever (the judge can never rule it true), so
spend the questions here, not later.

Ask in focused rounds (a few related questions at a time, not a survey), reflect
your understanding back, and keep going until the user **confirms** it's right.
Don't pad with trivial questions — but don't stop at one or two while the end
state is still fuzzy. Probe every dimension that's still open:

- **The measurable end state.** What single fact is true the moment this is
  done? Push past feelings ("it works", "the code is clean") to something binary.
- **The check, and how its proof reaches the transcript.** The `/goal` evaluator
  runs nothing and reads no files — it judges only what Claude has already
  surfaced in the conversation. So nail *how* the end state gets demonstrated in
  output: which command, whose exit code / count / diff must be printed.
- **Scope boundaries.** What is in scope and what is explicitly out — which
  files, dirs, surfaces, cases.
- **Guardrails.** What must **not** change or break along the way (a public API,
  other tests, unrelated files).
- **Partial / failure handling.** What still counts as *not* done, and what the
  goal should do if it can't get all the way there.
- **The bound.** The max number of turns before it should give up regardless.

When crafting goals for the **steps** of a plan, don't interrogate the user once
per step — draft them yourself from the plan's context, then show the full set
for one review pass. Make each step's goal the done-state of *that* step, and the
plan's finish-line goal the done-state of the *whole* plan; they can differ.

## What makes a good condition

Once the drill is done, write the condition so your own output can demonstrate
it. A condition that holds up has three parts:

1. **One measurable end state.** A test result, a build exit code, a file count,
   an empty queue. Not a feeling ("the code is clean") but a fact that is either
   true or false.
2. **A stated check.** How you prove it, phrased so the proof lands in the
   transcript: "`npm test` exits 0", "`git status --porcelain` is empty", "every
   file under `src/auth` is under 200 lines (wc -l output shown)".
3. **Constraints that must not change.** The guardrails: "no other test file is
   modified", "the public API of `foo.ts` is unchanged".

Always add a **bound** so a goal cannot run forever: "or stop after 20 turns".
The whole condition must be **at most 4000 characters**.

## Output

Emit the goal as **one fenced code block whose info tag is `GOAL`** — three
backticks immediately followed by `GOAL` on the opening fence, so the label rides
on the fence line and the block *body* is nothing but the condition. That body is
the whole artifact the user hands to `/goal`, so the copy must come out clean,
with no stray words:

Render the condition itself in **caveman-compressed** form: apply the
`caveman:caveman` skill (full intensity) so the text is as terse as it can be.
Caveman keeps all the substance — commands, exit codes, file paths, counts,
guardrails and the turn-bound stay **verbatim** — and only cuts filler (the
function words the target language lets you omit, hedging, pleasantries). A fast
model still judges this block, so honour caveman's own Auto-Clarity rule: if
dropping a word would blur *what counts as done*, keep that clause in plain form.
Terse, never ambiguous.

```GOAL
tests test/auth pass (npm test exits 0), lint clean (npm run lint exits 0); no
source outside test/auth modified; stop after 20 turns.
```

Then tell the user: **paste the block body into `/goal`**.

For a **plan**, the `GOAL` tag can't say *which* goal it is, so put a one-line
caption above each block naming it, and emit them in order — finish-line goal
first, then each step:

Finish line:
```GOAL
<finish-line condition>
```

Step 1 — theme context:
```GOAL
<step-1 condition>
```

Persist the plan's goals with `add_plan` (the caption is only for the user's eye
— `add_plan` stores the condition text itself). For a one-off there's just the
single block to paste.

## Stop after you write it

Producing the condition is the **end** of this skill. After you output the block:

- **Do not invoke native `/goal`** or any equivalent — arming and running it is
  the user's call, not yours. (Native `/goal` is itself the cross-turn loop;
  starting it here is what turns "draft me a goal" into a runaway loop.)
- **Do not start doing the work** the goal describes, and do not keep working
  toward it across turns. Hand the text back and stop.
- **Do not write the goal to any tier yourself** — this skill is stateless by
  design; only `add_plan` / `update_plan` persist goals (the plan path above).
