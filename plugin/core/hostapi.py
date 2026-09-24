#!/usr/bin/env python3
# The PORT boundary for multi-host support (topic 90ae46be).
#
# `core/` holds the host-AGNOSTIC memory logic; a `hosts/<name>.py` adapter
# maps one AI-coding-agent's hook I/O onto these types. The hard rule that keeps
# the hosts (present and future) from drifting: **core branches on CAPABILITY
# FLAGS, never on a host name.** A CI grep over core/ forbids host identifiers
# (even in comments, so the gate stays a trivial grep), so a capability a new host
# lacks (e.g. a distinct session-end event) automatically takes the documented
# fallback rather than a forgotten host-name branch.
#
# Pure stdlib; no runtime deps (clients need no Bun/Node).

from dataclasses import dataclass, field, replace
from typing import Any, Optional, Protocol


# ---- Normalized hook event (what every core handler reads) -------------------
@dataclass
class Event:
    """One hook firing, normalized across hosts. A host adapter's parse_event()
    fills this from the host's stdin payload; core handlers read ONLY these
    fields (plus `raw` as an escape hatch), never the host's native shape."""

    event: str                      # canonical event name: session_start | user_prompt | pre_tool | post_tool | stop | pre_compact
    session_id: str = ""
    cwd: str = ""                   # host-resolved working dir (pre git-canonicalization)
    launch_cwd: str = ""            # the directory the USER started this host process in, when the
                                    # adapter can observe it AND the host may run the session under a
                                    # DIFFERENT one. A host that RESTORES a saved session pins `cwd` to
                                    # the directory that session was BORN in (measured #527: a resume
                                    # reports the birth dir and an explicit cwd flag does not override
                                    # it), so every cwd-derived decision — which profile answers, which
                                    # assets were discovered on disk — silently follows the old repo
                                    # while the user sits in a new one. Core only ever COMPARES it
                                    # against `cwd` to say so out loud; it is never resolved,
                                    # persisted or sent. "" = unobservable, and core then says
                                    # nothing rather than guessing.
    model: Optional[str] = None     # the SESSION model this hook fired under, as the HOST
                                    # names it, lower-cased and verbatim — NO provider prefix
                                    # added or stripped. Some hosts report a bare id (e.g.
                                    # "gpt-5.5"); others a "provider/model" string. This is what
                                    # a model-scoped rule/register condition binds to. DISTINCT
                                    # from the CAPTURE model (each adapter pins its own for the
                                    # detached bookkeeping turn) — that is a property of the
                                    # capture, not of the session it records. None when the host
                                    # does not report a model at hook time; every consumer then
                                    # OMITS the `model` param.
    source: str = ""                # SessionStart only: startup | resume | clear | compact
    prompt: str = ""                # UserPromptSubmit only
    tool_name: str = ""             # tool events: canonical (Bash / Edit / Write / apply_patch / mcp__server__tool)
    tool_input: dict = field(default_factory=dict)
    tool_response: Any = None
    edited_path: str = ""           # tool events: the file an edit TOUCHED, normalized
                                    # across hosts. The host adapter fills this whenever
                                    # a tool edits a file — lifting it from whatever the
                                    # host carries (a structured path field, or a patch
                                    # header) — and leaves it "" for every non-edit event.
                                    # Core derives the file-edit triggers from THIS field,
                                    # never from a host's edit-tool vocabulary, so a host
                                    # whose edits carry no structured path still fires them.
    transcript_path: str = ""
    raw: dict = field(default_factory=dict)   # the untouched host payload (escape hatch; avoid in core)


