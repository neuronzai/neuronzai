---
description: Sign this machine in to Neuronz.ai in the browser and save a refreshable OAuth grant securely
---

# /neuronzai:login

Run the bundled login helper in the foreground so its browser callback remains
alive until sign-in completes:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" "${CLAUDE_PLUGIN_ROOT}/auth_cli.py" login
```

Relay the helper's result exactly and do not ask the user to paste a token. The
helper opens Neuronz.ai in their browser, completes OAuth authorization-code +
PKCE, and stores the refreshable grant outside the plugin package. Never print,
read, or otherwise expose the credential.

## When the machine has no browser

On a headless machine (a server reached over SSH, a container) the helper cannot
open a browser, so it prints a sign-in link and exits 0 instead of failing. Its
output says so explicitly and includes the link.

When that happens, finish the sign-in in two steps:

1. Relay the link to the user exactly as printed, and ask them to open it on any
   device that can reach Neuronz.ai, approve the sign-in, and paste back the code
   the page shows them. The link is good for about ten minutes.
2. Once they paste that code, complete the login by passing it straight through:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" "${CLAUDE_PLUGIN_ROOT}/auth_cli.py" login --code='<the code they pasted>'
```

Keep the `--code=` form (equals, not a space). A pasted value that begins with
`-` is read as another option in the space form and the command dies with
`argument --code: expected one argument`; quoting does not help.

The code is single-use, expires with the link, and is bound to this machine by a
PKCE verifier that never leaves it, so it is safe to pass on the command line —
but it is still the user's to hand over. Never invent one, never retry with a
guess, and if the exchange fails just start the workflow again from step one.

The result of step two is an ordinary refreshable grant, identical to what a
desktop browser login produces.

If the helper instead hangs waiting for a browser that cannot actually render —
a dead X forward, a container advertising a display it cannot use — force the
browserless path explicitly:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" "${CLAUDE_PLUGIN_ROOT}/auth_cli.py" login --no-browser
```

then continue from step 1 above. `NEURONZAI_NO_BROWSER=1` in the environment has
the same effect permanently for that machine.
