---
name: switch-profile
description: "Explicit-only Neuronz.ai workflow; use only when the user invokes the switch-profile workflow. Re-scope THIS native agent session to a different Neuronz.ai profile (e.g. you launched in the wrong directory). Session-only — does NOT map the directory to the profile."
---

# Neuronz.ai switch-profile workflow

Re-scope the CURRENT session's persistent memory to a different profile, for the
rest of this session only. Use it when the session matched the wrong profile —
e.g. you started the agent in the wrong directory and it resolved (or auto-created)
a profile you didn't mean.

This is SESSION-SCOPED and ephemeral. It does NOT create a cwd→profile route or a
(profile, cwd) topic anchor (it never calls `set_dir_profile`): the directory is
not mapped to the profile, and a fresh session in this directory resolves its
normal profile again. After the switch, new writes, recall, capture and run records
use the chosen profile. Receipt-ledger entries collected before the switch remain
owned by their original profile; SessionEnd flushes every profile ledger under the
profile that produced it.

Target profile (verbatim from the user, may be empty): $ARGUMENTS

## What to do

1. **Empty / reset.** If `$ARGUMENTS` is empty or is `reset` / `off` / `clear`,
   run the reset form (the second command in step 3) and tell the user this
   session reverted to its directory/env profile. Stop.

2. **Resolve the target.** Call `list_profiles` and match `$ARGUMENTS` against it:
   - Exact match → use it.
   - One obvious case-insensitive / near match → use it (say which one).
   - No match → tell the user it doesn't exist yet, show the closest existing
     names, and ask whether to switch anyway (that starts a NEW, empty profile
     under that name the moment this session first writes to it). Only proceed on
     their explicit yes.

3. **Register and set the override** — run this, replacing `<NAME>` with the resolved profile
   (the session id is filled in automatically by the CLI):

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/switch_profile.py" --session-id "${CLAUDE_SESSION_ID}" --profile "<NAME>"
   ```

   To reset instead (revert to the directory/env profile):

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/switch_profile.py" --session-id "${CLAUDE_SESSION_ID}" --reset
   ```

   This is the trusted server-side switch path. If it fails, the helper restores
   the previous local override; report the failure and do not claim the switch.

   It also re-materializes the new profile's assets and tells you what this
   session can and cannot see of them. Relay that sentence as it came: a kind
   this harness does not re-scan is on disk but NOT invocable here yet, so do not
   offer a command or subagent it says needs a new session. If nothing but the
   assets is wrong later, `/reload-assets` redoes just that leg.

4. **Thread it on your own tool calls.** For the REST of this session, pass
   `profile="<NAME>"` on every `neuronzai` MCP tool call (`fact_add`, `recall`,
   `fact_search`, `log_action`, `add_knowledge`, …). The HTTP MCP transport doesn't
   carry the session cwd, so the explicit `profile` arg is what scopes YOUR
   reads/writes. The push hooks (auto-recall, self-sweep, end-of-session sweep,
   run records) pick up the override automatically — you don't manage those.

5. **Confirm** in one line: the new profile, and that it's session-only (no route
   or anchor created, reverts when the session ends). If the helper reported no
   session id (its CLI didn't substitute `${CLAUDE_SESSION_ID}`), relay that — the
   switch can't be scoped to a session without it, so it was not applied.