# ---- Normalized hook output (what every core handler returns) ----------------
@dataclass
class Output:
    """A host-neutral hook result. The host adapter's emit() renders it to the
    host's wire format. `None` from a handler means "no output" (exit 0 silent).
    Only capabilities the target host supports are honored — see Capabilities."""

    context: Optional[str] = None       # inject as model-visible additionalContext
    system_message: Optional[str] = None  # user-visible note (not model context)
    deny: bool = False                  # block the tool/prompt (PreToolUse / UserPromptSubmit)
    deny_reason: str = ""
    reload_assets: bool = False         # ask the host to re-scan materialized assets in-session
                                        # (hosts without in-session asset reload ignore it)
    status_badge: Optional[str] = None  # the topic badge this host should show in its own status
                                        # surface. None = leave whatever is there alone; "" = CLEAR
                                        # it; any other text = show exactly that. Honored only where
                                        # caps.status_badge_from_result, which implies
                                        # caps.custom_statusline. A host whose status line is a
                                        # COMMAND IT POLLS composes its own badge inside that
                                        # command and ignores this field; nothing replaces that path.

    @staticmethod
    def inject(text: str) -> "Output":
        return Output(context=text)


# ---- Host capabilities (core branches on THESE, not on host names) -----------
@dataclass(frozen=True)
class Capabilities:
    has_session_end: bool                       # a distinct end-of-session event exists (vs only per-turn Stop)
    session_end_timeout_cap: Optional[int]      # host-enforced maximum in seconds; None = no host cap
    continues_from_stop_context: bool           # Stop additionalContext can force another model turn
    injects_on_pre_tool: bool                   # PreToolUse additionalContext reaches the model
    drops_sessionstart_context_on_compact: bool # SessionStart additionalContext is dropped on a compact-sourced fire
    signals_completed_compaction: bool          # this host tells us a compaction COMPLETED — a
                                                # compact-sourced SessionStart, or a bridge that
                                                # subscribes to the host's post-compaction event and
                                                # dispatches one. That report is where the session's
                                                # server-side delivery history is invalidated (see
                                                # core/compaction). False means the ONLY compaction
                                                # signal is the PRE-compaction hook, which is a
                                                # request and not an outcome (a host may let it be
                                                # canceled): such a host invalidates there instead,
                                                # trading one redundant re-delivery after a canceled
                                                # compaction for never missing a real one
    custom_statusline: bool                     # host lets a plugin render a custom status item
    status_badge_from_result: bool              # WHERE that status item's text comes from, which decides
                                                # which mechanism carries the topic badge. True: the host
                                                # hands the running extension a status surface, so a hook
                                                # RESULT carries the text (Output.status_badge) and nothing
                                                # the user owns is edited. False: the status line is a
                                                # COMMAND the host POLLS, declared in the user's own config
                                                # — the badge is composed inside that command, the field is
                                                # ignored, and the legacy installer hook is what wires it.
    supported_asset_kinds: frozenset            # subset of {skill, command, agent, output_style}
    headless_capture: bool                      # host ships a headless CLI we can drive for one silent
                                                # post-session bookkeeping turn (see core/detached). A host
                                                # without one still sweeps; it just never gets the detached lane
    inline_context_limit_chars: int             # per-prompt injection BUDGET in CHARACTERS: core sizes the
                                                # assembled additionalContext against it and raises a user-facing
                                                # over-limit notice when exceeded (#494/#519). UNIFORM across hosts
                                                # by policy — a frugality cap so a large envelope never needlessly
                                                # consumes the model's context window — NOT a per-host measured
                                                # cliff. What a host DOES past it is `side_files_over_inline_limit`.
                                                # 0 disables measurement for the host (no budget, no verdict).
    substitutes_session_id: bool                # host expands its session id into a command the model runs (so a
                                                # command-side CLI helper can self-scope by session id); a host
                                                # that does NOT gets the session id only on the hook payload, so
                                                # session-id-keyed writes must be driven by a hook (see core/switch)
    reports_model: bool                         # this host surfaces the SESSION model on its hook
                                                # payloads (or its bridge can read it synchronously at
                                                # dispatch), so Event.model is populated and the request
                                                # hooks forward a `model` param. A host that cannot takes
                                                # the documented no-model fallback (the param is omitted
                                                # and model-scoped rules do not ride); host_requirements
                                                # warns when a host that CAN report one did not this session
    side_files_over_inline_limit: bool          # past inline_context_limit_chars this host SIDE-FILES the payload
                                                # and hands the model only a short preview, so exceeding the budget
                                                # here means silent DATA LOSS (measured #519). False = the host
                                                # delivers the full context verbatim (measured #519: a 900k-char
                                                # append rode whole) — exceeding only wastes context window, it
                                                # loses nothing. NO default: every adapter must STATE it, because a
                                                # silent-data-loss verdict is too dangerous to inherit unstated.
                                                # core reads this to word the over-limit notice honestly.
    live_reload_asset_kinds: frozenset          # the asset KINDS this host re-scans WITHIN the running session
                                                # when a hook emits Output(reload_assets=True). A subset of
                                                # supported_asset_kinds, and usually SMALLER: a host may load
                                                # four kinds at startup and expose a rescan for one of them.
                                                # EMPTY means the directive is ignored — materializing still
                                                # works, the session just keeps the set it started with. core
                                                # words the reload report from THIS (see core/reload), so a
                                                # host never promises a rescan it does not perform.
    manual_reload_hint: str                     # what the USER can type in this session to make the host
                                                # rescan what live_reload_asset_kinds leaves out (e.g. a
                                                # built-in reload command a plugin cannot invoke itself).
                                                # "" = nothing short of a new session. Host-shaped wording,
                                                # host-owned: core quotes it and never composes one.


