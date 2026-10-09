---
name: new-profile
description: "Explicit-only Neuronz.ai workflow; use only when the user invokes the new-profile workflow. Create a NEW Neuronz.ai profile for this directory and move this session onto it — e.g. this directory sits inside another profile's recursive route and should have its own memory."
---

# Neuronz.ai new-profile workflow

Create a new, empty profile, route THIS session's directory to it, and move the
current session onto it. Use it when this directory resolved to a profile it
should not share — above all when it sits inside a directory another profile
covers together with its subdirectories. Automatic creation never happens there,
because the parent's route already matches.

The route it creates covers this directory only (unless the user asks for its
subdirectories too). It is a longer match than the parent's route, so it wins:
every later session started here uses the new profile, while the rest of the
parent's tree keeps the parent's profile.

Arguments (verbatim from the user, may be empty): $ARGUMENTS

## What to do

1. **Pick the name.** Use the name in `$ARGUMENTS`. If it is empty, ask the user
   for one and stop until they answer. If they asked for subdirectories to be
   included ("and its subfolders", `--recursive`), add `--recursive` below.

2. **Create and route** — run this, replacing `<NAME>`:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/new_profile.py" --name "<NAME>"
   ```

   If it reports that nothing was created, relay its reason and stop:
   - The name already exists → offer `/switch-profile <NAME>` for this session,
     or `set_dir_profile` to route this directory to that existing profile.
   - This directory already has a route of its own → tell the user which profile
     it routes to. Repointing it is a deliberate move (`set_dir_profile`), never
     something to do on your own.

3. **Move this session onto it** — run the `/switch-profile` helper with the same
   name, exactly as that command does:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/switch_profile.py" --session-id "${CLAUDE_SESSION_ID}" --profile "<NAME>"
   ```

   Relay what it says about this session's assets as it came. If it fails, say
   the profile and route were created but this session still uses its previous
   profile until a new session starts here.

4. **Thread it on your own tool calls.** For the rest of this session, pass
   `profile="<NAME>"` on every `neuronzai` MCP tool call.

5. **Confirm** in one or two lines: the new profile, the directory now routed to
   it, and that new sessions started there use it. Then offer to fill it: the
   profile starts empty, and `/neuronzai:init-profile` seeds it from where its
   knowledge already lives (this repository when run with no argument, or the
   repositories, GitHub, Notion and docs the user names).
