#!/usr/bin/env python3
# Claude Code host adapter (topic 90ae46be).
#
# Maps Claude Code's hook stdin/stdout onto the core Event/Output port. Claude
# Code's payload is ALREADY the canonical shape core expects, so parse_event is
# mostly field lifting; emit renders Output to Claude's hookSpecificOutput JSON.
# The only Claude-specific bits live HERE (never in core/): the CLAUDE_PROJECT_DIR
# cwd fallback and the capability flags.
#
# The cwd chain is HOST-STATED VALUES ONLY (payload, then the host's own env) —
# never the hook process's directory. See parse_event for why (#383).

import json
import os
import sys

from core.hostapi import Capabilities, Event, Output

# The model the detached capture runs at. PINNED — the capture no longer inherits
# whatever the session happened to be on.
#
# Measured 2026-08-11 over ONE identical 404,282-character payload, reasoning
# effort held at `medium`, varying only `--model`:
#
#   claude-opus-5              120.4s  22 turns  $3.3095  10 facts, 1 action
#   claude-sonnet-5             94.2s  20 turns  $1.9467   8 facts, 1 action
#   claude-haiku-4-5-20251001   45.0s   4 turns  $0.4747   0 facts, 0 actions
#
# The haiku run REPORTED SUCCESS — is_error false, clean exit, a closing line about
# what the session had shipped — and wrote nothing at all. Repeated with the child's
# transcript kept, it took ONE turn, made ZERO tool calls, and answered the
# transcript conversationally instead of executing the capture contract. The cause
# is the shape of the prompt: core/detached.build_prompt puts ~4,100 characters of
# instructions in FRONT of a payload that can be a hundred times longer, and holding
# an instruction across that ratio is the whole job here. The two larger models hold
# it; that one did not, and it failed silently, which is the worst way to fail a
# lane nobody is watching.
#
# PINNED, not capped. A ceiling takes the lower of the session's model and the cap,
# which would pass a haiku session's haiku straight through — the exact model the
# calibration disqualified.
CAPTURE_MODEL = "claude-sonnet-5"
CAPTURE_MODEL_ENV = "NEURONZAI_CAPTURE_MODEL"

# Canonical event name -> Claude Code hook event name (for hookEventName echo).
_HOOK_EVENT_NAME = {
    "session_start": "SessionStart",
    "user_prompt": "UserPromptSubmit",
    "pre_tool": "PreToolUse",
    "post_tool": "PostToolUse",
    "stop": "Stop",
    "pre_compact": "PreCompact",
    "session_end": "SessionEnd",
}


