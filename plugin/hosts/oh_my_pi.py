#!/usr/bin/env python3
"""oh-my-pi (omp) native-extension adapter for the shared Neuronz.ai lifecycle core.

The host id is `oh-my-pi`, NOT `omp`. tests/no_host_names_in_core.py is a
case-insensitive SUBSTRING scan over core/; the literal `omp` matches 122 existing
lines there ("compact", "compaction", "component"), which would make the gate
unusable. `oh-my-pi` and a word-bounded `omp` both match zero. The user-facing
binary and slash commands stay `omp`.

The extension module (omp/plugin.ts) normalizes omp's native event objects into the
flat payload parse_event() reads here: the
TypeScript side knows omp's shapes, this side knows the core contract. It has to do
real work for two of those fields, because omp puts neither on the event object —
`cwd` comes off the extension context, and the session id is not exposed on the
extension API at all (see session_id_from_transcript below).
"""

import json
import os
import re
import subprocess

from core.hostapi import Capabilities, Event


# Pinned to the build every capability below was verified against; a stale claim
# should be visible rather than silent. See features.json `verified` blocks.
MIN_OMP_VERSION = (18, 0, 5)
MIN_OMP_VERSION_TEXT = ".".join(str(part) for part in MIN_OMP_VERSION)

# omp's own state root. `PI_CODING_AGENT_DIR` is the documented override (its help
# text names ~/.omp/agent as the default), and `OMP_PROFILE`/--profile selects an
# isolated profile whose state lives beside it.
AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
DEFAULT_AGENT_DIR = os.path.join("~", ".omp", "agent")
# omp's project-scoped config dir (CONFIG_DIR_NAME upstream). Assets materialize
# under <cwd>/<PROJECT_CONFIG_DIR>/<kind> so each repo/profile gets its OWN
# discovered set instead of one shared config-home dir (verified: omp reads
# commands from <cwd>/.omp/commands and skills from <dir>/.omp/skills).
PROJECT_CONFIG_DIR = ".omp"

# omp's tool vocabulary is lowercase. Mapped to the canonical names core matches on.
_TOOL_ALIASES = {
    "bash": "Bash",
    "edit": "Edit",
    "write": "Write",
    "multiedit": "Edit",
    "apply_patch": "apply_patch",
    "skill": "Skill",
    "task": "Task",
}

# An MCP tool reaches omp namespaced by its server. Both the flattened
# `<server>_<tool>` form and omp's `<server>:<server>_<tool>` config form normalize
# to the canonical `mcp__<server>__<tool>` core matches against.
_MCP_COLON = re.compile(r"^([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)$")
_MCP_UNDERSCORE = re.compile(r"^(neuronzai)_(.+)$")
# With omp's default `tools.xdev=true` an MCP tool is not called by name at all: it
# is a DEVICE, reached as `write` with `path: xd://mcp__<server>_<server>_<tool>`
# and the JSON arguments in `content`. The proxy prefixes every tool with its
# server name, so the server appears twice; that repetition is what lets the
# device name be split back into `mcp__<server>__<tool>` without guessing where
# a foreign server's own underscores end.
_DEVICE_PREFIX = "xd://"
_MCP_DEVICE = re.compile(r"^mcp__([A-Za-z0-9.-]+)_\1_(.+)$")
# omp loads a skill by READING it (`read skill://<name>`), never through a `skill`
# tool, so the invocation core keys an asset run on is a read of the bare URL.
_SKILL_URL = re.compile(r"^skill://([A-Za-z0-9._-]+)/?$")
_PATCH_FILE_HEADER = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.MULTILINE
)


