#!/bin/sh
# neuronzai hook launcher.
#
# Claude Code runs a hook's command through a POSIX shell: `sh` on macOS/Linux,
# and Git Bash on Windows (install Git for Windows — it ships with git — or run
# Claude Code inside WSL). This launcher resolves a Python 3 interpreter
# (python3 -> python -> py, whichever the platform provides) and execs the target
# hook script passed as "$@" (exec preserves stdin), so the hooks fire regardless
# of how Python is named on PATH.
#
# Each candidate is verified with `--version` before use, so a non-working shim —
# notably the Windows "App execution alias" stub that resolves `python`/`python3`
# even when Python isn't installed — is SKIPPED (it exits non-zero for --version)
# and the loop falls through to a real interpreter (e.g. `py`) or the error below.
#
# On no interpreter it exits non-zero with a clear stderr line, which Claude Code
# surfaces as a "<hook> hook error" notice (even without --debug) without blocking
# the session — visible, not silent.
#
# WHY hooks.json wraps this launcher in a fallback one-liner (do not "simplify" it
# back to a bare `sh "$CLAUDE_PLUGIN_ROOT/hooks/run.sh" …`): Claude Code installs a
# marketplace plugin into a VERSION-STAMPED dir (…/neuronzai/neuronzai/<version>)
# and runs a GC sweep at startup/update that can DELETE the old version dir while a
# still-running session's hooks still point at it (their commands were expanded at
# session start). Once that dir is gone, this run.sh is gone with it, and every
# hook in the live session errors. So each hooks.json command first checks whether
# its own "$CLAUDE_PLUGIN_ROOT/hooks/run.sh" still exists and, if not, falls back to
# the newest SURVIVING sibling version dir before exec'ing run.sh (passing the
# target script relative to the plugin root as "$0" and its args as "$@", so the
# fallback snippet stays byte-identical across every entry). A dev/clone checkout
# (no version tail, never swept) always finds run.sh and never triggers the
# fallback; a true uninstall finds no sibling and fails loudly exactly as before.
for _py in python3 python py; do
  if command -v "$_py" >/dev/null 2>&1 && "$_py" --version >/dev/null 2>&1; then
    exec "$_py" "$@"
  fi
done
echo "neuronzai: no Python 3 interpreter found (tried python3, python, py). Hooks are disabled until Python 3 is installed and on PATH." >&2
exit 1
