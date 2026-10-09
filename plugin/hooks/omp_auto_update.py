#!/usr/bin/env python3
# CLI invoked by the login workflow. Decides whether to OFFER omp's own plugin
# auto-update, and turns it on only when the user has said yes.
#
#   omp_auto_update.py check    -> prints exactly one verdict line (below)
#   omp_auto_update.py enable   -> sets marketplace.autoUpdate=auto, then re-reads it
#
# omp (18.8.4) checks its marketplaces at startup and acts on the setting
# `marketplace.autoUpdate`: `auto` installs plugin updates, `off` skips the check,
# and the default `notify` only writes "N marketplace plugin update(s) available"
# to omp's DEBUG log (src/extensibility/plugins/marketplace-auto-update.ts), which
# the user never sees. A customer on the default therefore stays on the version
# they installed. The setting is the user's, so the plugin never flips it on its
# own: `check` only says whether the question applies, and the workflow runs
# `enable` after an explicit yes.
#
# `check` verdicts, one line each, first word is the contract:
#   ask <current>   omp marketplace install, setting readable and not `auto`
#   on              already `auto`, nothing to offer
#   skip <reason>   not omp, a linked dev checkout (no marketplace pin to update),
#                   or `omp config` could not be read — fail closed: no question
#
# Pure stdlib. Runs inside omp's bash tool, which inherits the environment the
# extension exported (NEURONZAI_HOST, CLAUDE_PLUGIN_ROOT) and omp's own
# PI_CODING_AGENT_DIR / profile selection, so `omp config` edits the same
# settings the running omp reads.

import os
import re
import subprocess
import sys

SETTING = "marketplace.autoUpdate"
WANTED = "auto"
OMP_HOST_ID = "oh-my-pi"
CONFIG_TIMEOUT_SECONDS = 15

# A marketplace install lives in a versioned copy named
# "<plugin>___<marketplace>___<version>" (same shape update_notice.py reads).
# `omp plugin link` points at a checkout instead, which no upgrade ever replaces.
_MARKETPLACE_INSTALL_DIR = re.compile(r"^.+___.+___v?\d+\.\d+")


def _is_omp():
    return os.environ.get("NEURONZAI_HOST") == OMP_HOST_ID


def _is_marketplace_install():
    root = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").rstrip("/")
    return bool(root) and bool(_MARKETPLACE_INSTALL_DIR.match(os.path.basename(root)))


def _omp_config(*args):
    """stdout of `omp config <args>`, stripped, or None when omp cannot answer."""
    try:
        done = subprocess.run(
            ["omp", "config", *args],
            capture_output=True,
            text=True,
            timeout=CONFIG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def check():
    if not _is_omp():
        return "skip not-omp"
    if not _is_marketplace_install():
        return "skip linked-checkout"
    current = _omp_config("get", SETTING)
    if not current:
        return "skip unreadable"
    if current == WANTED:
        return "on"
    return f"ask {current}"


def enable():
    if not _is_omp():
        print("omp auto-update: this session is not running in omp; nothing changed.", file=sys.stderr)
        return 1
    if _omp_config("set", SETTING, WANTED) is None:
        print(f"omp auto-update: `omp config set {SETTING} {WANTED}` failed; nothing changed.", file=sys.stderr)
        return 1
    current = _omp_config("get", SETTING)
    if current != WANTED:
        print(f"omp auto-update: the setting reads back as {current!r}, not {WANTED!r}.", file=sys.stderr)
        return 1
    print(
        "omp auto-update: on. omp now installs Neuronz.ai plugin updates when it starts; "
        "each update is loaded from the session after the one that installed it."
    )
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    action = args[0] if args else ""
    if action == "check":
        print(check())
        return 0
    if action == "enable":
        return enable()
    print("usage: omp_auto_update.py check|enable", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
