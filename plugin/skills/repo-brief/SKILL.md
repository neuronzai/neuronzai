---
name: repo-brief
description: Generate or refresh a Neuronz.ai "repo brief" — the small per-repo orientation (layout, build/test commands, load-bearing conventions, gotchas) that Neuronz.ai pushes WHOLE into context at SessionStart. Reach for this when the SessionStart context flags that the current repo has no brief yet, when you've changed how a repo works and its brief is now stale, or when the user asks to set up / refresh repo briefs. Writes via the upsert_repo_brief MCP tool (caveman, size-capped).
---

# Repo brief

A **repo brief** is the per-repo orientation Neuronz.ai pushes WHOLE into every
SessionStart — the fresh-kept equivalent of a `CLAUDE.md`. It is small,
caveman-formatted, and hard-capped in CHARACTERS (`repo_brief_max_bytes` — the
key name is legacy). One brief per repo,
written via the `upsert_repo_brief` tool.

## When to reach for this
- SessionStart context says **a repo here has no brief yet** → create one.
- You **changed how a repo works** (layout, build/test, a convention, a gotcha)
  and its brief is now stale → refresh it.
- The user asks to **set up / seed** briefs for the profile's repos → bulk (the
  `/repo-brief-init` command drives that, applying this procedure per repo).

## What a good brief holds
The 3–5 things a fresh agent would otherwise rediscover: is it a monorepo /
workspaces, the top-level layout, the build / test / typecheck commands, and the
load-bearing conventions or gotchas. Orientation, not documentation.

## Procedure
1. **Scope.** Default = the CURRENT repo (the cwd's git toplevel). For a bulk ask,
   discover every git repo under the cwd and CONFIRM the set before writing:
   ```bash
   ROOT="${1:-$PWD}"
   find "$ROOT" -maxdepth 3 -type d -name .git -prune 2>/dev/null \
     | while read -r g; do git -C "$(dirname "$g")" rev-parse --show-toplevel; done | sort -u
   ```
   `repoKey` = the **git-toplevel basename** (disambiguate with a parent segment
   only on a basename collision).
2. **New vs refresh.** Run `list_repo_briefs` first — a repo that already has a
   brief is a REFRESH (overwrite), not a new one; say so.
3. **Compose.** If the repo has a `CLAUDE.md` (or `AGENTS.md` / an orientation
   README), **distill** it; else **analyze** the top-level layout + `package.json`
   scripts / Makefile / pyproject + the load-bearing conventions. Write **caveman
   `full` style** (drop the function words the target language lets you omit — in
   English, articles a/an/the — plus filler; fragments fine) but keep commands, paths,
   identifiers, versions and exact strings VERBATIM. Real newlines, never literal
   `\n`. Terse — it rides into context every session.
4. **Pin the repo state.** Read the commit the brief describes:
   ```bash
   git -C "$(git rev-parse --show-toplevel)" rev-parse HEAD
   ```
   Pass it as `sourceCommit`. This is what lets a later session tell an old card
   whose repo **has not moved** (no warning — it is still accurate) from one the
   code **has changed under** (flagged possibly-stale, with this sha to diff
   against). Skip it only when the repo is not a git checkout.
5. **Write** with `upsert_repo_brief { repoKey, body, sourceCommit }` (the active
   profile applies; pass `profile` only for a repo in a different one). The body is
   hard-capped: on a `413 {currentSize, cap}` rejection, compress harder (cut
   prose, keep the technical tokens) and retry — never ask the server to truncate.
6. **(Optional) Deep docs.** For a repo with a rich `CLAUDE.md`, also file its
   detailed sections as **knowledge** (`add_knowledge`, `source:
   repo://<repoKey>/<section>`) for on-demand detail; the brief can point to it.
7. **Report** what you created vs refreshed (repoKey + size). Briefs appear in the
   NEXT session's context and are viewable under **Knowledge → Repo Briefs**.
