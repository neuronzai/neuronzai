#!/usr/bin/env python3
# SessionStart handler (host-agnostic) — materialize the active profile's ASSETS
# from the server into the matching host config-dir subdirectories, so a profile
# can ship host assets (skills, slash commands, subagents) that
# appear automatically in sessions started inside that profile. This is the
# single lifecycle implementation dispatched by entry.py for every host.
#
# Calls GET /api/assets/materialize (profile resolved EXACTLY as the SessionStart
# bootstrap handler does: X-Profile when NEURONZAI_PROFILE / a /switch-profile
# override is set, else the session cwd as ?cwd=). The endpoint returns
#   { "profile", "hash", "assets": [ { "kind", "slug", "name", "body",
#                                       "files": [ {path, content} ] } ] }
# and a strong `ETag: "<hash>"` (a pure function of the RESOLVED PROFILE NAME plus
# that profile's active (kind, slug, contentHash) rows). The profile is part of the
# digest, so a cwd whose profile_route was REMAPPED can never get a 304 against the
# previous profile's ETag even when the two profiles' asset sets are byte-identical
# — the remap always falls through to a full 200 and re-keys the store below.
#
# CROSS-PROFILE CORRECTNESS (the load-bearing invariant)
# ------------------------------------------------------
# The target dirs (the config-dir subdirs) are GLOBAL — one shared set per config
# dir, NOT per profile. So entering a session under profile P must leave those dirs
# holding EXACTLY P's active set, regardless of what a different profile's session
# left behind. We RECONCILE the shared dirs on EVERY session start — including a
# 304 — not just on a full 200. The ETag only saves the body REFETCH; it NEVER
# skips the local symlink reconcile. Concretely:
#   * PER-PROFILE STORE. Each asset is written under a per-profile, collision-free
#     subdir  <store_root>/<profileKey>/<kindDir>/<slug>[/SKILL.md]  where
#     profileKey = profile_key(name) is an INJECTIVE function of the raw profile
#     name (sha256-backed), so two profiles can never share a store entry even if
#     their names look alike. Symlinks in the shared target dir point INTO it.
#   * RICHER CACHE. The per-(profile-or-cwd) cache file is JSON
#     { etag, profile, profileKey, manifest } where manifest lists every
#     {kind, slug[, files]} this profile materialized — enough to rebuild the
#     symlinks from the persisted store WITHOUT a refetch on a 304. Healing on a
#     304 is only sound because the ETag is profile-seeded server-side: a 304
#     therefore means "same profile, same set", so the cached profileKey the heal
#     reconciles from is guaranteed to be the profile this session resolved.
#   * RECONCILE = install THIS profile's links first (idempotent), then PRUNE last
#     every owned symlink not in this profile's active set — removing this profile's
#     deleted assets AND any OTHER profile's leftover links (no cross-profile bleed).
#     Install-first/prune-last means a hook killed at the timeout errs toward "this
#     profile present" rather than "this profile gone".
#   * NO-USABLE-CACHE FAIL: server unreachable AND no healable cache (so we cannot
#     identify/rebuild this profile's set) → OFFBOARD to empty (remove all our
#     symlinks) rather than leave a previous profile bleeding.
#
# Storage model: we ONLY ever create/replace/remove symlinks that resolve INTO our
# store (the neuronzai-assets root OR the legacy neuronzai-skills root, so an
# upgrade self-heals old links) — the user's own real assets and foreign symlinks
# are never touched. Ownership is decided on the RAW (unresolved) link target so a
# DANGLING link into our store is still recognised as ours and is removable.
#
# Fail-open: network error/timeout (4s) heals from the cached store when possible,
# else offboards, returns None. A 401/403 fail-CLOSES (remove ALL our symlinks +
# clear the cache) ONLY when the unauthenticated /healthz liveness probe confirms
# the server is actually UP — a genuinely revoked/missing token. When /healthz is
# unreachable or non-200 (a deploy window), a 401/403 is treated like any outage:
# heal from the store (KEEP the links + cache), return None — so an in-flight
# SessionStart (compaction / resume / plugin reload) that merely RACED a deploy
# can't wipe every synced asset.
#
# IN-SESSION RELOAD: like the legacy hook, after the on-disk reconcile this handler
# asks the host to re-scan the user asset (skill) dirs in-session — expressed as
# `Output(reload_assets=True)` from handle(), which a host that supports in-session
# reload renders as its own re-scan directive; a host without one simply ignores the
# flag. So freshly-synced skills hot-reload where the
# host allows it, not only on the next session. The filesystem result (store,
# symlinks, cache) is identical to the legacy hook.
#
# Pure stdlib Python 3 (urllib/json/os/sys/pathlib/hashlib) — clients need no Bun
# install. Reuses core/api.py (BASE_URL, OAuth/env bearer via api.headers, ENV_PROFILE) +
# core/profile.py (resolve).

import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from core import api, profile as profile_mod, published
from core.hostapi import Event, Host, Output  # noqa: F401 (Output kept for signature parity)

KIND_DIRS = {
    "skill": "skills",
    "command": "commands",
    "agent": "agents",
}

# Dirs of a kind the server no longer serves (#506: `output_style`). Earlier builds
# linked into them, so every reconcile/offboard still sweeps our own links out of
# the matching sibling of each target dir; the user's own files there are untouched.
RETIRED_KIND_DIRS = ("output-styles",)

# The directory name of an asset STORE under some host config home. Ownership of a
# symlink is decided on these names alone (see _Store.owned_symlink_target), so one
# installation recognises — and may replace — a link another installation wrote.
STORE_NAME = "neuronzai-assets"
LEGACY_STORE_NAME = "neuronzai-skills"  # pre-rename skills store (still ours)
STORE_DIR_NAMES = (STORE_NAME, LEGACY_STORE_NAME)

