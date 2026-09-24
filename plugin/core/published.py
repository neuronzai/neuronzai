#!/usr/bin/env python3
"""The cross-agent publish ledger.

Materializing is now per-repo, so a session's assets land where only that repo's
sessions read them. That fixes what we publish NEXT; it does nothing about what is
ALREADY sitting in a shared directory, and each agent only sweeps the directories it
declares itself. A set published into one agent's config home therefore survives
until you happen to run THAT agent with THAT config home again — and until then it is
read by every session of it, whatever repo they are in, for whatever profile.

The ledger closes that: every reconcile records the links it placed, keyed by the
publisher (the agent plus the store it published from) and the target it published
into, so ANY later session can find a foreign publication and remove it. A list of
foreign directories could not do this — it would have to know about config homes and
retired profiles nobody runs any more, which is exactly the state that leaks.

Only GLOBAL publications are swept. A repo-local one leaks nowhere: the only sessions
that read it are the ones in that repo, which is the scope the assets were resolved
for. Sweeping those would delete the links another repo's session is using right now.
"""

import hashlib
import json
import os
import time
from pathlib import Path

LEDGER_DIR_NAME = "published"

# What a publication can be, and the only distinction the sweep acts on.
SCOPE_GLOBAL = "global"  # a config-home dir: read by every session of that agent
SCOPE_REPO = "repo"      # a project dir: read only inside that repository


def ledger_dir():
    base = os.environ.get("NEURONZAI_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".neuronzai"
    )
    return Path(base) / LEDGER_DIR_NAME


def _safe(name):
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(name or ""))[:48]


def _digest(*parts):
    raw = "\0".join(str(p or "") for p in parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def ledger_path(host_name, store_root, target):
    """One file per (agent, store, target). The store is in the key because one agent
    run against two config homes is two independent publishers, and the target is in
    it because one publisher legitimately holds one publication per repo at once —
    keying on the publisher alone would make each repo's session erase the record of
    the last, and an unrecorded link is one nothing can ever sweep."""
    return ledger_dir() / "{}-{}.json".format(
        _safe(host_name), _digest(store_root, target))


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    links = entry.get("links")
    entry["links"] = [str(link) for link in links if str(link)] if isinstance(links, list) else []
    return entry


def _write(path, entry):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(path))
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _drop(path):
    try:
        path.unlink()
    except OSError:
        pass


def record(host_name, store_root, target, profile, profile_key, links, scope):
    """Record (or clear) ONE publication. An empty link set removes the file rather
    than leaving an entry that claims a publication which no longer exists."""
    path = ledger_path(host_name, store_root, target)
    links = [str(link) for link in links if str(link)]
    if not links:
        _drop(path)
        return path
    _write(path, {
        "host": str(host_name or ""),
        "storeRoot": str(store_root or ""),
        "target": str(target or ""),
        "scope": scope,
        "profile": str(profile or ""),
        "profileKey": str(profile_key or ""),
        "links": sorted(dict.fromkeys(links)),
        "updatedAt": int(time.time()),
    })
    return path


def entries():
    """Every readable ledger entry, as (path, entry). Unreadable/garbage files are
    skipped rather than repaired: the ledger is a cache of what we did, and a session
    that cannot read it must still materialize."""
    found = []
    try:
        names = sorted(os.listdir(ledger_dir()))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        path = ledger_dir() / name
        entry = _read(path)
        if entry is not None:
            found.append((path, entry))
    return found


def sweep_foreign(profile_key, is_owned, remove, keep=()):
    """Remove every GLOBAL publication belonging to a different profile.

    `is_owned(path)` re-confirms the path is still one of our symlinks at sweep time —
    the ledger says what we wrote, never what is there now, and a user who replaced a
    link with their own file keeps it. `remove(path)` unlinks. `keep` is this
    session's own freshly installed links: a session whose target dir IS the config
    home (a home-directory session) publishes into the very place a foreign entry
    names, and the ledger must never be the reason we delete what we just installed.

    Returns the list of removed paths.
    """
    mine = {os.path.realpath(str(path)) for path in keep}
    removed = []
    for path, entry in entries():
        if entry.get("scope") != SCOPE_GLOBAL:
            continue
        if str(entry.get("profileKey") or "") == str(profile_key or ""):
            continue
        survivors = []
        for link in entry["links"]:
            if os.path.realpath(link) in mine:
                survivors.append(link)  # this session just installed it; not foreign
                continue
            if not os.path.islink(link):
                continue  # already gone, or replaced by a real file: forget the record
            if not is_owned(link):
                survivors.append(link)  # someone else's link wearing our name: leave it
                continue
            if remove(link):
                removed.append(link)
            else:
                survivors.append(link)
        if survivors == entry["links"]:
            continue
        if survivors:
            entry["links"] = survivors
            _write(path, entry)
        else:
            _drop(path)
    return removed