def _canonical_tool(name):
    raw = str(name or "").strip().strip("\"'")
    if not raw:
        return ""
    if raw.startswith("mcp__"):
        device = _MCP_DEVICE.match(raw)
        return f"mcp__{device.group(1)}__{device.group(2)}" if device else raw
    lowered = raw.lower()
    if lowered in _TOOL_ALIASES:
        return _TOOL_ALIASES[lowered]
    colon = _MCP_COLON.match(raw)
    if colon:
        # `neuronzai:neuronzai_fact_add` -> server `neuronzai`, tool `fact_add`.
        server, rest = colon.group(1), colon.group(2)
        prefix = server + "_"
        tool = rest[len(prefix):] if rest.startswith(prefix) else rest
        return f"mcp__{server}__{tool}"
    underscore = _MCP_UNDERSCORE.match(raw)
    if underscore:
        return f"mcp__{underscore.group(1)}__{underscore.group(2)}"
    return raw


def _skill_input(tool_input):
    """Normalize a skill invocation's arguments to the {"skill": slug} shape
    core/runs keys an asset run on, dropping the namespace prefix the asset is
    materialized under so the slug matches the stored asset."""
    slug = str(tool_input.get("skill") or tool_input.get("name") or "").strip()
    if slug.startswith("neuronzai-"):
        slug = slug[len("neuronzai-"):]
    return {"skill": slug}


def _normalize_call(tool_name, tool_input):
    """The one place omp's tool-call shapes become the calls core reasons about.

    Returns (canonical tool, input). Three omp-specific shapes are unwrapped here:
    a `write` to an `xd://` device is the DEVICE's call (an MCP device becomes the
    MCP tool with its JSON body as input, any other device keeps its own name), a
    `read` of a bare `skill://` URL is a skill invocation, and a `skill` call's
    arguments are reduced to the stored asset slug. Everything else passes through
    canonicalized. A device write is never a file edit: `_edited_path` sees the
    unwrapped name and returns "" for it.
    """
    tool = _canonical_tool(tool_name)
    args = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Write":
        path = str(args.get("path") or args.get("filePath") or args.get("file_path") or "").strip()
        if path.startswith(_DEVICE_PREFIX):
            device = path[len(_DEVICE_PREFIX):].strip().strip("/")
            if not device:
                return "Write", args
            try:
                body = json.loads(str(args.get("content") or ""))
            except ValueError:
                body = None
            return _canonical_tool(device), body if isinstance(body, dict) else {}
        return tool, args
    if tool == "read":
        match = _SKILL_URL.match(str(args.get("path") or "").strip())
        if match:
            return "Skill", _skill_input({"skill": match.group(1)})
        return tool, args
    if tool == "Skill":
        return tool, _skill_input(args)
    return tool, args


def _edited_path(tool_name, tool_input, cwd):
    """The file an edit TOUCHED, normalized. Core derives its file-edit triggers from
    this field alone, never from a host's edit-tool vocabulary."""
    if tool_name in ("Edit", "Write"):
        path = str(tool_input.get("filePath") or tool_input.get("file_path")
                   or tool_input.get("path") or "").strip()
    elif tool_name == "apply_patch":
        patch = str(tool_input.get("patch") or tool_input.get("command") or "")
        match = _PATCH_FILE_HEADER.search(patch)
        path = match.group(1).strip() if match else ""
    else:
        return ""
    if path and cwd and not os.path.isabs(path):
        return os.path.normpath(os.path.join(cwd, path))
    return path


def session_id_from_transcript(path):
    """Lift omp's real session id out of its session file.

    omp exposes no session id anywhere on the extension API — not on the event
    objects, not on the extension context, not on the runtime handle. It does write
    one into every session file, twice: in the basename after the timestamp, and in
    the `{"type":"session"}` header record. Reading it here means the id our
    session-scoped writes use is the SAME id omp shows in its own resume picker,
    which minting our own would not be.

    Prefer the header record and fall back to the basename, so a file whose first
    records are unreadable still yields the id. The first physical line is a
    fixed-width `{"type":"title"}` slot omp rewrites in place, so the header is the
    SECOND record — scan a few lines rather than assuming a position.
    """
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for _ in range(8):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("type") == "session":
                    found = str(record.get("id") or "").strip()
                    if found:
                        return found
    except OSError:
        pass
    base = os.path.basename(str(path))
    if base.endswith(".jsonl"):
        base = base[: -len(".jsonl")]
    _timestamp, _sep, tail = base.partition("_")
    return tail.strip()


