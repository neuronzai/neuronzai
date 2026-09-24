---
name: repo-brief-init
description: "Explicit-only Neuronz.ai workflow; use only when the user invokes the repo-brief-init workflow. Seed Neuronz.ai repo briefs for every git repo in this profile (the explicit bulk on-ramp; applies the repo-brief skill per repo)"
---

# Neuronz.ai repo-brief-init workflow

The explicit **bulk** on-ramp for **repo briefs** — the per-repo orientation
Neuronz.ai pushes at SessionStart. (A single repo's brief is normally created or
refreshed on its own via the `repo-brief` skill; this command seeds a whole
profile's repos at once.)

Optional scope (a directory to scan, else the cwd): $ARGUMENTS

## What to do
1. **Discover** every git repo at/below the scan root (`$ARGUMENTS` or `$PWD`):
   ```bash
   ROOT="${ARGUMENTS:-$PWD}"
   find "$ROOT" -maxdepth 3 -type d -name .git -prune 2>/dev/null \
     | while read -r g; do git -C "$(dirname "$g")" rev-parse --show-toplevel; done | sort -u
   ```
   If you are inside one repo but the profile spans siblings (e.g. a multi-repo
   workspace like jimini), offer to scan the parent directory instead.
2. **Show the detected set and get an explicit OK** before writing — creating
   briefs is a persistent write. Run `list_repo_briefs` first so refreshes
   (overwrites) vs new briefs are clear.
3. **For each confirmed repo, apply the `repo-brief` skill's procedure** (distill
   `CLAUDE.md` or analyze → caveman, capped → `upsert_repo_brief`; on a 413,
   compress and retry). `repoKey` = the git-toplevel basename.
4. **Report** created vs refreshed (repoKey + size); the briefs appear at the next
   SessionStart and are viewable under **Knowledge → Repo Briefs**.
