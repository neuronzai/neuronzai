#!/usr/bin/env python3
# Shared HTTP client + env for the neuronzai hooks (host-agnostic).
#
# Every hook talks to the same JSON API over the same auth; this centralizes the
# base URL, OAuth/env credential resolution, header building, and best-effort HTTP so a
# handler is a few lines. Pure stdlib. NEVER raises to the caller on a network
# error — a down server must never block a session (fail-open); auth failures
# print to stderr (which does NOT pollute additionalContext) so a missing login
# is visible instead of silently disabling memory.

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from core import auth

BASE_URL = auth.base_url()
# Explicit profile (sent as X-Profile) wins; else the server resolves it from cwd.
ENV_PROFILE = (os.environ.get("NEURONZAI_PROFILE") or "").strip()
SWEEP_PROTOCOL_HEADER = "X-Neuronzai-Sweep-Protocol"
SWEEP_PROTOCOL_VERSION = "2"


def headers(profile="", session_id="", extra=None, force_refresh=False):
    h = dict(extra or {})
    token = auth.access_token(force_refresh=force_refresh)
    if token:
        h["Authorization"] = f"Bearer {token}"
    if profile:
        h["X-Profile"] = profile
    if session_id:
        h["X-Session-Id"] = session_id
    return h


def _note_auth(where, code):
    print(
        f"[{where}] auth failed ({code}) — run the Neuronz.ai login workflow; skipped",
        file=sys.stderr,
    )


def _open_json(request, timeout, where, retry_factory):
    """Open once, refresh an OAuth grant on 401, then retry once."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        if error.code == 401 and not auth.environment_token():
            refreshed = auth.access_token(force_refresh=True)
            if refreshed:
                try:
                    with urllib.request.urlopen(retry_factory(), timeout=timeout) as response:
                        raw = response.read()
                        return json.loads(raw) if raw else {}
                except urllib.error.HTTPError as retry_error:
                    error = retry_error
                except Exception:
                    return None
        if error.code in (401, 403):
            _note_auth(where, error.code)
        return None
    except Exception:
        return None


def get_json(path, params=None, profile="", session_id="", timeout=4, where="neuronzai",
             extra_headers=None):
    """GET {BASE_URL}{path}?{params} → parsed JSON, or None on any failure."""
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{BASE_URL}{path}{qs}"
    make_request = lambda force=False: urllib.request.Request(
        url, headers=headers(profile, session_id, extra_headers, force_refresh=force), method="GET"
    )
    return _open_json(make_request(), timeout, where, lambda: make_request())


def post(path, params=None, body=None, profile="", session_id="", timeout=4, where="neuronzai",
         extra_headers=None):
    """POST (best-effort). Returns parsed JSON on success, else None. Never raises."""
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    data = json.dumps(body).encode() if body is not None else b""
    extra = dict(extra_headers or {})
    if body is not None:
        extra.setdefault("Content-Type", "application/json")
    url = f"{BASE_URL}{path}{qs}"
    make_request = lambda force=False: urllib.request.Request(
        url,
        headers=headers(profile, session_id, extra or None, force_refresh=force),
        method="POST",
        data=data,
    )
    return _open_json(make_request(), timeout, where, lambda: make_request())