# A cold cache: every field absent so healable() is False and we force a full 200.
COLD_CACHE = {"etag": "", "profile": None, "profileKey": None, "manifest": None}


# WHY a pass ended where it did, kept APART from `source` (what it DID on disk)
# because one disk outcome has several causes, and a user who is told the wrong one
# goes looking in the wrong place: "unreachable" sends someone to their network and
# their NEURONZAI_URL when the server answered, badly (#536).
#
# CLOSED set — core/reload.outcome_sentence switches on it exhaustively, and the
# reload gate pins that every member here has a sentence there.
REASON_FETCHED = "fetched"                # a 200 that parsed: the set came from the server
REASON_UNCHANGED = "unchanged"            # a 304: the server confirmed we hold this set
REASON_UNREACHABLE = "unreachable"        # nothing answered: network down or timeout
REASON_SERVER_ERROR = "server_error"      # reached; it answered an error of its own
REASON_BAD_PAYLOAD = "bad_payload"        # reached; a 200 whose body is not a catalog
REASON_TOKEN_REJECTED = "token_rejected"  # reached, up, and refusing this token
REASONS = frozenset({REASON_FETCHED, REASON_UNCHANGED, REASON_UNREACHABLE,
                     REASON_SERVER_ERROR, REASON_BAD_PAYLOAD, REASON_TOKEN_REJECTED})


class Materialized:
    """What ONE materialization pass actually did, for a caller that has to TELL the
    user (core/reload). The lifecycle hooks ignore it: they are silent and their
    fail-open paths are deliberately indistinguishable from success to the session.
    A user who typed a reload is owed the difference.

        source  'server' — a 200: every store entry rewritten from the response
                'store'  — a 304 or an outage: healed from what was already on disk
                'empty'  — an outage with nothing healable: links removed, not stale
                'purged' — the server is up and rejected our token: fail-closed
        profile the profile the set belongs to ('' when nothing resolved)
        count   how many assets that set holds
        reason  one of REASONS — the CAUSE. 'store' and 'empty' each carry three of
                them, and only one of the three is an unreachable server.
    """

    __slots__ = ("source", "profile", "count", "reason")

    def __init__(self, source, profile, count, reason):
        self.source = source
        self.profile = (profile or "").strip()
        self.count = int(count or 0)
        self.reason = reason


def _cached_count(cache):
    manifest = cache.get("manifest")
    return len(manifest) if isinstance(manifest, list) else 0


# ---- pure helpers (no host paths) -------------------------------------------
def sanitize(text):
    # Filesystem-safe rendering of an arbitrary string. LOSSY (distinct inputs can
    # collapse) — only ever used as a human-readable PREFIX, never as the identity.
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (text or ""))


def profile_key(profile):
    # INJECTIVE, filesystem-safe identity for a profile. The sha256 suffix is the
    # identity (distinct raw names never collide); the sanitized prefix is purely
    # for human debuggability of the on-disk store dir.
    digest = hashlib.sha256((profile or "").encode("utf-8")).hexdigest()[:16]
    prefix = sanitize((profile or "").strip())[:24].strip("_") or "p"
    return f"{prefix}-{digest}"


def is_unsafe_rel(rel):
    # Defensive: the server already guarantees safe relative paths, but never let a
    # file escape its store dir. Reject absolute paths and any `..` segment.
    if not rel or rel.startswith("/") or rel.startswith("\\"):
        return True
    parts = rel.replace("\\", "/").split("/")
    return any(part == ".." for part in parts)


def read_cache(path):
    # Tolerant reader: ANY legacy bare-string etag, partial dict, or unparseable
    # file degrades to a COLD cache (forces a full 200 + the flat->nested store
    # migration), never to a populated-but-wrong cache that could drive a 304.
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return dict(COLD_CACHE)
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return dict(COLD_CACHE)
    if not isinstance(obj, dict):
        return dict(COLD_CACHE)
    if not all(k in obj for k in ("etag", "profile", "profileKey", "manifest")):
        return dict(COLD_CACHE)
    if not isinstance(obj.get("manifest"), list):
        return dict(COLD_CACHE)
    return obj


def clear_cache(path):
    try:
        path.unlink()
    except OSError:
        pass


def write_no_follow(dest, content):
    # Write a REAL file at `dest`, never THROUGH a symlink (a pre-placed symlink
    # would redirect the write outside the store — a TOCTOU gap the containment
    # check can't close alone). Remove any existing symlink first.
    if dest.is_symlink():
        dest.unlink()
    dest.write_text(content, encoding="utf-8")


# ---- repo-local git ignore (project-scoped hosts only) ----------------------
# Materializing into <cwd>/<base> puts our symlinks inside the user's repository,
# where they would show up as untracked files in every `git status` and could be
# committed by a careless `git add -A`. We register them in the repo's OWN ignore
# file, .git/info/exclude: it is never committed (so no diff appears in the repo),
# it is per-clone (so we never touch a shared .gitignore the team owns), and it has
# NO effect on files git already tracks — a project dir holding checked-in config
# keeps behaving exactly as before.
# The block is SCOPED to one materialization target, named in its own markers, and
# only the block carrying THIS target's name is ever rewritten. One repository
# legitimately holds several at once — another harness publishing into its own
# project dir, a second worktree sharing this common git dir, a session started in a
# subdirectory — and a single global block would mean the last session to run
# un-ignores every other one's links while they are still on disk.
GIT_EXCLUDE_BEGIN_PREFIX = "# BEGIN neuronzai assets: "
GIT_EXCLUDE_END_PREFIX = "# END neuronzai assets: "


