#!/usr/bin/env python3
"""Surface host requirements before relying on host lifecycle events.

Two things are checked at SessionStart and folded into ONE system_message:
  1. the host's own version/lifecycle notice (host.requirements_notice()); and
  2. the SESSION MODEL — a host that reports its model at session start
     (caps.reports_model) but supplied none THIS session cannot scope model-aware
     rules, so say so. A host that structurally does not report a model at session
     start (caps.reports_model False) is silent here: it is not a regression, and
     for such a host model-scoped rules may still ride on a later lane that does
     carry the model (e.g. a per-prompt recall lane).

This fires once per session (SessionStart), never per turn — the same gating the
version notice already uses.
"""

from .hostapi import Output

# Kept close to the version-notice wording: a plain, visible ⚠ line naming the
# consequence, not a stack trace.
MODEL_NOT_REPORTED_NOTICE = (
    "⚠ Neuronz.ai: this host did not report the session model, so model-scoped "
    "rules and registers will not ride this session. If this is unexpected, the host "
    "may have stopped sending the model on its hook payload — start a fresh session."
)


def handle(event, host):
    parts = []
    notice = host.requirements_notice()
    if notice:
        parts.append(notice)
    # caps/name may be absent on a minimal test double — treat missing as "does not
    # report a model", which keeps such a host silent here.
    caps = getattr(host, "caps", None)
    if getattr(caps, "reports_model", False) and not (getattr(event, "model", None) or ""):
        parts.append(MODEL_NOT_REPORTED_NOTICE)
    return Output(system_message="\n".join(parts)) if parts else None
