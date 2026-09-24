#!/usr/bin/env python3
# SessionStart handler (host-agnostic) — bootstrap persistent memory for the
# session's profile. This is the single lifecycle implementation dispatched by
# entry.py for every host.
#
# GET /api/memory/bootstrap?chunk=N&cwd=… returns SERVER-RENDERED context chunks
# injected as additionalContext, so the session opens already oriented in its
# profile. This handler is opaque to what is in them: a chunk may carry the rules
# themselves or, since #512, a POINTER the agent has to pull — the server decides,
# and nothing here parses, reformats or claims anything about the text it injects.
# Chunked (#129) because a host may persist large hook output to a side file and
# inject only a preview: the server renders small budgeted chunks and the wire
# config fires this handler once per chunk.
#
# Compaction is handled in TWO independent halves, and they were conflated until
# #512. INVALIDATION (POST /compacted — see core/compaction) fires on EVERY host
# that reports a completed compaction, because the server's per-session delivery
# state is stale for all of them: the session id survives a compaction, so the
# rules pointer's fetched-state, the gate cadence and the recall seen-map all
# describe context that no longer exists. INJECTION is the capability question:
# a host that DROPS SessionStart additionalContext on a compact-sourced fire
# (caps.drops_sessionstart_context_on_compact) has nowhere to put the chunk and
# exits silent, leaving the next per-prompt recall to re-seed; a host that KEEPS
# it injects normally, below. Before #512 the invalidation rode inside the
# dropping-host branch, so every host that keeps compact context — the majority —
# compacted with its delivery history untouched and was re-served pointers to
# rules it had just lost.

import os

from core import api, auth, compaction, profile as profile_mod
from core.hostapi import Event, Host, Output

SIGNED_OUT_PREFIX = (
    "⚠ Neuronz.ai is not signed in — persistent memory (recall, rules, sweep) is "
    "off for this session."
)

# A bootstrap that FAILS must say so (#14). api.get_json returns None on any
# failure — HTTP 500, a >4s timeout, a malformed body — and previously this
# handler returned silently on that, which is indistinguishable from "this
# profile has no rules". For a memory product that is the worst failure mode: the
# agent runs the whole session unaware it is flying blind, and the user only finds
# out when a rule is violated. An EMPTY profile is different and stays silent —
# get_json returns a body (not None) when the server answers with nothing to say.
DELIVERY_FAILED_MESSAGE = (
    "⚠ Neuronz.ai memory did NOT load for this session — the bootstrap request failed "
    "(server error, timeout, or bad response). Your rules, repo brief and standing "
    "context are missing, so an empty memory here means UNKNOWN, not \"no rules\". "
    "Treat anything you would normally check against stored rules as unverified, and "
    "start a new session once the server is reachable."
)

# A session running under a DIFFERENT directory than the one the user is standing
# in must say so (#527). A host that restores a saved session pins its working
# directory to the one the session was BORN in, and that pinned directory — not
# the shell's — is what resolves the profile here and what the asset materializer
# reconciled on disk. The failure is silent and total: the wrong profile answers
# every recall, the wrong repo's skills and commands are the ones that exist, and
# nothing in the transcript says which directory won. Only a host that can observe
# BOTH directories fills Event.launch_cwd; the rest leave it "" and this stays
# quiet. The notice states the fact and the only reliable fix — a session's
# directory cannot be changed after the fact.
CWD_PIN_NOTICE_TEMPLATE = (
    "⚠ Neuronz.ai: this session is running in {session}, but you launched it from "
    "{launched}. A resumed session keeps the directory it was created in, so the "
    "profile answering recall and the skills/commands on disk are {session}'s — not "
    "this directory's. Start a NEW session here to work in it."
)


def cwd_pin_notice(event: Event) -> "str | None":
    """The #527 notice, or None when there is nothing to report. Both directories
    are realpath'd before comparison so a symlinked checkout (or a trailing slash)
    is not reported as a mismatch."""
    def canonical(path: str) -> str:
        path = (path or "").strip()
        return os.path.realpath(os.path.expanduser(path)) if path else ""

    session, launched = canonical(event.cwd), canonical(event.launch_cwd)
    if not session or not launched or session == launched:
        return None
    return CWD_PIN_NOTICE_TEMPLATE.format(session=session, launched=launched)


