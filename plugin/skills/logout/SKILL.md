---
name: logout
description: "Explicit-only Neuronz.ai workflow; use only when the user invokes the logout workflow. Revoke this machine's saved Neuronz.ai OAuth grant and remove its local credentials"
---

# Neuronz.ai logout workflow

Run the bundled logout helper:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" "${CLAUDE_PLUGIN_ROOT}/auth_cli.py" logout
```

Relay the helper's result exactly. Never inspect or print credential values.
