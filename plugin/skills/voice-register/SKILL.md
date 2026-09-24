---
name: voice-register
description: >-
  Set up a Neuronz.ai voice register — the condition that says WHEN a
  way-of-writing applies, plus the atom group and claims it points at. Reach for
  it whenever the user says how something should be written ("always open with
  the ask", "be blunter with Marie", "write my PR descriptions like this"), or
  wants a voice they already have applied somewhere new. Reads the existing
  arrangement first so it extends rather than duplicates, keeps the WHEN out of
  the claim, and verifies the new layer actually activates.
---

# Create a voice register

Voice is **three objects**, deliberately kept apart:

- An **atom** is one neutral claim about how something is written — "opens with
  the ask, never with pleasantries". It says nothing about when it applies.
- An **atom group** is a named set of those claims, and it can **inherit** from
  other groups (a DAG). Groups are **org-wide**. This is the reusable half: the
  content.
- A **register condition** — the tools just call it a register — states the axes
  saying **when** a group applies and points at exactly **one** group. It is the
  only profile-scoped one. This is the WHEN.

The split is the whole design. Applying a voice somewhere new is one new
condition pointing at the group you already have — nothing is copied, and adding
a claim to that group reaches every place it applies. Deleting a condition stops
applying that voice *there* and leaves the group intact.

**You own the organisation, not the user.** Never make them think in registers,
groups, or axes. Take their words, decide the arrangement, state it back in one
line, and act.

## When to reach for this

- The user states how something should read — generally, or for a person, an
  act, a language, a situation.
- The user wants an existing way-of-writing applied somewhere new.
- You corrected course after they rewrote your draft, and the correction should
  hold from now on.

**Not** for a one-off ("make this draft shorter" is an edit, not a voice). **Not**
for a rule: if breaking it produces the *wrong action* it is a rule
(`propose_rule`); if it produces badly *written* output it is voice. **Not** a
fact. And answering a pending ask about a claim you inferred earlier is
`resolve_voice_ask`, not a new condition.

## 1. Read the arrangement first

Never skip this — it is what keeps the tier from growing near-duplicates.

- `list_registers` — this profile's conditions, their axes, their specificity,
  and the group each applies.
- `list_atom_groups` — the org's groups with their parents, full ancestry, and
  the conditions applying each **across profiles**.

You are looking for: a group that already says this, a condition that already
covers this moment, and anything your new condition would tie with.

## 2. Pin the axes from the user's own words

Five axes. **An omitted axis means ANY.** Conditions are **flat** — nothing is
inherited to fill one in, so state every axis you mean.

- **`direction`** — `user_to_colleague` (you ghost-write AS the user) /
  `agent_to_user` (you write TO them) / `agent_to_machine` (you write a stored
  record). **Bind this on nearly every condition.** Leaving it open applies one
  voice to both writing as the user and writing to them, which is almost never
  meant.
- **`audience`** — WHO it is for. Always a **persona subject**: pass a name, an
  alias, or an id. The subject must exist first — `add_persona_subject`, with
  `kind=group` for a class like "clients". An unknown name is refused, an
  ambiguous one asks for the id, and the user themself is refused outright
  (writing TO them is `direction: agent_to_user` with audience left open).
- **`act`** — one of `slack_post`, `email_send`, `pr_create`, `pr_review`,
  `issue_comment`, `notion_write`, `commit_message`, `doc_write`, `chat_reply`
  (in-conversation prose, no tool call), `record_write` (writing a durable
  record body).
- **`language`** — ISO 639-1 (`fr`, `en`, `pt-br`).
- **`situation`** — a free kebab-case slug. Read the next section before you
  bind one.

Specificity is the count of axes bound to a **concrete value on this row**. More
specific wins; the everything-open condition is simply the general voice and
sits underneath.

### Binding a situation costs automatic activation

The automatic lanes always probe `situation: *`, and `*` on the probe side
matches only conditions that are open on that axis. So **a condition binding a
concrete situation never fires from prompt-time injection or the tool gate.** It
is instead announced by name in the conditional-layer index with its WHEN, and
activates only when an agent judges the situation holds and calls
`get_resolved_voice` passing it. The write is accepted and flagged
(`situation_never_produced`) rather than refused.

Bind a situation **only** when the trigger is a judgment nothing but the agent
can make ("while a long task runs unattended", "during an incident"). Otherwise
leave it open and narrow with `act` / `audience` / `direction`, which the lanes
do detect.

## 3. Pick the group — reuse before you create

The condition is cheap; the group is the asset. In order of preference:

1. **An existing group already says this** → create the condition pointing at
   it. Nothing else to write. This is the common case and the one the design is
   built for.
2. **An existing group is close, and this narrows it** → `add_atom_group` with
   the existing one as a `parent`. Inheritance composes content, so a condition
   pointing at the child brings the whole stack.
3. **Genuinely a new way of writing** → `add_atom_group` with a slug, a name
   (the name heads the layer in the composed voice, so make it say the *style*
   — the condition says the *when*), and `parents` for whatever general group it
   sits on top of.

Never copy a claim between groups. `bind_voice_atom` holds one atom in a second
group, so it lives once and is referenced twice.

## 4. Write the claims

`add_voice_atom`, one atomic claim per call, `groups` naming at least one group:

- **`facet` — state it, never leave it to be guessed**: `tone` (how it sounds —
  direction-sensitive), `mechanical` (typography, punctuation, formatting —
  genuinely spans directions), `structure` (ordering, length, what to open
  with), `other`. A `tone` claim reachable only through a direction-open
  condition is the mistake this field exists to catch.
- **`status`**: the user's words in chat are the approval, so a stated
  preference is `verified` (the default). Anything **you** inferred is
  `unverified` with its evidence in `context` — it is asked about at relevance
  and **never applied** until they confirm.

**Keep the WHEN out of the claim.** "Speak plainly when writing to Marie", filed
in the general group, tells the agent to speak plainly to *everyone* and the
part about Marie is just words. The condition carries the when; the atom carries
the claim. The server checks this and can hand back a `splitSuggestion` naming
the words it matched and the axis they belong on — advisory, never a refusal,
and the atom is stored either way. Act on it when the words really are the
claim's scope; ignore it when the mention is just subject matter ("names people
by member ID rather than email address").

## 5. Scope the condition

- `global: true` — every profile of the org, **including profiles that don't
  exist yet**. The right choice for a voice that isn't about a project: how you
  explain things back to the user, how you write stored records.
- `profiles: [...]` — named projects.
- Neither — this profile only.

**A concrete `audience` forces the scope to be exactly the profile you are
calling from**, and anything else is refused (409), in either order. The
addressee is a persona row that exists in one profile; shared wider it would
activate for nobody. Drop the audience, or keep the condition local.

## 6. Create it, then prove it fires

`add_register` with `slug` (unique in the profile), `name`, `group`, the axes,
and the scope. Then **verify**: call `get_resolved_voice` with the exact
situation you built it for and check three things.

- The new layer is there. If it isn't, an axis you bound is narrower than what
  the probe carries — most often a `situation`.
- It ranks where you expect, most-specific-first.
- `conflicts` is empty. An `equal_specificity` conflict means two conditions
  claim the same moment with the same score and the tie is broken by declaration
  order — a coin toss deciding how the user sounds. Fix it by binding one more
  axis on one of them, and say which you chose and why.

Report back in one line: what now applies, where, and what it sits under.

## Applying a voice somewhere new

One `add_register` pointing at the existing group. Do not create a second group,
do not copy atoms. To stop applying it there later, `delete_register` — the
group survives everywhere else it is used.

## Don't

- **Don't widen** an existing condition's axes or scope, and don't re-point it at
  another group, on your own initiative. Each of those changes how the user
  sounds somewhere they didn't ask about — ask first.
- **Don't** file voice as a fact or a rule.
- **Don't** treat an empty result as breakage. Nothing is seeded; a profile with
  no conditions genuinely has no voice yet.