# ---- The Host port -----------------------------------------------------------
class Host(Protocol):
    name: str
    caps: Capabilities
    login_hint: str

    def parse_event(self, canonical_event: str, raw_stdin: str) -> Event: ...
    def emit(self, canonical_event: str, output: Optional[Output]) -> None: ...
    def config_home(self) -> str: ...
    def requirements_notice(self) -> str: ...
    def asset_target_dir(self, kind: str) -> Optional[str]: ...
    # The per-CWD base dir a host materializes assets under, or None to use the
    # fixed config-home dirs of asset_target_dir(). A host whose native discovery
    # is project-scoped (a per-repo config dir) returns a cwd-derived base so concurrent
    # profiles in different repos never share one target dir; cwd="" (a session
    # with no repo, e.g. a profile override) returns None -> config-home dirs.
    def asset_target_base(self, cwd: str) -> Optional[str]: ...
    def asset_is_directory(self, kind: str) -> bool: ...
    def asset_legacy_target_dirs(self, kind: str) -> list[str]: ...
    def asset_legacy_names(self, kind: str, slug: str) -> list[str]: ...
    def render_asset(self, kind: str, slug: str, body: str) -> str: ...
    def render_asset_files(self, kind: str, slug: str, body: str) -> dict[str, str]: ...
    def iter_transcript(self, path): ...

    # ---- detached capture (core/detached) ------------------------------------
    # A host that ships a headless CLI can run one silent bookkeeping turn after
    # the session ends. Both the invocation and the litter it leaves are entirely
    # host-shaped, so they live behind these two methods; core knows only that it
    # gets something to run and a list of paths to remove afterwards.
    #
    # capture_command returns (argv, env_overrides). The env half is not optional
    # decoration: our bundled MCP config points at the plugin through a variable
    # the HOST defines, and the detached run is not a process the host launched, so
    # nothing has defined it. The adapter names its own variable; core just passes
    # the plugin root in and merges whatever comes back.
    #
    # There is deliberately NO `model` here. The capture model is a property of the
    # capture, not of the session it records, and which model names are even legal
    # is host-shaped — so each adapter pins its own.
    def capture_command(self, session_id: str, effort: str,
                        mcp_config: str, plugin_root: str) -> Optional[tuple]: ...
    def capture_artifacts(self, session_id: str) -> list: ...

    # capture_receipt folds the child's raw stdout down to the run marker core
    # stores. Core owns WHY the receipt exists (core/detached._log_path) but only
    # the host knows what its own binary prints there, and the two hosts print
    # very different things: one emits a single result envelope, the other a live
    # event stream that carries the session's own content and must be reduced
    # before any of it reaches disk. Returns the exact bytes to store.
    def capture_receipt(self, stdout: bytes) -> bytes: ...


def with_tool(event: Event, **changes) -> Event:
    """Small helper for adapters/tests: return a copy of `event` with fields set."""
    return replace(event, **changes)