class OhMyPiHost:
    name = "oh-my-pi"
    login_hint = "/neuronzai:login"
    TOOL_MATCHERS = {
        "SHELL": "Bash",
        "FILE_EDIT": "Edit|Write|apply_patch",
        "RULE_WRITE": "(?:mcp__.*__|.*_)(?:create|update|delete)_rule.*",
        "PR_WRITE": ("(?:mcp__.*__|.*_)(?:create_pull_request|pull_request_review_write|"
                     "add_comment_to_pending_review|add_reply_to_pull_request_comment).*"),
        "NOTION_WRITE": "(?:mcp__.*__|.*_)notion-(?:create-pages|update-page).*",
        "MESSAGE_WRITE": ("(?:mcp__.*__|.*_)(?:conversations_add_message|send_email|"
                          "add_issue_comment).*"),
        "ACTION_LOG": "(?:mcp__.*__|.*_)log_action.*",
        "TOPIC_MODE": "(?:mcp__.*__|.*_)(?:enter_topic|exit_topic)",
    }
    caps = Capabilities(
        # omp fires a distinct `session_shutdown`, so the terminal handlers do not
        # fall back to the per-turn stop.
        has_session_end=True,
        # omp DOES cap an AWAITED shutdown handler, at 2s
        # (SESSION_SHUTDOWN_HANDLER_TIMEOUT_MS), deliberately: an extension must not
        # hold Ctrl+C hostage. That cap does not belong here, because this field
        # CLAMPS the timeouts written into the generated manifest, and the manifest's
        # timeouts are the DETACHED child's own budget: `omp/plugin.ts` spawns the
        # terminal handlers detached and returns immediately, so the sweep's 200s
        # runs after omp has exited rather than inside a 2s window, and the child
        # ARMS that budget on itself (`entry.py:_arm_deadline`) because nothing
        # host-side outlives it to enforce one. Clamping to 2 here would truncate
        # the child instead of describing the host.
        session_end_timeout_cap=None,
        # VERIFIED on 18.0.5, and the reason omp is the only non-Claude-Code host
        # with self-consolidation. Context returned from the END-OF-TURN handler
        # (`agent_end`) is silently dropped, but that is not the lane: omp's
        # `session_stop` is deliberately Claude-Code-compatible, and its result
        # (`{continue, additionalContext}`, plus `decision`/`reason` aliases)
        # buys exactly the one extra agent turn the self-sweep needs. That is what
        # `omp/plugin.ts` returns.
        continues_from_stop_context=True,
        # VERIFIED absent: a pre-tool handler's returned message never reached the
        # model across two tool calls. omp's pre-tool return can block a call or
        # rewrite its arguments, but cannot add model-visible context, so the
        # advisory rule gate stays unwired here (features.json `rule-gate`).
        injects_on_pre_tool=False,
        drops_sessionstart_context_on_compact=False,
        # This host fires a distinct POST-compaction notification, and its
        # PRE-compaction handler can return `{cancel: true}` — so a compaction that
        # was requested here is not a compaction that happened. The bridge subscribes
        # to the post-compaction event and dispatches a compact-sourced session start
        # from it, which is where the delivery history is invalidated; nothing clears
        # anything at the cancelable pre-compaction moment.
        signals_completed_compaction=True,
        # omp exposes a first-class status surface to an extension (ctx.ui.setStatus),
        # so the topic badge needs no edit to any file the user owns — unlike Claude
        # Code, where a plugin cannot declare a status line and hooks/statusline_-
        # install.py has to rewrite the user's settings.json. The badge rides the
        # neutral Output.status_badge from core/topic_mode, which this adapter's emit()
        # renders and omp/plugin.ts applies; the second flag is what keeps that legacy
        # installer hook (registry: "statusline") off this host.
        custom_statusline=True,
        status_badge_from_result=True,
        supported_asset_kinds=frozenset({"skill", "command", "agent"}),
        # omp ships a headless CLI, but the capture trio below is not implemented
        # yet; leaving this False keeps core from spawning a run it cannot build.
        headless_capture=False,
        inline_context_limit_chars=10000,  # uniform inline-injection warning threshold (#519)
        side_files_over_inline_limit=False,  # measured #519: 900k tail survived verbatim, overflow wastes window
        substitutes_session_id=False,
        reports_model=True,             # the omp bridge reads ctx.models.current() (provider/id) at hook
                                        # dispatch and forwards it on the payload, e.g. anthropic/claude-fable-5-1
        # An extension can materialize assets but cannot make this host re-read
        # them: the rescan (caches, skills, slash commands, agents, MCP) hangs off
        # the built-in reload command, reachable only from the user's own input.
        # So: no kinds reload themselves, and the hint names the one keystroke
        # that fixes that — it keeps the conversation, it only re-reads discovery.
        live_reload_asset_kinds=frozenset(),
        manual_reload_hint="/reload-plugins",
    )

    # ---- lifecycle ----------------------------------------------------------
    def parse_event(self, canonical_event, raw_stdin):
        try:
            payload = json.loads(raw_stdin) if raw_stdin else {}
        except (TypeError, ValueError):
            payload = {}
        cwd = str(payload.get("cwd") or "").strip()
        tool_name, tool_input = _normalize_call(payload.get("tool_name"), payload.get("tool_input"))
        transcript_path = str(payload.get("transcript_path") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            session_id = session_id_from_transcript(transcript_path)
        return Event(
            event=canonical_event,
            session_id=session_id,
            cwd=cwd,
            # #527: omp RESTORES a resumed session's working directory — `-r` reports
            # the directory the session was BORN in and `--cwd` does not override it
            # (measured on 18.1.16) — so `cwd` above can be a repo the user is not in.
            # The bridge reads the launch directory (PWD, falling back to its own
            # process cwd) and puts it here; core compares the two and says so.
            launch_cwd=str(payload.get("launch_cwd") or "").strip(),
            source=str(payload.get("source") or "").strip(),
            prompt=str(payload.get("prompt") or ""),
            tool_name=tool_name,
            tool_input=tool_input,
            tool_response=payload.get("tool_response"),
            edited_path=_edited_path(tool_name, tool_input, cwd),
            transcript_path=transcript_path,
            # The omp bridge reads the live session model (ctx.models.current(),
            # which reflects /model switches) and puts it on the payload as
            # "<provider>/<id>", e.g. "anthropic/claude-fable-5-1". Lift it
            # lower-cased and verbatim — the provider/id shape is how omp names the
            # model, so it is NOT a prefix to strip. None when the bridge could not
            # read one. SESSION model, distinct from the pinned CAPTURE_MODEL.
            model=(str(payload.get("model") or "").strip().lower() or None),
            raw=payload,
        )

    def emit(self, canonical_event, output):
        del canonical_event
        if output is None:
            return
        body = {}
        if output.context:
            body["context"] = output.context
        if output.system_message:
            body["system_message"] = output.system_message
        if output.deny:
            body["deny"] = True
            body["deny_reason"] = output.deny_reason
        # No reload_assets leg: the extension API exposes no rescan, so the
        # directive was never read. What this host CAN do is in manual_reload_hint.
        #
        # `status_badge` is the one field whose EMPTY value is meaningful: "" is the
        # bridge's instruction to clear the badge on an exit_topic, so it is tested
        # for None and it alone decides the body is non-empty.
        if output.status_badge is not None:
            body["status_badge"] = output.status_badge
        if body:
            print(json.dumps(body, ensure_ascii=False))

    # ---- host locations -----------------------------------------------------
    def config_home(self):
        configured = os.environ.get(AGENT_DIR_ENV)
        root = configured if configured else os.path.expanduser(DEFAULT_AGENT_DIR)
        return str(root).rstrip("/")

    def requirements_notice(self):
        try:
            result = subprocess.run(
                ["omp", "--version"], check=False, capture_output=True,
                text=True, timeout=2,
            )
            output = (result.stdout or result.stderr or "").strip()
        except (OSError, subprocess.SubprocessError):
            result = None
            output = ""
        # omp prints `omp/<semver>`; the guard keeps the match off the leading name.
        match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", output)
        if result is not None and result.returncode == 0 and match:
            running = tuple(int(part) for part in match.groups())
            if running >= MIN_OMP_VERSION:
                return ""
            shown = ".".join(str(part) for part in running)
            return (
                f"Neuronz.ai requires oh-my-pi {MIN_OMP_VERSION_TEXT} or newer; this "
                f"session is running {shown}. Earlier releases do not provide the "
                "verified extension lifecycle used by this integration."
            )
        return (
            "Neuronz.ai could not verify the oh-my-pi version. oh-my-pi "
            f"{MIN_OMP_VERSION_TEXT} or newer is required for the verified extension "
            "lifecycle."
        )

    # ---- assets -------------------------------------------------------------
    # omp discovers each kind under its OWN state root, verified live: a SKILL.md
    # dropped in <agent dir>/skills/<slug>/ was loaded and listed by the running
    # agent. omp ALSO reads Claude Code's directories, which is why our skills are
    # already visible to an omp user who has Claude Code installed — but that path
    # is a bonus, never the mechanism: relying on it would silently strand an
    # omp-only user, and would make two hosts reconcile the same directory.
    def asset_target_dir(self, kind):
        if kind == "skill":
            return os.path.join(self.config_home(), "skills")
        if kind == "command":
            return os.path.join(self.config_home(), "commands")
        if kind == "agent":
            return os.path.join(self.config_home(), "agents")
        return None

    def asset_target_base(self, cwd):
        # Per-cwd target root: <cwd>/.omp. omp's native project discovery reads
        # this repo-local dir, so materializing here isolates concurrent profiles
        # (different repos = different dirs = no cross-profile clobber). No cwd (a
        # profile-override session with no repo) -> None -> the shared config-home
        # dirs of asset_target_dir(), preserving the pre-per-cwd behavior there.
        cwd = str(cwd or "").strip()
        if not cwd:
            return None
        return os.path.join(os.path.expanduser(cwd), PROJECT_CONFIG_DIR)

    def asset_is_directory(self, kind):
        return kind == "skill"

    def asset_legacy_target_dirs(self, kind):
        # Pre-per-cwd builds symlinked into the SHARED config-home dirs. Once the
        # target moves to <cwd>/.omp, those old links must be swept or the old
        # cross-profile leak survives beside the fix — so the config-home dir for
        # this kind is the legacy dir the reconcile/offboard sweep cleans.
        direct = self.asset_target_dir(kind)
        return [direct] if direct else []

    def asset_legacy_names(self, kind, slug):
        # The on-disk name a pre-per-cwd build used in the shared dir, so a slug
        # that fails to install into <cwd>/.omp keeps its old shared link as a
        # fallback rather than being swept to nothing.
        if not slug:
            return []
        return [slug if self.asset_is_directory(kind) else f"{slug}.md"]

    def render_asset(self, kind, slug, body):
        del kind, slug
        return body

    def render_asset_files(self, kind, slug, body):
        del kind, slug, body
        return {}

    # ---- detached capture ---------------------------------------------------
    # Not implemented yet: omp ships a headless CLI (`omp -p --mode json`) that this
    # can be built on, but until it is, caps.headless_capture stays False so core
    # never reaches these. Returning None rather than a half-built command keeps a
    # capture from being spawned that cannot complete.
    def capture_command(self, session_id, effort, mcp_config, plugin_root):
        del session_id, effort, mcp_config, plugin_root
        return None

    def capture_artifacts(self, session_id):
        del session_id
        return []

    def capture_receipt(self, stdout):
        del stdout
        return json.dumps({"host": self.name, "started": False,
                           "status": "unsupported"}, ensure_ascii=False).encode("utf-8")

    # ---- transcript ---------------------------------------------------------
    def iter_transcript(self, path):
        """Walk one omp session file into the shared per-entry shape.

        The file is a flat JSONL event log, not a message array: a fixed-width
        `title` slot, a `session` header, `model_change` / `thinking_level_change`
        records, `message` records wrapping the real message, and `custom` records
        carrying tool execution. Model and effort therefore arrive on their OWN
        records BEFORE the assistant messages they describe, so they are tracked as
        running state and attached to each assistant entry as it is yielded.
        """
        try:
            handle = open(path, "r", encoding="utf-8")
        except OSError:
            return
        model = ""
        effort = ""
        pending_tool_uses = []
        # Every toolCall part already yielded, by omp's call id. A call is recorded
        # TWICE in the file — as the assistant message's toolCall part and, AFTER
        # that message, as a `tool_execution_start` record whose args differ (omp
        # lifts the model's `i` intent field out of the execution args) — so the
        # id, not the arguments, is what pairs them. Without this every call
        # counted twice, the second copy attached to the NEXT assistant message.
        seen_call_ids = set()
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")

                if kind == "model_change":
                    model = str(record.get("model") or "")
                    continue
                if kind == "thinking_level_change":
                    effort = str(record.get("thinkingLevel") or "")
                    continue
                if kind == "custom":
                    data = record.get("data")
                    if not isinstance(data, dict):
                        continue
                    if record.get("customType") == "tool_execution_start":
                        call_id = str(data.get("toolCallId") or "")
                        if call_id and call_id in seen_call_ids:
                            continue
                        tool, args = _normalize_call(data.get("toolName"), data.get("args"))
                        if tool:
                            pending_tool_uses.append((call_id, {"name": tool, "input": args}))
                    continue
                if kind != "message":
                    continue

                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                content = message.get("content")
                texts = []
                tool_uses = []
                tool_results = []
                call_ids = set()
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        part_type = part.get("type")
                        if part_type == "text":
                            texts.append(str(part.get("text") or ""))
                        elif part_type == "toolCall":
                            tool, args = _normalize_call(part.get("name"), part.get("arguments"))
                            if tool:
                                tool_uses.append({"name": tool, "input": args})
                                seen_call_ids.add(str(part.get("id") or ""))
                        elif part_type in ("toolResult", "tool_result"):
                            result = part.get("content") or part.get("output")
                            if result:
                                tool_results.append(
                                    json.dumps(result, ensure_ascii=False)
                                    if isinstance(result, (dict, list)) else str(result)
                                )
                if role == "toolResult":
                    result = message.get("content")
                    if result:
                        tool_results.append(
                            json.dumps(result, ensure_ascii=False)
                            if isinstance(result, (dict, list)) else str(result)
                        )
                if role == "assistant" and pending_tool_uses:
                    # An execution record with no part of its own (an id the message
                    # stream never carried) still contributes its tool here; the
                    # (name, input) key is the fallback for a record with no id at all.
                    known = {(use["name"], json.dumps(use["input"], sort_keys=True))
                             for use in tool_uses}
                    for call_id, use in pending_tool_uses:
                        if call_id and call_id in seen_call_ids:
                            continue
                        key = (use["name"], json.dumps(use["input"], sort_keys=True))
                        if key not in known:
                            tool_uses.append(use)
                    pending_tool_uses = []
                yield {
                    "role": role,
                    "texts": texts,
                    "tool_uses": tool_uses,
                    "tool_results": tool_results,
                    "model": model if role == "assistant" else "",
                    "effort": effort if role == "assistant" else "",
                    "meta": bool(message.get("customType")),
                    # omp runs a subagent as its own session with its own file, so a
                    # record in THIS file is never a sidechain of another one.
                    "sidechain": False,
                }
