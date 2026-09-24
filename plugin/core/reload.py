#!/usr/bin/env python3
# PreToolUse handler — the /reload-assets command (#530).
#
# Assets are materialized ONCE, at session start. Three ordinary situations leave a
# running session holding the wrong set, and none of them can wait for the next one:
# a /switch-profile re-scopes the session while its links still point at the old
# profile's assets; a concurrent session in another repo sweeps a set this session
# was using (a home-directory session on a host whose project dir IS its global dir
# cannot be isolated at all); and an asset edited in the dashboard, or a store
# corrupted by hand, is invisible until a restart.
#
# WHY A HOOK, not just the CLI. The command tells the model to run a small helper.
# The helper can materialize — but only a hook RESULT can carry the host's in-session
# rescan directive, and on the host that has no session-id substitution the session
# id arrives on the hook payload only. So this interceptor does the work and blocks
# the helper (which would otherwise redo it), exactly as core/switch does.
#
# WHAT IT PROMISES is computed from capabilities, never asserted: a host re-scans the
# kinds in caps.live_reload_asset_kinds and nothing else, and what the USER can still
# do about the rest is caps.manual_reload_hint. Both are host-owned; this module
# names no host. A reload that says "done" while the session keeps the old set is
# worse than one that says "written to disk, restart to see it" — the first sends
# someone hunting for a command that is not there.
#
# Pure stdlib.

from core import assets, cmdline
from core.hostapi import Event, Host, Output

# The command-side helper the /reload-assets command tells the model to run.
HELPER = "reload_assets.py"

# The command whose hook CAN carry the rescan directive. A message printed to stdout
# cannot, so it names this instead of promising what only a hook result delivers.
RELOAD_COMMAND = "/reload-assets"

# Kind -> the word a user recognizes. Ordered: the report lists kinds in THIS order
# so two hosts' messages read the same way.
KIND_WORDS = (("skill", "skills"), ("command", "commands"),
              ("agent", "subagents"), ("output_style", "output styles"))


def _kind_list(kinds):
    """'skills, commands and subagents' for a set of kind literals, in a fixed order.
    Empty -> ''."""
    words = [word for kind, word in KIND_WORDS if kind in kinds]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


# The CAUSE clause, per assets.REASON_*. Reached only for the two fail-open disk
# outcomes ('store' / 'empty'), which #536 is entirely about: they used to render one
# sentence for three causes, and that sentence blamed the network even when the
# server had answered. Each clause has to read correctly in BOTH templates below, so
# it is a bare statement with no trailing punctuation.
CAUSES = {
    assets.REASON_UNREACHABLE: "Neuronz.ai is unreachable",
    assets.REASON_SERVER_ERROR: "Neuronz.ai answered with an error instead of the asset list",
    assets.REASON_BAD_PAYLOAD: "Neuronz.ai sent back an asset list that could not be read",
    assets.REASON_UNCHANGED: "Neuronz.ai has nothing newer",
}
# Said when a reason arrives with no clause of its own. Unreachable in shipped code
# (the gate pins CAUSES against assets.REASONS), and deliberately claims nothing
# about WHY — a wrong cause is worse than no cause.
NO_CAUSE = "Neuronz.ai did not hand over an asset list"


def outcome_sentence(result):
    """What the materialization DID, in the user's terms. `result` is an
    assets.Materialized (or None when the reconcile could not even run).

    Two things are being said, and they come from two different fields: what happened
    on disk is `source`, and WHY is `reason`. One disk outcome carries several causes
    — a server that never answered, a server that answered an error, and a server
    that answered garbage all leave the same links behind — and the user chases the
    cause, not the disk (#536)."""
    if result is None:
        return "Assets could not be reconciled."
    where = f'profile "{result.profile}"' if result.profile else "this session's profile"
    if result.source == "server":
        return f"Re-fetched {result.count} asset(s) for {where} and rewrote them on disk."
    if result.source == "purged":
        return ("Neuronz.ai rejected this machine's token, so every synced asset was "
                "removed. Run the login workflow.")
    cause = CAUSES.get(result.reason, NO_CAUSE)
    if result.source == "store":
        return f"{cause} — kept the {result.count} asset(s) already on disk for {where}."
    return (f"{cause} and nothing was cached for {where}, so its assets were removed "
            "rather than left pointing at another profile's set.")


def rescan_sentence(host: Host, live=None, recover: str = ""):
    """What THIS MESSAGE gets re-scanned, from the host's own capabilities.

    `live` is the set of kinds this message can have re-scanned; None means the
    host's own capability. Only a hook RESULT carries the host's rescan directive, so
    a hook passes None and a CLI writing to stdout passes an empty set — a message
    that cannot ask for a rescan must not report one.

    `recover` names a command that CAN carry the directive, for the kinds the host
    re-scans live but this message could not. Empty = do not offer it (the reload
    helper only ever runs when that interceptor did NOT fire, so pointing back at it
    would send the user in a circle)."""
    rescanned = set(host.caps.live_reload_asset_kinds) if live is None else set(live)
    recoverable = set(host.caps.live_reload_asset_kinds) - rescanned if recover else set()
    live_words = _kind_list(rescanned)
    stale = _kind_list(set(host.caps.supported_asset_kinds) - rescanned - recoverable)
    hint = (host.caps.manual_reload_hint or "").strip()

    if live_words and not stale:
        return "This session re-scans them now."
    if live_words:
        parts = [f"This session re-scans {live_words} now; {stale} need a newly started session"]
    elif recoverable:
        clause = (f"This session has not re-scanned them — run {recover} to pick "
                  f"{_kind_list(recoverable)} up in place")
        if stale:
            clause += f", and {stale} need a newly started session"
        parts = [clause]
    elif hint:
        return (f"This session does not re-scan assets on its own — run {hint} to pick "
                "up the new set without losing this conversation.")
    else:
        parts = [f"This session does not re-scan assets: {stale or 'they'} need a newly "
                 "started session"]
    if hint:
        parts.append(f"or run {hint} in this session")
    return " ".join(parts) + "."


def report(host: Host, result, live=None, recover: str = ""):
    return (f"reload-assets: {outcome_sentence(result)} "
            f"{rescan_sentence(host, live=live, recover=recover)}")


def cli_report(host: Host, result, offer_reload: bool = False):
    """The report for a message PRINTED to stdout, which carries no rescan directive.

    Same outcome sentence, but every kind the host can re-scan live is reported as
    NOT re-scanned, because this message cannot ask for it. `offer_reload` names the
    one way back into an in-place rescan for those kinds; the reload helper itself
    passes False (see `recover` in rescan_sentence)."""
    return report(host, result, live=frozenset(),
                  recover=RELOAD_COMMAND if offer_reload else "")


def handle(event: Event, host: Host):
    tool_input = event.tool_input or {}
    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    if cmdline.helper_invocation(command, HELPER) is None:
        return None  # not a reload-assets invocation — let the tool run

    # force=True: the point of a user-invoked reload is that the LOCAL state is in
    # doubt, and a conditional request answered 304 heals from the same store the
    # user is asking us to rebuild.
    result = assets.materialize(event, host, force=True)
    message = report(host, result)
    # Blocked, not run: the helper would repeat the whole reconcile for nothing. The
    # message goes to the model (context/deny_reason) AND the user (system_message) —
    # the model is the one that has to stop offering a skill that needs a restart.
    return Output(deny=True, deny_reason=message, system_message=message, context=message,
                  reload_assets=bool(host.caps.live_reload_asset_kinds))