def signed_out_message(host: Host) -> str:
    """User-visible no-credential notice with this host's native login syntax."""
    return f"{SIGNED_OUT_PREFIX} Run {host.login_hint} to turn it on."


def _notices(*parts) -> str:
    """Join the notices this chunk owes the user, skipping the absent ones."""
    return "\n\n".join(part for part in parts if part)


def handle(event: Event, host: Host, chunk: int = 1):
    """Return an Output (inject the chunk) or None (silent). `chunk` selects which
    server-rendered bootstrap chunk this invocation fetches."""
    if (event.source or "").strip() == "compact":
        # A COMPLETED compaction (this fire is the host's report that one happened).
        # Invalidate the session's delivery history on chunk 1 only, so it fires once
        # even though the wire config runs this handler once per chunk.
        if chunk == 1:
            compaction.invalidate(event)
        if host.caps.drops_sessionstart_context_on_compact:
            # Nothing this handler emits can reach the model on this host's
            # compact-sourced fire, so emit nothing rather than pay for a chunk the
            # host will discard. The invalidation above is what restores the rules:
            # the next per-prompt recall re-delivers them.
            return None

    prof, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)

    # The wrong-directory notice (#527), chunk 1 only so one session start cannot
    # produce two copies. It rides ALONGSIDE whatever this chunk was going to say
    # rather than replacing it — the session is still bootstrapping, just from a
    # directory the user is not standing in, and a notice that swallowed the
    # signed-out or delivery-failed message would trade one silent failure for
    # another.
    pin = cwd_pin_notice(event) if chunk == 1 else None

    # Visible sign-in notice (chunk 1 only, so it fires once even though the wire
    # config runs this handler once per chunk). No saved OAuth grant AND no legacy
    # env token → memory is off for the whole session; surface a system_message
    # instead of letting it fail quietly to stderr. auth.status() is network-free
    # and definitive, so a transient outage never triggers this (fail-open) — only
    # a real missing login.
    if chunk == 1 and not auth.status().get("authenticated"):
        return Output(system_message=_notices(signed_out_message(host), pin))

    params = {"chunk": str(chunk)}
    if cwd:
        params["cwd"] = cwd
    # #18: the repo's CURRENT head, so the server can tell a repo brief that is
    # merely OLD from one the code has actually moved under. Read off event.cwd,
    # not the resolved `cwd`, because a /switch-profile session deliberately sends
    # no cwd — this value resolves nothing and is never persisted, so unlike the
    # cwd it can create no profile route or anchor. "" whenever there is no git
    # repo, which just degrades the hint to age-only.
    head = profile_mod.head_commit(event.cwd)
    if head:
        params["head"] = head
    # The SESSION model this session is running under, so the bootstrap can scope
    # model-aware rules from the first turn — same omit-when-absent contract as the
    # other observables. The adapter already lower-cased the host's own id; no
    # provider prefix is added or stripped. None → param omitted.
    if event.model:
        params["model"] = event.model
    boot = api.get_json("/api/memory/bootstrap", params=params, profile=prof,
                        session_id=event.session_id, where="memory_guidance")
    if boot is None:
        # Delivery FAILED (not "nothing to deliver") — surface it once, on chunk 1,
        # so the two cases are distinguishable to the user. Chunk 2 stays silent so
        # a single outage cannot produce two warnings.
        return Output(system_message=_notices(DELIVERY_FAILED_MESSAGE, pin)) if chunk == 1 else None
    # No default profile: a successful response always names one; bail otherwise.
    # The pin notice still goes out: "this profile has nothing to say" is exactly
    # the shape a wrong-directory session takes when the pinned repo is empty.
    if not (boot.get("profile") or "").strip():
        return Output(system_message=pin) if pin else None
    chunks = boot.get("chunks") or []
    if not chunks or not (chunks[0] or "").strip():
        return Output(system_message=pin) if pin else None
    return Output(context=chunks[0], system_message=pin)
