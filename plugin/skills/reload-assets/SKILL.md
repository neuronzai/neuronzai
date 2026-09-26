---
name: reload-assets
description: "Explicit-only Neuronz.ai workflow; use only when the user invokes the reload-assets workflow. Re-sync this session's Neuronz.ai assets (skills, commands, subagents) from the server, without starting a new session"
---

# Neuronz.ai reload-assets workflow

Re-materialize the CURRENT session's profile assets: refetch them from Neuronz.ai
unconditionally (no cache short-circuit), rewrite the on-disk store, reconcile the
symlinks this harness reads, and remove any set another profile left behind.

Reach for it when:

- you ran `/switch-profile` and the session is still offering the old profile's
  skills and commands;
- an asset was edited in the dashboard and you want it now;
- an asset's files look wrong or truncated (the refetch rewrites every entry);
- a session that shares an asset directory with another one lost its links.

## What to do

1. **Run the reload** — one command, nothing to fill in:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/reload_assets.py"
   ```

   The plugin intercepts this call and does the work itself, so it reports back
   instead of executing. That is expected — the interception is what carries the
   in-session rescan directive. Treat the message you get as the result.

2. **Relay the report verbatim**, then say what it means for the rest of this
   session in one line. The report is written from what this harness can actually
   re-scan, so do not upgrade it: if it says some kinds need a newly started
   session, those commands/subagents are NOT available here yet, and
   offering one would send the user looking for something that is not there. If it
   names something they can run themselves, repeat that exactly.

3. **If it reports the token was rejected**, say so plainly and point at the login
   workflow — the assets were removed on purpose, and a second reload will not
   bring them back.