class ClaudeCodeHost:
    name = "claude-code"
    login_hint = "/neuronzai:login"
    # Semantic tool category -> Claude Code tool-name regex fragment (for generated
    # matchers). A category absent here contributes nothing to this host's matcher.
    TOOL_MATCHERS = {
        "SHELL": "Bash",
        "FILE_EDIT": "Edit|Write",
        "NOTEBOOK": "NotebookEdit",
        "RULE_WRITE": "mcp__.*(create|update|delete)_rule.*",
        "PR_WRITE": ("mcp__.*create_pull_request.*|mcp__.*pull_request_review_write.*|"
                     "mcp__.*add_comment_to_pending_review.*|mcp__.*add_reply_to_pull_request_comment.*"),
        "NOTION_WRITE": "mcp__.*notion-create-pages.*|mcp__.*notion-update-page.*",
        "MESSAGE_WRITE": ("mcp__.*conversations_add_message.*|mcp__.*send_email.*|"
                          "mcp__.*add_issue_comment.*"),
        "ACTION_LOG": "mcp__.*log_action.*",
        "TOPIC_MODE": "mcp__.*enter_topic|mcp__.*exit_topic",
    }
    caps = Capabilities(
        has_session_end=True,
        session_end_timeout_cap=None,
        continues_from_stop_context=True,
        injects_on_pre_tool=True,
        drops_sessionstart_context_on_compact=True,
        # A compaction is followed by a SessionStart fire carrying source="compact",
        # which IS the completed-compaction report — so the delivery-history
        # invalidation happens there and never at PreCompact.
        signals_completed_compaction=True,
        custom_statusline=True,
        # statusLine is a command this host POLLS from the user's own settings — the
        # badge is composed inside that command, not handed back on a hook result.
        status_badge_from_result=False,
        supported_asset_kinds=frozenset({"skill", "command", "agent"}),
        headless_capture=True,         # `claude -p` runs one turn with no terminal
        inline_context_limit_chars=10000,  # uniform inline-injection warning threshold (#519)
        substitutes_session_id=True,   # ${CLAUDE_SESSION_ID} expands in a command the model runs
        reports_model=True,            # every hook payload carries `model` (bare id, e.g. claude-opus-5)
        side_files_over_inline_limit=True,  # measured #519: >10000 chars side-file to a preview = silent data loss
        # `reloadSkills` re-scans the SKILL dirs in the running session; the other
        # three kinds are read once at startup and have no rescan directive, so a
        # reload reports them honestly as needing a new session.
        live_reload_asset_kinds=frozenset({"skill"}),
        manual_reload_hint="",         # nothing the user can type reaches the other kinds
    )

    # The native file-edit tools that carry a structured `file_path` — the source
    # of the normalized Event.edited_path on this host.
    _EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

    def parse_event(self, canonical_event: str, raw_stdin: str) -> Event:
        payload = {}
        if raw_stdin:
            try:
                payload = json.loads(raw_stdin)
            except Exception:
                payload = {}
        # cwd: stdin payload first (reliable per-session value), then the
        # CLAUDE_PROJECT_DIR env — both values the HOST itself states. There is
        # deliberately NO os.getcwd() leg (#383): the hook process's own directory
        # is a value WE picked, not one the host reported, and this field no longer
        # only routes a profile — the gate and per-prompt recall forward it as the
        # `workdir`/`branch` OBSERVABLES a rule condition binds to. The evaluator
        # treats a target it cannot see as UNKNOWN and forwards that leaf to the
        # agent as a question; a confidently WRONG value instead resolves the leaf
        # FALSE and silently drops a rule that should have been asked about. "" is
        # the honest answer and every consumer already omits the param for it.
        cwd = str(
            payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ""
        ).strip()
        tool_name = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        # Normalized edited-path: on this host a file edit carries a structured
        # `file_path`, so lift it verbatim for the edit tools (unchanged for every
        # other tool → ""). Core reads this instead of the raw file_path.
        edited_path = str(tool_input.get("file_path") or "") if tool_name in self._EDIT_TOOLS else ""
        return Event(
            event=canonical_event,
            session_id=str(payload.get("session_id") or "").strip(),
            cwd=cwd,
            source=str(payload.get("source") or "").strip(),
            prompt=str(payload.get("prompt") or ""),
            tool_name=tool_name,
            tool_input=tool_input,
            tool_response=payload.get("tool_response"),
            edited_path=edited_path,
            transcript_path=str(payload.get("transcript_path") or "").strip(),
            # The session model rides on every Claude Code hook payload as a bare
            # id (e.g. "claude-opus-5"). Lift it lower-cased and verbatim — no
            # provider prefix added or stripped — for the model-scoped conditions
            # the gate/recall/bootstrap requests forward. None when absent, which
            # every consumer omits. This is the SESSION model, NOT CAPTURE_MODEL.
            model=(str(payload.get("model") or "").strip().lower() or None),
            raw=payload,
        )

    def emit(self, canonical_event: str, output) -> None:
        # Claude Code accepts several output keys in ONE object, so build a combined
        # payload rather than XOR: e.g. the self-sweep emits a user-facing
        # `systemMessage` status line ALONGSIDE the model-visible additionalContext.
        if output is None:
            return
        out = {}
        if output.deny:
            out["decision"] = "block"
            out["reason"] = output.deny_reason
        if output.system_message:
            out["systemMessage"] = output.system_message
        if output.context or output.reload_assets:
            hso = {"hookEventName": _HOOK_EVENT_NAME.get(canonical_event, canonical_event)}
            if output.context:
                hso["additionalContext"] = output.context
            if output.reload_assets:
                hso["reloadSkills"] = True   # Claude Code re-scans skill dirs in-session
            out["hookSpecificOutput"] = hso
        if out:
            print(json.dumps(out, ensure_ascii=False))

    def config_home(self) -> str:
        return (os.environ.get("CLAUDE_CONFIG_DIR")
                or os.path.join(os.path.expanduser("~"), ".claude")).rstrip("/")

    def requirements_notice(self) -> str:
        return ""

    # Where each asset KIND materializes (the dir the agent reads). None = this host
    # can't host that kind. Claude Code reads all three kinds from BOTH its config
    # home and the project dir; we publish to the project dir (asset_target_base)
    # and keep the config-home dir only as a legacy sweep target.
    _ASSET_SUBDIR = {"skill": "skills", "command": "commands",
                     "agent": "agents"}

    def asset_target_dir(self, kind):
        sub = self._ASSET_SUBDIR.get(kind)
        return os.path.join(self.config_home(), sub) if sub else None

    def asset_target_base(self, cwd):
        # Per-cwd target root: <cwd>/.claude. The config-home dir is read by EVERY
        # Claude session whatever its cwd (and, since other harnesses scan it too, by
        # other agents), so publishing one profile's set there hands it to every other
        # profile. The project dir is scanned only inside this repo, which is the
        # scope the assets were resolved for. Always ".claude" regardless of
        # CLAUDE_CONFIG_DIR: the config dir is per-installation, the project dir is
        # fixed by the product. No cwd -> None -> the config-home dirs, unchanged.
        cwd = str(cwd or "").strip()
        if not cwd:
            return None
        return os.path.join(os.path.expanduser(cwd), ".claude")

    def asset_is_directory(self, kind):
        return kind == "skill"

    def asset_legacy_target_dirs(self, kind):
        # Pre-per-cwd builds symlinked into the config-home dirs, where every other
        # profile's session could see them. Those links must be swept once the target
        # moves into the repo, or the leak survives beside the fix. In a session whose
        # cwd IS the config home's parent (a $HOME session) the two dirs coincide;
        # the reconcile sweep skips a legacy dir that is also the current target.
        direct = self.asset_target_dir(kind)
        return [direct] if direct else []

    def asset_legacy_names(self, kind, slug):
        # The on-disk name a pre-per-cwd build used in the config-home dir, so a slug
        # that fails to install into <cwd>/.claude keeps its old link as a fallback
        # rather than being swept to nothing.
        if not slug:
            return []
        return [slug if self.asset_is_directory(kind) else f"{slug}.md"]

    def render_asset(self, kind, slug, body):
        del kind, slug
        return body

    def render_asset_files(self, kind, slug, body):
        del kind, slug, body
        return {}

    # Where a headless run leaves its own transcript. Deliberately a GLOB over the
    # project dirs rather than a re-implementation of the host's path-to-slug rule:
    # the id is a UUID we minted for that one run, so a glob cannot match anything
    # else, and it cannot drift when the slug rule changes.
    def capture_artifacts(self, session_id):
        if not session_id:
            return []
        return [os.path.join(self.config_home(), "projects", "*", session_id + ".jsonl")]

    # The ONLY built-in tools the capture run may have. It reasons over a payload
    # that deliberately includes tool RESULTS — fetched pages, file contents, command
    # output — i.e. text an attacker can influence, in a run that is unsupervised,
    # unwatched and whose output is discarded. Under bypassPermissions a shell there
    # is remote code execution behind a prompt injection, with nobody to notice.
    #
    # This is `--tools`, which controls what EXISTS, not `--allowedTools`, which only
    # pre-approves what already does — under a permission bypass an allowlist changes
    # nothing. Verified live: this leaves exactly Glob/Grep/Read among the built-ins
    # while all 112 of our MCP tools still arrive, since --tools bounds the BUILT-IN
    # set only.
    CAPTURE_TOOLS = "Read,Grep,Glob"

    # The same boundary stated a SECOND way, through a different mechanism. One flag
    # is one assumption about a CLI we do not own: rename it, or change what it means
    # under a permission bypass, and an unattended run silently regains a shell with
    # nothing failing. These two are independent — an allow-list over the built-in set
    # and a deny-list over named tools — so the guarantee survives either one being
    # wrong. Mirrors the deny-list this repo already uses to sandbox the same binary
    # in the shared base-ci reviewer, which also keeps every model lane read-only.
    #
    # NOT also --allowedTools: it is a PERMISSION list, so under bypassPermissions it
    # grants nothing and removes nothing. Passing it would read like a third control
    # while doing nothing, which is worse than its absence.
    CAPTURE_DENIED_TOOLS = ("Bash,Edit,MultiEdit,Write,NotebookEdit,"
                            "WebFetch,WebSearch,Agent,Task")

    def capture_command(self, session_id, effort, mcp_config, plugin_root):
        """One silent headless turn, reading its prompt on stdin.

        `--setting-sources ''` is the recursion guard: with no user settings there
        are no plugin hooks, so this run's own session end spawns nothing. It also
        costs the run every skill and MCP server the user has configured, which is
        why the tools come back explicitly via --mcp-config — and it drops any
        permission denies the user had configured, which is a second reason the tool
        surface has to be bounded HERE rather than inherited.

        `--strict-mcp-config` pins the tool set. Without it the run's available
        tools depend on which of the user's other MCP servers happened to boot in
        time, so the prompt prefix — and its cache key — changes run to run.

        `bypassPermissions` stays because nobody is present to answer a prompt; what
        makes that safe is that the surface it bypasses is read-only (CAPTURE_TOOLS).

        `--model` is PINNED here rather than taken from the session (CAPTURE_MODEL);
        `--effort` is the one thing still inherited, capped by core.
        """
        command = ["claude", "-p",
                   "--session-id", session_id,
                   "--setting-sources", "",
                   "--strict-mcp-config",
                   "--mcp-config", mcp_config,
                   "--tools", self.CAPTURE_TOOLS,   # no shell, no writes, no fetches
                   "--disallowedTools", self.CAPTURE_DENIED_TOOLS,   # …said twice, two ways
                   "--permission-mode", "bypassPermissions",   # nobody is here to answer
                   "--output-format", "json",
                   "--model", self.capture_model()]
        if effort:
            command += ["--effort", effort]
        # Our bundled MCP config reaches the plugin through this variable. This host
        # defines it for processes IT launched, and the detached run is not one of
        # those, so without this the config expands to nothing, the run starts with
        # no memory tools, and it writes nothing at all — silently, since it is
        # silent by design.
        return command, {"CLAUDE_PLUGIN_ROOT": plugin_root}

    def capture_model(self):
        """The pinned capture model, overridable per machine (CAPTURE_MODEL_ENV).

        The override exists so an operator can move the whole lane onto a different
        model without waiting on a release — not so it can be lowered safely. A
        blank or unset value is the pin, never "let the session decide".
        """
        return os.environ.get(CAPTURE_MODEL_ENV, "").strip() or CAPTURE_MODEL

    def capture_receipt(self, stdout):
        """`--output-format json` prints exactly one result envelope and nothing
        else, so this host's stdout already IS the receipt — status, turn count,
        cost. There is nothing to fold out of it."""
        return stdout

    def _tool_result_text(self, entry, block):
        """One tool result as text, densest view first.

        `toolUseResult` is a structured sibling of `message` that carries what the
        prose block only renders: a command's stdout and stderr separately, an
        edit's diff hunks, a search's matches. It is both shorter and more precise
        than the rendered block, so it wins whenever it is present.
        """
        result = entry.get("toolUseResult")
        if isinstance(result, dict):
            if "stdout" in result:
                parts = [str(result.get("stdout") or "").strip()]
                stderr = str(result.get("stderr") or "").strip()
                if stderr:
                    parts.append("stderr: " + stderr)
                if result.get("interrupted"):
                    parts.append("[interrupted]")
                return "\n".join(p for p in parts if p) or "(no output)"
            if "structuredPatch" in result or "filePath" in result:
                lines = []
                for hunk in result.get("structuredPatch") or []:
                    lines.extend(hunk.get("lines") or [])
                return "edited %s\n%s" % (result.get("filePath") or "?",
                                          "\n".join(lines) if lines else "(no diff)")
            if "matches" in result or "query" in result:
                return "query=%r -> %s" % (result.get("query"), result.get("matches"))
        if isinstance(result, str):
            return result
        content = block.get("content")
        if isinstance(content, str):
            return content
        return "\n".join(str(item.get("text") or "") for item in (content or [])
                         if isinstance(item, dict) and item.get("type") == "text")

    def iter_transcript(self, path):
        """Yield NORMALIZED transcript entries from Claude Code's JSONL transcript,
        so transcript-parsing handlers (runs/usage/detached capture/recall-ctx) stay host-agnostic.
        Each entry:
            {"role": str, "texts": [str, ...], "tool_uses": [{"name", "input"}, ...],
             "tool_results": [str, ...], "model": str, "effort": str,
             "meta": bool, "sidechain": bool}

        `tool_results` exists for the detached capture, which needs what the prose
        alone never says — an agent writes "let me check" and the RESULT is what
        settled it. Consumers that only want conversation simply ignore the field.

        `model` and `effort` are what THIS turn ran at, and are "" on a synthetic
        entry (the host's own placeholder turns carry a sentinel model, never a real
        one). The detached capture reads them so its own run matches what the user
        was on.

        `path` is a filesystem path OR an already-positioned file DESCRIPTOR (int):
        it is handed to open() unchanged, so a caller that only wants the tail of a
        huge transcript can seek first instead of forcing a full parse (core/transcript).
        open()'s default closefd=True means the descriptor is closed here.

        `meta` marks an entry the HOST synthesized rather than a human or the model:
        Claude Code sets `isMeta` on a materialized skill/command body, the
        local-command caveat and image placeholders. They carry the `user` role and a
        real text block, so only this flag separates them from a genuine turn — the
        sweep drops them so the extractor never mines our own instructions as session
        facts. Hook-injected context (SessionStart card, per-prompt recall) never
        reaches here at all: it lands on `type:"attachment"` lines that carry no
        `message`, so they yield no text.

        `sidechain` marks SUBAGENT traffic (`isSidechain`) — a nested agent's turns,
        not this session's conversation. Current Claude Code versions file those in
        their own transcripts, so the flag is False throughout a live main transcript;
        it is surfaced because older versions inlined them and a consumer must not
        mistake a subagent's prose for what the user just replied to.

        Tolerant of any malformed line; never raises."""
        try:
            fh = open(path, "r", encoding="utf-8")
        except OSError:
            return
        with fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message") or {}
                role = msg.get("role") or obj.get("type") or ""
                content = msg.get("content")
                texts, tool_uses, tool_results = [], [], []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text":
                            texts.append(str(item.get("text") or ""))
                        elif item.get("type") == "tool_use":
                            tool_uses.append({"name": str(item.get("name") or ""),
                                              "input": item.get("input") or {}})
                        elif item.get("type") == "tool_result":
                            tool_results.append(self._tool_result_text(obj, item))
                model = str(msg.get("model") or "")
                yield {"role": role, "texts": texts, "tool_uses": tool_uses,
                       "tool_results": tool_results,
                       "model": "" if model.startswith("<") else model,
                       "effort": str(obj.get("effort") or ""),
                       "meta": bool(obj.get("isMeta")),
                       "sidechain": bool(obj.get("isSidechain"))}
