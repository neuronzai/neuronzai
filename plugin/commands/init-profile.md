---
description: Seed a new or thin Neuronz.ai profile from where its knowledge already lives — repositories, GitHub, Notion, docs — reading every source in full and writing the profile's initial facts, knowledge docs, people and proposed rules, then proving them with a recall-only quiz. With no argument inside a git repository, analyses that repository.
---

# /neuronzai:init-profile

Build this profile's starting memory from the places its knowledge already lives.
The finish line: an agent spawned in this profile tomorrow knows this material the way
a colleague who has been there from the start knows it — from Neuronz.ai records
ALONE, without ever opening the sources again. Everything below serves that line, and
§6's quiz is how you prove you reached it.

Sources (verbatim from the user, may be empty): $ARGUMENTS

**The target profile** is the one this session uses — named in this session's
Neuronz.ai briefing, or the one `/neuronzai:switch-profile` / `/neuronzai:new-profile`
moved it to. Pass it as `profile` on EVERY Neuronz.ai tool call, yours and every
subagent's. Do not ask the user to confirm the records before writing them: reading
and writing is the whole job, and the quiz is the check.

## 1. Decide what to read

- **Arguments name sources** ("scan the whole X repo here and everything on its
  GitHub, plus the complete Notion knowledge base and source Y") → those are the
  sources. Split the sentence into a list of concrete sources; each gets its own row
  in §2 and in the final report. "Here", "this repo" or "the current repo" means the
  git repository this session runs in, analysed as in the next bullet; a repository
  the arguments do not name is not read.
- **No arguments, and this directory is inside a git repository** (`git rev-parse
  --show-toplevel` succeeds) → the source is THAT repository, analysed the way an
  `/init` analysis of a codebase works: layout and what each top-level part is for;
  how to install, build, run, test and lint it (the exact commands); architecture and
  the main flows through it; conventions and the rules its docs state; gotchas;
  recent history (`git log` — what changed lately, what was reverted, why);
  `CLAUDE.md`, `AGENTS.md`, `README*`, `CONTRIBUTING*` and `docs/`. **Write NO file
  into the repository** — not a `CLAUDE.md`, not a note, nothing. Everything becomes
  Neuronz.ai records.
- **No arguments and not inside a git repository** → ask the user what to ingest and
  stop until they answer.

## 2. Check access FIRST — before reading anything

For each source, confirm you can actually reach it before you start:

- a local repository → the path exists and `git` works there;
- a remote repository or its issues and pull requests (GitHub, GitLab, Gitea) → the
  matching MCP server or CLI is present in this session AND authenticated (one cheap
  read call, e.g. fetch the repository's metadata);
- Notion, Confluence, Google Drive, Slack, a wiki → the matching MCP server is present
  and one cheap read succeeds;
- a URL → it loads.

Report every unreachable source UP FRONT, in one short list, each with exactly what is
needed (which MCP server to install or re-authenticate, which CLI login to run). Never
investigate an unreachable source by guessing around it, and never fill its gap from
general knowledge. Continue with the reachable ones; the unreachable ones go into the
final report as skipped, with the reason.

## 3. Read ALL of it — record what a colleague would know

"Take the whole knowledge base into account" means **read every page, file and thread
of every reachable source** — it does NOT mean copy it. Reading is complete; recording
is selective.

**The bar for a record:** what an experienced colleague who has been there from the
start would know and could bring up when it matters. That is:

- what the product or business is, who it serves, how it makes money;
- who owns what — people, teams, services, decisions;
- how things are done: release, deploy, incident response, review, on-call,
  onboarding, the exact commands;
- the internal vocabulary — names, acronyms, codenames and what they mean;
- past decisions and WHY — what was tried, what was dropped and the reason;
- gotchas, known traps, things that look wrong but are deliberate;
- what is still open.

Three things never pass: something whose value expires immediately (a build's status
today, the current state of one pull request, a ticket's assignee this week);
boilerplate the reader could regenerate in a second (a license text, a generated
file); and any secret — a password, token, API key, private key or connection
string with credentials — which is never written into a record, even when the
source holds it in plain text (record WHERE it lives and how to obtain it instead).
Write each record so it stays true: keep the durable part, drop the transient
half.

**Every record is self-contained.** A link may ride along as provenance, but it never
replaces the content: "see the Notion page on releases" is worthless to an agent that
cannot open Notion. Write the release steps themselves.

**Routing — where each thing goes:**

- **One atomic claim** → `fact_add`. A term and its meaning → `kind: "glossary"`. A
  named thing (a service, a customer, a system, a team as a thing) → `kind: "entity"`.
  Everything else → `kind: "fact"`. A claim the source hedges, or that you could not
  confirm → `status: "unverified"`; stated intent or a roadmap → `status: "plan"`.
- **A document worth keeping whole** — a runbook, a process, an architecture overview,
  a decision record, and ONE overview per repository → `add_knowledge`, with:
  - a **stable `source` key** derived from where it came from, so a re-run UPDATES the
    same document instead of adding a duplicate: `repo://<name>` for a repository's
    overview, `repo://<name>/<path>` for a doc inside it, `github://<owner>/<repo>/…`
    for issues, pull requests and wikis, `notion://<page-id>`, `url://<host>/<path>`;
  - `derivedFacts`: the document's complete set of atomic facts, each
    `{content, kind?, status?}` — not a separate `fact_add` per atom.
- **A colleague or a team** → `add_persona_subject` (`kind: "person"` or
  `kind: "group"`, with the aliases people use for them), then each durable trait
  about them — what they own, how they work, how they like to be approached →
  `fact_add` with `kind: "persona"` and `subject: "<their name>"`. Create a group with
  `add_persona_subject` BEFORE attributing traits to it.
- **An imperative addressed to the people or agents working there** ("always run the
  migrations before merging", "never deploy on Fridays") → `propose_rule`, with the
  passage it came from as evidence. Never `create_rule`: a rule binds only after the
  user approves it. A sentence that DESCRIBES how the system behaves ("the sweep never
  touches the profile while anything is capturing") is a fact, however it is worded.

**Settle the look-alikes.** `fact_add` and `add_knowledge` may answer with
`neighbors` — live facts that already say nearly the same thing. Read them
(`fact_get`) and settle with `fact_resolve(newId, targetId, outcome)` only the ones
that say the same thing or contradict it: `duplicate` when they say the same thing,
`supersedes` only when the material shows the value CHANGED, `conflicts` when two
sources disagree and you cannot tell which is current. Leave a merely related
neighbour alone.

## 4. Scale — split what does not fit

A source too large to read in one context (a big monorepo, a whole Notion workspace,
years of pull requests) is split into sections — by directory, by page tree, by year —
and each section is handed to a subagent. Give every subagent the target profile, its
section, and §3 of these instructions VERBATIM; each writes its own records. Repository
overviews and cross-cutting documents are yours, written after the sections report
back, so they can describe the whole. You dedupe through the `neighbors` the writes
return: settle, as above, the ones the subagents reported and did not resolve.

On a harness without subagents, work through the sections one at a time yourself.

## 5. Re-running

Running this again on the same sources REFRESHES the profile: reuse the same `source`
keys so each document is updated in place (pass its complete current `derivedFacts`,
which retires atoms that no longer hold), and settle `neighbors` as above so a fact
that changed supersedes the old one instead of sitting next to it.

## 6. Prove it — the recall-only quiz

This is the pass condition. Do not report success without it.

1. **Write the questions** from the SOURCES, not from your records — 15 to 30 of them,
   spread over every source and every kind of knowledge: who approves or owns X, what
   term Y means, why Z was dropped, how to run the tests, how a release goes out, what
   to do when W breaks. Keep the expected answer for each, from the source.
2. **Hand the questions to a FRESH subagent with NO access to the sources.** Its
   instructions: answer each question using ONLY the Neuronz.ai tools (`recall`,
   `fact_search`, `get_knowledge`, `get_persona`, `read_rules`) with
   `profile: "<target profile>"`; do not read files, run shell commands, browse, or
   call any other tool; when the records do not answer a question, reply `UNKNOWN`
   rather than guessing. Give it the questions only — never the expected answers.
3. **Grade** every answer against the source: correct, partially correct, wrong, or
   unknown. Every miss is a gap in the records: write the missing record (§3's routing),
   then quiz the missed questions again with another fresh subagent.
4. **Iterate** until every question is answered, or the remaining misses are genuinely
   unanswerable from the sources themselves (say which, and why).

On a harness without subagents, answer the questions yourself from the Neuronz.ai
tools only, and say in the report that the quiz was not independent.

## 7. Report

One short report to the user:

- **Sources read** — each source and how much of it (files, pages, pull requests).
- **Sources skipped** — each, with the reason and what would unlock it.
- **Records written** — counts by kind: knowledge docs (created / updated), facts by
  kind (fact, glossary, entity, persona), people and teams, proposed rules (and that
  they wait for the user's approval in the dashboard).
- **Quiz** — the score on the first pass and the final pass (e.g. `19/25 → 25/25`),
  and any question left unanswered with why.
