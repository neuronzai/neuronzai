#!/usr/bin/env python3
# Multi-host hook dispatcher (topic 90ae46be).
#
# ONE entry point for every hook on every host. The wire config (a host's
# hooks.json / config.toml) invokes:
#     entry.py --host <host> --event <canonical-event> [--chunk N]
# with the host's hook payload on stdin. entry.py selects the host adapter, parses
# stdin into a core Event, dispatches to the core handler for that event, and lets
# the adapter emit the result. Behind a byte-stable launcher (run.sh) this keeps a
# host's hash-pinned hook trust from re-prompting on every release — the launcher
# never changes; the versioned logic lives here.
#
# Fail-open ALWAYS: any error exits 0 with no output so a hook never blocks a
# session. Pure stdlib.

import argparse
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _arm_deadline():
    """Self-enforce the manifest timeout, for hosts that spawn us DETACHED.

    A host that awaits its handler can kill it on its own clock. A host that
    CANNOT — omp caps an awaited session_shutdown handler at ~2s, so the terminal
    handlers are spawned detached and outlive the agent — has nothing left to
    enforce with once it exits. Then the only process that can honour the
    manifest's timeout is this one, and a timeout nothing enforces is a number,
    not a budget.

    A daemon timer plus a hard _exit rather than a signal: SIGALRM is POSIX-only
    and interrupts just the main thread, and by the time this fires there is no
    caller left to report to and nothing worth unwinding for. Absent or
    unparseable means no deadline, so every existing host keeps its behaviour.
    """
    raw = os.environ.get("NEURONZAI_HANDLER_DEADLINE", "").strip()
    if not raw:
        return
    try:
        seconds = float(raw)
    except ValueError:
        return
    if seconds <= 0:
        return
    timer = threading.Timer(seconds, lambda: os._exit(75))  # EX_TEMPFAIL
    timer.daemon = True
    timer.start()

# host name -> adapter factory. New host = one line here + one hosts/<name>.py.
def _load_host(name):
    if name == "claude-code":
        from hosts.claude_code import ClaudeCodeHost
        return ClaudeCodeHost()
    if name == "oh-my-pi":
        from hosts.oh_my_pi import OhMyPiHost
        return OhMyPiHost()
    raise ValueError(f"unknown host: {name}")


def resolve_host_name(explicit):
    """Resolve a host adapter NAME for a caller that may not have been told one.

    Every generated wire config passes `--host` explicitly, so this is for
    `--host auto` and the user-invoked helper CLIs: their command bodies are
    shared markdown, and the shell that runs them carries no argument the
    generator could have filled in.

    A hardcoded fallback is the wrong shape for that question. The CLI's host
    decides which directories are rewritten and which rescan sentence is printed,
    so guessing wrong repairs a directory the running harness never reads and then
    reports success — the exact failure the reload report exists to prevent.

    So a bridge that CAN declare itself does (`NEURONZAI_HOST`, exported into the
    tool shell alongside the plugin root it already exports — omp's does), and the
    host that cannot is Claude Code.
    """
    name = (explicit or "").strip()
    if name and name != "auto":
        return name
    declared = (os.environ.get("NEURONZAI_HOST") or "").strip()
    # Unknown value: fall through rather than fail. _load_host raises on a name it
    # does not know, and a stale export must not break a switch that would work.
    if declared in ("claude-code", "oh-my-pi"):
        return declared
    return "claude-code"


def _dispatch(host, handler_name, event, args):
    """Route to a core HANDLER (not just an event — several handlers can share an
    event, e.g. SessionEnd runs ledger flush + detached capture + run recording).
    Each handler returns an Output|None. Adding a host or a handler never touches
    core's branching rules — only this table + a hosts/ file. An unknown/not-yet-
    ported handler no-ops (fail-open)."""
    if handler_name == "guidance":
        from core import guidance
        return guidance.handle(event, host, chunk=args.chunk)
    if handler_name == "host_requirements":
        from core import requirements
        return requirements.handle(event, host)
    if handler_name == "recall":
        from core import recall
        return recall.handle(event, host)
    if handler_name == "gate":
        from core import gate
        return gate.handle(event, host)
    if handler_name == "capture":
        from core import capture
        return capture.handle(event, host)
    if handler_name == "selfsweep":
        from core import selfsweep
        return selfsweep.handle(event, host)
    if handler_name == "sweep":
        from core import sweep
        return sweep.handle(event, host)
    if handler_name == "skills_sync":
        from core import assets
        return assets.handle(event, host)
    if handler_name == "topic_mode_sync":
        from core import topic_mode
        return topic_mode.handle(event, host)
    if handler_name == "skills_usage":
        from core import usage
        return usage.handle(event, host)
    if handler_name == "runs_record":
        from core import runs
        return runs.handle(event, host)
    if handler_name == "topic_exit":
        from core import topic_exit
        return topic_exit.handle(event, host)
    if handler_name == "switch":
        from core import switch
        return switch.handle(event, host)
    if handler_name == "reload":
        from core import reload as reload_handler
        return reload_handler.handle(event, host)
    if handler_name == "detached_capture":
        from core import detached
        return detached.handle(event, host)
    # The detached child re-enters HERE, in its own process session, with the same
    # hook payload on stdin. Same dispatcher, same adapters — the only difference is
    # that nothing is waiting for it.
    if handler_name == "capture_run":
        from core import detached
        return detached.run(event, host)
    return None


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", required=True)
    parser.add_argument("--event", required=True)      # canonical event (parse/emit)
    parser.add_argument("--handler", required=True)    # which core handler to run
    parser.add_argument("--chunk", type=int, default=1)
    args, _ = parser.parse_known_args()

    # Before the first blocking read: stdin itself can hang on a detached spawn.
    _arm_deadline()

    raw = sys.stdin.read()
    host = _load_host(resolve_host_name(args.host))
    event = host.parse_event(args.event, raw)
    output = _dispatch(host, args.handler, event, args)
    # #494 — size the exact context about to cross the host boundary, and when it
    # exceeds the host's MEASURED inline limit, co-emit a user-facing systemMessage
    # (never model context) so a silent spill becomes loud. Reporting is isolated
    # from emission twice: report() is fail-open, and this guard ensures even an
    # import or implementation defect cannot suppress otherwise valid hook output.
    try:
        from core import envelope
        sized = envelope.report(args.event, output, host, event)
        if sized is not None and sized.get("over") and output is not None:
            notice = envelope.over_limit_notice(sized, host)
            existing = getattr(output, "system_message", None)
            output.system_message = "\n\n".join(p for p in (existing, notice) if p)
    except Exception:
        pass
    host.emit(args.event, output)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # best-effort: never block a session on a hook failure
        pass
    sys.exit(0)