def git_exclude_context(cwd):
    """`(worktree_root, exclude_file)` for a session at `cwd`, or None outside a
    repository. Both halves are needed and they are NOT the same place: ignore
    patterns are anchored at the WORKTREE ROOT (a session can start in a
    subdirectory, so we walk up to find it), while git reads the exclude FILE from
    the repo's COMMON git dir. `.git` is a directory in a normal clone and a
    `gitdir:` pointer file in a linked worktree or submodule, whose own git dir
    carries a `commondir` pointing back at the main checkout's.

    The cwd is expanded first, exactly as the hosts expand it when they derive the
    target base from it: the links are placed under the EXPANDED path, so looking for
    the repository under the raw one would walk `<process cwd>/~/proj` and register
    nothing for links that were really written."""
    try:
        here = Path(os.path.expanduser(str(cwd))).resolve() if cwd else None
    except OSError:
        return None
    if here is None:
        return None
    for root in (here, *here.parents):
        dot = root / ".git"
        try:
            if dot.is_dir():
                git_dir = dot
            elif dot.is_file():
                pointer = dot.read_text(encoding="utf-8", errors="replace").strip()
                if not pointer.startswith("gitdir:"):
                    continue
                git_dir = Path(pointer.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = root / git_dir
            else:
                continue
            common = git_dir / "commondir"
            if common.is_file():
                rel = common.read_text(encoding="utf-8", errors="replace").strip()
                if rel:
                    shared = Path(rel)
                    git_dir = shared if shared.is_absolute() else git_dir / shared
            return root, git_dir.resolve() / "info" / "exclude"
        except OSError:
            return None
    return None


def escape_exclude_pattern(rel):
    # A gitignore pattern, not a path: escape the characters git reads as syntax so a
    # slug containing one can never widen the rule to other files.
    return "".join("\\" + c if c in "*?[]!#\\ " else c for c in rel)


def exclude_scope(base):
    """The NAME this materialization target's block carries — the absolute target
    base, resolved so that two presentations of one directory (a symlinked home, a
    relative cwd) are one scope rather than two competing blocks. None when the host
    publishes outside any repo (no base), or when the path could not be named in a
    single marker line — a newline in it would forge the markers, so we manage
    nothing rather than write a block we can never find again."""
    if not base:
        return None
    try:
        scope = str(Path(base).resolve())
    except OSError:
        return None
    if not scope.strip() or "\n" in scope or "\r" in scope:
        return None
    return scope


def render_exclude_block(scope, entries):
    # The managed block for ONE scope, or "" when that scope materialized nothing
    # (the block is dropped rather than left behind empty).
    if not entries:
        return ""
    lines = [GIT_EXCLUDE_BEGIN_PREFIX + scope]
    lines.extend("/" + escape_exclude_pattern(entry) for entry in entries)
    lines.append(GIT_EXCLUDE_END_PREFIX + scope)
    return "\n".join(lines) + "\n"


def _hold_exclude_lock(path, timeout=2.0, stale_after=30.0):
    """Best-effort exclusive claim on the exclude file, as an O_EXCL sentinel beside
    it. Returns the lock path when held, None when it gave up.

    Scoping the block stops concurrent writers destroying the USER's patterns, but
    the read-modify-write itself is still one: two sessions that both read before
    either writes leave the loser's block out of the published file, and its links
    then show up in `git status` until that session next reconciles. A lock is
    cheap here because the critical section is a few milliseconds of file IO.

    Giving up is deliberate rather than fatal: the write is best-effort ignore
    bookkeeping, never the materialization, so after the timeout we do exactly what
    the unlocked code did and at least register our own block."""
    lock = path.with_name(path.name + ".neuronzai.lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return lock
        except FileExistsError:
            try:
                # A crashed session must not lock the file out forever.
                if time.time() - os.path.getmtime(lock) > stale_after:
                    lock.unlink()
                    continue
            except OSError:
                pass
        except OSError:
            return None  # unwritable git dir: the write below will fail the same way
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def write_git_exclude(path, scope, entries):
    """Replace the block named `scope` with `entries` (repo-root-relative posix
    paths), leaving every other line untouched — the user's own patterns AND the
    blocks other scopes manage in this same shared file. Best-effort: an unwritable
    git dir costs a noisy `git status`, never the materialization."""
    lock = _hold_exclude_lock(path)
    try:
        _rewrite_exclude_block(path, scope, entries)
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass


def _rewrite_exclude_block(path, scope, entries):
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        existing = ""
    kept, dropping = [], False
    for line in existing.splitlines():
        stripped = line.strip()
        if dropping:
            # Any END marker closes the block: a truncated or hand-edited file must
            # not swallow the rest of the user's patterns.
            if stripped.startswith(GIT_EXCLUDE_END_PREFIX):
                dropping = False
            continue
        if stripped.startswith(GIT_EXCLUDE_BEGIN_PREFIX) and stripped[len(GIT_EXCLUDE_BEGIN_PREFIX):] == scope:
            dropping = True
            continue
        kept.append(line)
    head = "\n".join(kept).rstrip("\n")
    block = render_exclude_block(scope, entries)
    updated = (head + "\n" if head else "") + block
    if updated == existing:
        return  # no write, so a read-only git dir with nothing to change stays quiet
    # The file is the USER'S, and several sessions in one repo write it at once, so
    # the staging name must be unique: a shared one lets a second writer truncate the
    # source the first is about to publish, losing patterns we never owned.
    tmp = path.with_name(f"{path.name}.neuronzai.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(updated, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def server_is_up():
    # Unauthenticated liveness probe used ONLY to qualify a 401/403 (see handle()).
    # Returns True iff GET /healthz returns a clean 200. ANY non-200, network error,
    # or timeout -> False. The endpoint is pure liveness (no auth, no DB), so it is
    # unreachable/non-200 exactly when the container is down or mid-deploy — letting
    # us tell a transient deploy-window 401 (server not up) from a real token
    # revocation (server up, still 401). Fails toward False, i.e. toward NOT purging.
    try:
        probe = urllib.request.Request(f"{api.BASE_URL}/healthz", method="GET")
        with urllib.request.urlopen(probe, timeout=2) as res:
            return res.getcode() == 200
    except Exception:
        return False


def effective_target_dir(host, kind, cwd=""):
    """The dir where the materializer PLACES this kind's on-disk entry for a session
    at `cwd` — the SINGLE source of truth shared by the SessionStart materializer
    (_Store.target_dir_for) and the SessionEnd run/usage ownership gates, which MUST
    resolve the SAME path or a per-cwd move silently breaks ownership detection. A
    host with project-scoped discovery relocates the dir under <cwd>/<base> (repo-
    local, so concurrent profiles in different repos never share one dir); otherwise
    the host's fixed config-home dir. Returns None when the host hosts no dir for
    this kind.

    The relocated dir keeps the BASENAME the host declared for that kind rather than
    this module's KIND_DIRS name, because the two are not always the same question:
    a host that projects one kind into another's directory (so both kinds answer with
    one dir) must keep doing so per-cwd, and composing KIND_DIRS would silently split
    them into two dirs the host never reads."""
    target = host.asset_target_dir(kind)
    if not target:
        return None
    base = host.asset_target_base(str(cwd or ""))
    if base:
        return Path(base) / os.path.basename(str(target).rstrip("/"))
    return Path(target)


# ---- the per-config-dir store (all host paths injected via config_home) ------
class _Store:
    """The owned symlink store + reconcile logic, bound to ONE host config dir and
    ONE set of supported kinds. Everything host-specific enters here through
    `home` (host.config_home()) and `supported` (host.caps.supported_asset_kinds);
    core never hardcodes a config path or a host name."""

    def __init__(self, host, cwd=""):
        self.host = host
        self.cwd = str(cwd or "")
        home = Path(host.config_home())
        self.home = home
        self.store_root = home / STORE_NAME  # our owned store (symlink targets)
        self.legacy_store = home / LEGACY_STORE_NAME  # pre-rename skills store
        self.cache_dir = home / ".neuronzai-cache"  # per-profile cache
        # Symlinks are placed via effective_target_dir(self.cwd); the store BODIES
        # stay under config_home either way, so only SYMLINKS ever land in the repo.
        # Materialize only kinds this host supports AND can host a target dir for, in
        # the conventional order (a host that returns no target dir for a kind — e.g.
        # no agent equivalent — silently omits it).
        supported = host.caps.supported_asset_kinds
        self.kinds = tuple(k for k in KIND_DIRS
                           if k in supported and host.asset_target_dir(k))

    def store_dir_for(self, pkey, kind):
        return self.store_root / pkey / KIND_DIRS[kind]

    def target_dir_for(self, kind):
        # The dir the agent reads this kind from — resolved through the shared
        # effective_target_dir so the run/usage ownership gates land on the SAME path.
        # None-target kinds never reach here (filtered out of self.kinds).
        return effective_target_dir(self.host, kind, self.cwd)

    def is_directory(self, kind):
        return self.host.asset_is_directory(kind)

    def on_disk_name(self, kind, slug):
        return slug if self.is_directory(kind) else f"{slug}.md"

    def cleanup_dirs_for(self, kind):
        current = str(self.target_dir_for(kind))
        return tuple(dict.fromkeys([current, *self.host.asset_legacy_target_dirs(kind)]))

    def retired_dirs(self):
        # The retired-kind dirs beside every dir this host publishes into, current
        # and legacy: the output-styles dir beside each kind dir, in the repo and the
        # config home alike.
        parents = dict.fromkeys(
            os.path.dirname(directory.rstrip("/"))
            for kind in self.kinds for directory in self.cleanup_dirs_for(kind)
        )
        return [os.path.join(parent, name) for parent in parents for name in RETIRED_KIND_DIRS]

    def cache_path_for(self, profile, cwd):
        # One cache file per profile when a profile is set; otherwise per cwd (full
        # sha256, NOT truncated, so distinct cwds can't collide on one file).
        if profile:
            key = "prof-" + profile_key(profile)
        else:
            key = "cwd-" + hashlib.sha256((cwd or "").encode("utf-8")).hexdigest()
        return self.cache_dir / f"assets-{key}.json"

    def write_cache(self, path, obj):
        # Atomic: write a temp file then os.replace, so a torn write can never leave
        # an invalid/partial JSON cache behind.
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(path))
        except OSError:
            pass  # best-effort: a missed cache only costs a full fetch next session

    def owned_symlink_target(self, link):
        # Return the RAW target of `link` IF it is a symlink whose (unresolved)
        # target sits INSIDE a Neuronz.ai asset store — this config home's, or ANY
        # other installation's. Using the raw os.readlink target — not
        # link.resolve() — means a DANGLING link into a store (its target file was
        # deleted) is still recognised as ours and stays removable. Returns None for
        # a real dir/file or a foreign symlink, so every removal/replacement routes
        # through it and we never touch the user's own assets.
        #
        # Deliberately NOT limited to self.store_root: now that the target dir lives
        # in the repo, one project dir can be written by SEVERAL installations of the
        # same host — a user running two or three config homes of it side by side —
        # each keeping its own store. A store-root-only check would read a sibling
        # installation's link as a stranger, refuse to replace it, and silently drop
        # the asset. Those links hold the same profile's set, so last writer wins.
        try:
            if not link.is_symlink():
                return None
            raw = os.readlink(link)
        except OSError:
            return None
        raw_path = Path(raw)
        if not raw_path.is_absolute():
            raw_path = link.parent / raw_path
        if any(part in STORE_DIR_NAMES for part in raw_path.parts):
            return raw_path
        return None

    def remove_owned_symlink(self, link):
        # Unlink ONLY if it is one of our symlinks (raw target inside a store root).
        # Returns True if removed. Never deletes a real dir/file or a foreign symlink.
        if self.owned_symlink_target(link) is None:
            return False
        try:
            link.unlink()
            return True
        except OSError:
            return False

    def remove_store_entry(self, entry):
        # Delete one entry from OUR store, never following a link out of it (#558).
        # A symlink is unlinked, not descended into — the store root is ours, but a
        # planted link inside it would otherwise redirect a recursive delete at
        # whatever it points at. `shutil.rmtree` unlinks the symlinks it meets
        # rather than following them, so a body directory is safe to remove whole.
        try:
            if entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(str(entry), ignore_errors=True)
            else:
                entry.unlink()
            return True
        except OSError:
            return False

    def prune_store(self, pkey, manifest):
        # Make THIS profile's store hold EXACTLY the manifest's bodies (#558).
        # reconcile() already prunes the target dirs to that set; without this the
        # bodies those links pointed at are never removed, so a renamed, descoped or
        # deleted asset leaves its file behind for as long as the config home lives
        # (measured on one real profile: 129 store files against 32 links, four of
        # them assets that no longer exist anywhere).
        #
        # Same manifest as the link prune, in the same pass, so the two can never
        # disagree about what the active set is. Runs LAST — after the links are
        # installed and the dirs pruned — so a hook killed mid-run leaves an extra
        # body rather than a link pointing at a body that is gone.
        #
        # ONLY this profileKey's dirs, and only kinds this host manages: another
        # profile's store may be linked from a repo this session knows nothing about,
        # and a kind dir left by a host version that supported more kinds is not ours
        # to judge either. A body removed while another repo on this same profile
        # still links to it leaves that link dangling until its own next session,
        # which is already handled — owned_symlink_target() recognises a dangling
        # link into a store as ours and removable, and a profile-wide manifest makes
        # any such link stale by definition.
        keep = {kind: set() for kind in self.kinds}
        # Per kept directory-kind entry: the attachment set the manifest DESCRIBES,
        # or None for an entry that does not describe one at all (see below).
        keep_files = {}
        for entry in manifest:
            kind = str((entry or {}).get("kind") or "").strip()
            slug = str((entry or {}).get("slug") or "").strip()
            if kind not in self.kinds or not slug:
                continue
            name = self.on_disk_name(kind, slug)
            keep[kind].add(name)
            if self.is_directory(kind):
                files = (entry or {}).get("files")
                keep_files[(kind, name)] = (
                    {str(rel).strip() for rel in files if str(rel).strip()}
                    if isinstance(files, list) else None
                )
        for name in RETIRED_KIND_DIRS:
            # The retired kind's bodies: every link to them was swept above.
            try:
                entries = list((self.store_root / pkey / name).iterdir())
            except OSError:
                entries = []
            for entry in entries:
                self.remove_store_entry(entry)
        for kind in self.kinds:
            store = self.store_dir_for(pkey, kind)
            try:
                entries = list(store.iterdir())
            except OSError:
                continue  # the kind has no store dir yet: nothing to prune
            for entry in entries:
                if entry.name not in keep[kind]:
                    self.remove_store_entry(entry)
                elif self.is_directory(kind):
                    self.prune_store_files(entry, keep_files.get((kind, entry.name)))

    def prune_store_files(self, base, kept):
        # The same invariant one level down, and the sharper half of #558: an asset
        # that KEEPS its slug while losing an attachment kept that file inside a
        # directory the agent READS, so a reference deleted upstream stayed visible
        # to the model — worse than an orphan body nothing points at.
        #
        # `kept` is None when the manifest entry does not describe its attachments,
        # which is the one case that must NOT prune: a cache written by an older
        # plugin can omit the list entirely, and an absent list read as "no
        # attachments" would delete every live attachment on a 304 heal. An entry
        # that genuinely has none carries an empty list, so the real empty case
        # still prunes.
        if kept is None:
            return
        allowed = {"SKILL.md"} | {rel.replace("\\", "/").strip("/") for rel in kept}
        for root, dirs, files in os.walk(str(base), topdown=False, followlinks=False):
            for name in files + dirs:
                path = Path(root) / name
                rel = os.path.relpath(str(path), str(base)).replace(os.sep, "/")
                if rel in allowed:
                    continue
                # A directory is removed only once it is empty, so a kept file deeper
                # in the tree is never taken with its parent (bottom-up walk).
                if path.is_dir() and not path.is_symlink():
                    try:
                        path.rmdir()
                    except OSError:
                        pass
                    continue
                self.remove_store_entry(path)

    def sync_git_exclude(self, links):
        # Register the symlinks we placed INSIDE the user's repository in that
        # repo's .git/info/exclude, so materializing never dirties `git status` and
        # `git add -A` can't commit a link into the project.
        #
        # We own exactly ONE block there: the one named after THIS session's target
        # base. A host publishing to its config home has no base and so manages
        # nothing here — it must not clear the block a project-scoped host in the
        # same repo is keeping, and neither must a sibling worktree or a session
        # rooted in a subdirectory, all of which share this one exclude file.
        scope = exclude_scope(self.host.asset_target_base(str(self.cwd or "")))
        if scope is None:
            return
        context = git_exclude_context(self.cwd)
        if context is None:
            return
        root, exclude_file = context
        entries = []
        for link in links:
            link = Path(link)
            try:
                # Resolve the PARENT only: resolving the link itself would follow it
                # into the store and land outside the repo.
                absolute = link.parent.resolve() / link.name
                entries.append(absolute.relative_to(root).as_posix())
            except (OSError, ValueError):
                continue  # unreadable, or outside this repo
        write_git_exclude(exclude_file, scope, sorted(dict.fromkeys(entries)))

    def publication(self):
        # WHERE this session publishes, and therefore WHO can read it: a project dir
        # is read only by sessions in that repo, a config-home dir by every session
        # of this agent whatever its cwd. Only the second can leak, and the ledger
        # sweep acts on exactly that distinction.
        base = self.host.asset_target_base(str(self.cwd or ""))
        if base:
            return published.SCOPE_REPO, str(base)
        return published.SCOPE_GLOBAL, str(self.home)

    def sync_publish_ledger(self, pkey, profile_name, links):
        # Record what we just published, then remove any GLOBAL publication left by
        # another profile. The record is what makes the sweep possible at all: the
        # config home of an agent nobody runs any more, or of a profile that has been
        # retired, is precisely the state a hardcoded list of foreign directories
        # cannot know about, and it goes on being read by every session that does
        # look there.
        scope, target = self.publication()
        links = [str(link) for link in links]
        published.record(self.host.name, str(self.store_root), target,
                         profile_name, pkey, links, scope)
        published.sweep_foreign(
            pkey,
            lambda path: self.owned_symlink_target(Path(path)) is not None,
            lambda path: self.remove_owned_symlink(Path(path)),
            keep=links,
        )

    def remove_all_owned_symlinks(self):
        # Offboarding (401/403, or a no-usable-cache outage): drop every symlink in
        # EVERY supported target dir that points into our store, leaving the user's
        # own assets untouched. Empty-but-correct beats a stale other-profile set.
        directories = dict.fromkeys(
            [*(directory for kind in self.kinds for directory in self.cleanup_dirs_for(kind)),
             *self.retired_dirs()]
        )
        for directory in directories:
            try:
                entries = list(Path(directory).iterdir())
            except OSError:
                continue
            for entry in entries:
                self.remove_owned_symlink(entry)
        self.sync_git_exclude([])  # nothing of ours is left to ignore
        scope, target = self.publication()
        # ... and nothing of ours is left to sweep either: an entry claiming a
        # publication we just removed would send every later session looking for
        # links that are gone.
        published.record(self.host.name, str(self.store_root), target, "", "", [], scope)

    def write_asset_store(self, pkey, kind, asset):
        if kind not in self.kinds:
            return None
        slug = str(asset.get("slug") or "").strip()
        if not slug or is_unsafe_rel(slug) or "/" in slug or "\\" in slug:
            return None
        body = asset.get("body")
        if not isinstance(body, str):
            return None
        source_body = body
        body = self.host.render_asset(kind, slug, source_body)

        store = self.store_dir_for(pkey, kind)
        if self.is_directory(kind):
            item_dir = store / slug
            # Ensure a REAL directory, never a symlink that could redirect writes out.
            if item_dir.is_symlink():
                item_dir.unlink()
            item_dir.mkdir(parents=True, exist_ok=True)
            write_no_follow(item_dir / "SKILL.md", body)
            files = []
            file_specs = list(asset.get("files") or [])
            file_specs.extend(
                {"path": path, "content": content}
                for path, content in self.host.render_asset_files(kind, slug, source_body).items()
            )
            for f in file_specs:
                rel = str((f or {}).get("path") or "").strip()
                content = (f or {}).get("content")
                if is_unsafe_rel(rel) or not isinstance(content, str):
                    continue  # skip the unsafe/malformed file, keep the rest of the asset
                dest = item_dir / rel
                try:
                    if store.resolve() not in dest.resolve().parents:
                        continue  # final containment check, belt-and-suspenders
                except OSError:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                write_no_follow(dest, content)
                if rel not in files:
                    files.append(rel)
            return {"kind": kind, "slug": slug, "files": files}
        store.mkdir(parents=True, exist_ok=True)
        write_no_follow(store / f"{slug}.md", body)  # attached files N/A for these
        return {"kind": kind, "slug": slug}

    def reconcile_symlink(self, pkey, kind, slug):
        # Point the target-dir entry at THIS profile's store entry, but ONLY if the
        # existing entry is absent or already one of ours. A real dir/file or foreign
        # symlink (the user's own asset of the same name) is left alone.
        is_dir = self.is_directory(kind)
        name = self.on_disk_name(kind, slug)
        link = self.target_dir_for(kind) / name
        target = self.store_dir_for(pkey, kind) / name
        existing = self.owned_symlink_target(link)
        if existing is not None:
            if str(existing) == str(target):
                return True
            # Ours but pointing elsewhere (another profile's store, or the legacy flat
            # store on upgrade) — replace.
            try:
                link.unlink()
            except OSError:
                return False
        elif link.exists() or link.is_symlink():
            return False
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=is_dir)
            return True
        except OSError:
            return False

    def reconcile(self, pkey, manifest, profile_name=""):
        # Make the shared target dirs hold EXACTLY this profile's active set among OUR
        # owned symlinks. INSTALL this profile's links FIRST (idempotent), then PRUNE
        # last — so a hook killed mid-run leaves this profile present rather than gone.
        if not pkey or not isinstance(manifest, list):
            return
        self.store_root.mkdir(parents=True, exist_ok=True)

        active = {str(self.target_dir_for(kind)): set() for kind in self.kinds}
        active_slugs = {kind: set() for kind in self.kinds}
        installed_slugs = {kind: set() for kind in self.kinds}
        installed_claims = {}
        installed_links = []
        order = {kind: index for index, kind in enumerate(self.kinds)}
        ordered_manifest = sorted(
            manifest,
            key=lambda entry: order.get(str((entry or {}).get("kind") or "").strip(), -1),
        )
        for entry in ordered_manifest:
            kind = str((entry or {}).get("kind") or "").strip()
            slug = str((entry or {}).get("slug") or "").strip()
            if kind not in self.kinds or not slug:
                continue
            installed = self.reconcile_symlink(pkey, kind, slug)
            claim = (str(self.target_dir_for(kind)), self.on_disk_name(kind, slug))
            previous_kind = installed_claims.get(claim)
            if installed and previous_kind and previous_kind != kind:
                print(
                    f"[assets_sync] {previous_kind} and {kind} share the catalog entry "
                    f'"{claim[1]}" in {claim[0]}; {kind} wins',
                    file=sys.stderr,
                )
            if installed:
                installed_claims[claim] = kind
                installed_slugs[kind].add(slug)
                installed_links.append(Path(claim[0]) / claim[1])
            active[str(self.target_dir_for(kind))].add(self.on_disk_name(kind, slug))
            active_slugs[kind].add(slug)

        # Prune LAST: in each target dir remove OUR symlinks whose name is not in this
        # profile's active set (this profile's deleted assets AND any other profile's
        # leftover links — the no-bleed guarantee). A kind with zero active entries
        # still self-cleans. We never create an empty target dir here.
        current_dirs = dict.fromkeys(str(self.target_dir_for(kind)) for kind in self.kinds)
        for directory in current_dirs:
            tdir = Path(directory)
            if not tdir.exists():
                continue
            try:
                entries = list(tdir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name in active[directory]:
                    continue
                self.remove_owned_symlink(entry)

        # A legacy dir that IS one of this session's target dirs must be left to the
        # prune pass above: sweeping it here would delete the links just installed,
        # keeping only the slugs that FAILED. The two are compared as directories,
        # not as strings — a symlinked home or a `~` in the cwd names the same place
        # by a different path, and a miss is silent (assets installed, then removed).
        current_real = {os.path.realpath(directory) for directory in current_dirs}
        legacy_keep = {}
        for kind in self.kinds:
            failed_slugs = active_slugs[kind] - installed_slugs[kind]
            for directory in self.host.asset_legacy_target_dirs(kind):
                if os.path.realpath(directory) in current_real:
                    continue
                keep = legacy_keep.setdefault(directory, set())
                for slug in failed_slugs:
                    keep.update(self.host.asset_legacy_names(kind, slug))
        for directory in self.retired_dirs():
            legacy_keep.setdefault(directory, set())
        for directory, keep in legacy_keep.items():
            try:
                entries = list(Path(directory).iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name in keep:
                    continue
                self.remove_owned_symlink(entry)

        self.sync_git_exclude(installed_links)
        self.sync_publish_ledger(pkey, profile_name, installed_links)

        # LAST, and after the link prune on purpose (#558): the store holds the
        # bodies those links point at, so dropping a body before its link exists
        # would be the one ordering that can leave a session pointing at nothing.
        self.prune_store(pkey, manifest)

    def healable(self, cache):
        pkey = cache.get("profileKey")
        manifest = cache.get("manifest")
        if not pkey or not isinstance(manifest, list):
            return False
        for entry in manifest:
            kind = str((entry or {}).get("kind") or "").strip()
            slug = str((entry or {}).get("slug") or "").strip()
            if kind not in self.kinds or not slug:
                return False
            store = self.store_dir_for(pkey, kind)
            if self.is_directory(kind):
                base = store / slug
                if not (base / "SKILL.md").exists():
                    return False
                for rel in (entry or {}).get("files") or []:
                    if is_unsafe_rel(rel) or not (base / rel).exists():
                        return False
            else:
                if not (store / f"{slug}.md").exists():
                    return False
        return True

    def heal(self, cache):
        # Reconcile the shared dirs from the persisted store using the cached profile
        # + manifest (no network). Used on a 304 and on an outage with a healable
        # cache. (The in-session re-scan directive rides handle()'s return, not this
        # side-effect path — see the IN-SESSION RELOAD note above.)
        self.reconcile(cache.get("profileKey"), cache.get("manifest"),
                       str(cache.get("profile") or ""))

    def offboard(self):
        # No usable cache to rebuild THIS profile's set: drop all our symlinks so a
        # previous profile's links can't bleed into this session. Empty-but-correct
        # beats a stale other-profile set.
        self.remove_all_owned_symlinks()


def _reconcile(event: Event, host: Host, force: bool = False):
    """Materialize the active profile's assets into the host's config-dir subdirs,
    reconciling the shared target dirs to hold EXACTLY this profile's active set.
    All work is a SIDE EFFECT on disk. Fail-open on outage, fail-closed (purge) only
    on a token-up 401/403. The caller (handle) emits the in-session reload directive.

    `force` skips the conditional request. The ETag saves a body refetch by asserting
    "you already hold this set" — true of the SERVER's answer, and the whole point of
    a user-invoked reload is that the answer is not what is in doubt: the store, the
    links or the dirs are. A 304 heals from the cached manifest, so a store entry that
    was corrupted or hand-edited would be healed back to its own broken content. A
    forced 200 rewrites every entry from the response body."""

    # Precedence: /switch-profile session override > NEURONZAI_PROFILE env > cwd.
    # This matters on a --resume of a switched session: SessionStart re-fires, so the
    # override (keyed by session id) must materialize the SWITCHED profile's assets,
    # not the cwd's. Under an override resolve() returns cwd="" (no anchor/route) and
    # the cache keys on the switched profile.
    profile, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)
    # The store MATERIALIZES into the host's discovery dir for the RAW session cwd
    # (event.cwd) — where a project-scoped host actually scans project assets — NOT
    # the git-canonical or override cwd resolve() returns (that is "" under
    # /switch-profile and the main-worktree root in a linked worktree/subdir, neither
    # of which a project-scoped host scans).
    # resolve()'s cwd still keys the cache + the ?cwd= profile lookup below.
    store = _Store(host, event.cwd)
    # The session id — sent as X-Session-Id so the server can record per-session
    # skill availability (the adoption-ratio denominator for BI).
    session_id = event.session_id

    cache_path = store.cache_path_for(profile, cwd)
    cache = read_cache(cache_path)
    can_heal = store.healable(cache)

    def fell_back(reason):
        # Every way this pass can fail short of a token rejection has the SAME disk
        # half: heal from the persisted store if we can, else offboard to empty (a
        # stale other-profile set must not bleed into this session). What differs is
        # the REASON, and it is carried out rather than collapsed — a caller that has
        # to tell the user cannot re-derive which branch ran (#536).
        if can_heal:
            store.heal(cache)
        else:
            store.offboard()
        return Materialized("store" if can_heal else "empty",
                            cache.get("profile") or profile,
                            _cached_count(cache) if can_heal else 0, reason)

    # Only short-circuit to a 304 when we can actually heal from the store; a missing
    # footprint (or cold/legacy cache) omits If-None-Match -> full 200. A forced
    # reload omits it too, so the bodies are rewritten rather than trusted.
    extra = {}
    if cache.get("etag") and can_heal and not force:
        extra["If-None-Match"] = cache["etag"]
    req_headers = api.headers(profile=profile, session_id=session_id, extra=extra)

    url = f"{api.BASE_URL}/api/assets/materialize"
    if not profile and cwd:
        url += "?" + urllib.parse.urlencode({"cwd": cwd})

    req = urllib.request.Request(url, headers=req_headers, method="GET")
    header_etag = ""
    try:
        with urllib.request.urlopen(req, timeout=4) as res:
            status = res.getcode()
            body = res.read()
            header_etag = (res.headers.get("ETag") or "").strip()
    except urllib.error.HTTPError as err:
        if err.code == 304:
            # Nothing changed server-side — but the shared dirs may have been mutated
            # by another profile's session, so RECONCILE (don't just return).
            # can_heal held (we only sent If-None-Match when it did).
            store.heal(cache)
            return Materialized("store", cache.get("profile") or profile,
                                _cached_count(cache), REASON_UNCHANGED)
        if err.code in (401, 403):
            # A 401/403 is only trustworthy as a REVOCATION when the server is
            # actually up. During a deploy the container is down/unhealthy, so an
            # in-flight SessionStart (compaction / resume / plugin reload) can race a
            # transient 401 — and fail-closing then wipes every synced asset until the
            # session is reopened. Gate on the unauthenticated /healthz probe: server
            # NOT up -> treat as an outage (heal from the store, KEEP the links +
            # cache); only server-up-and-still-401 is a real revoked/missing token.
            if not server_is_up():
                # /healthz proved the server is not serving, so from the user's side
                # this IS the unreachable case, not a rejection.
                return fell_back(REASON_UNREACHABLE)
            # Fail-CLOSED: server is up and rejecting our login (revoked / wrong env)
            # — remove ALL of our symlinks and clear the cache so recovery forces a
            # clean full 200.
            store.remove_all_owned_symlinks()
            clear_cache(cache_path)
            print(
                f"[assets_sync] auth failed ({err.code}) — run the Neuronz.ai login workflow; "
                "asset sync disabled "
                "and previously-synced assets removed",
                file=sys.stderr,
            )
            return Materialized("purged", profile, 0, REASON_TOKEN_REJECTED)
        # Other HTTP error: the server WAS reached and answered badly of its own
        # accord. Heal from the store if we can, else offboard to empty.
        return fell_back(REASON_SERVER_ERROR)
    except urllib.error.URLError:
        # Network down / timeout: nothing answered at all.
        return fell_back(REASON_UNREACHABLE)

    if status == 304:
        store.heal(cache)
        return Materialized("store", cache.get("profile") or profile,
                            _cached_count(cache), REASON_UNCHANGED)

    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        # A 200 with a garbage body must not leave a prior profile's links bleeding:
        # apply the same fail policy as an outage (heal if we can, else offboard).
        # The server answered — the body is what was wrong.
        return fell_back(REASON_BAD_PAYLOAD)

    assets = data.get("assets") or []
    profile_name = str(data.get("profile") or profile or "").strip()
    pkey = profile_key(profile_name) if profile_name else profile_key(cache.get("profile") or cwd)
    new_etag = str(data.get("hash") or "").strip()
    # Prefer the strong ETag header; fall back to the JSON `hash` (quoted to match
    # how the server weakly-compares it on If-None-Match).
    etag_to_cache = header_etag or (f'"{new_etag}"' if new_etag else "")

    manifest = []
    for a in assets:
        entry = store.write_asset_store(pkey, str((a or {}).get("kind") or "").strip(), a or {})
        if entry:
            manifest.append(entry)
    store.reconcile(pkey, manifest, profile_name)
    store.write_cache(
        cache_path,
        {"etag": etag_to_cache, "profile": profile_name, "profileKey": pkey, "manifest": manifest},
    )
    return Materialized("server", profile_name, len(manifest), REASON_FETCHED)


def handle(event: Event, host: Host):
    """Reconcile the profile's assets on disk, then ask the host to re-scan them
    in-session so freshly-synced skills are usable without waiting for the next
    session. The directive rides only where the host actually re-scans something
    (live_reload_asset_kinds non-empty); emitting it at a host that reads it and
    does nothing costs nothing, but SAYING it does is how a docs table ends up
    promising a reload that never happens.

    SAME-SLUG SWAP ACROSS PROFILES. The reconcile REPOINTS the shared target-dir
    symlink <targetDir>/<slug> at the new profile's store entry, keeping the path
    identical while the content behind it changes. That is safe to signal
    unconditionally: the reload directive is consumed as a full INVALIDATION of the
    host's asset loader memos (not an mtime/inode-keyed refresh), so the next lookup
    re-reads <targetDir>/<slug>/SKILL.md from disk and follows the NEW link target.
    The kinds the directive does NOT cover keep the set the session started with;
    that is why core/reload.py words the report from the capability, per kind."""
    _reconcile(event, host)
    return Output(reload_assets=bool(host.caps.live_reload_asset_kinds))


def materialize(event: Event, host: Host, force: bool = False) -> Materialized:
    """Reconcile without emitting anything, and report what happened. The entry point
    for a user-invoked reload (core/reload), which owns the message itself and must
    word it from the OUTCOME rather than from the fact that a function returned."""
    return _reconcile(event, host, force=force)
