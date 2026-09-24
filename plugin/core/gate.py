#!/usr/bin/env python3
# PreToolUse handler (host-agnostic) — re-surface the rules governing the action
# about to run. This is the single lifecycle implementation dispatched by entry.py
# for every host.
#
# A standing rule injected at session start goes stale and loses the salience race
# to whatever the harness injects fresher and closer to the action; this handler
# fires right before a tool runs, DESCRIBES the call to the server, and injects
# whatever rules the server says govern it — so the governing rule is the LAST
# thing the agent sees before acting. Soft (it informs, never blocks).
#
# #297 — THIS CLIENT NO LONGER CLASSIFIES. It used to map the call to keys from a
# hardcoded vocabulary (`slack_post`, `notion_write`, `pod_console`, …) and send
# the verdict. Those keys were OUR integrations: a customer on Linear, Vercel and
# Stripe produced none of them, and adding theirs meant editing this file and
# cutting a plugin release. A multi-tenant SaaS cannot enumerate its customers'
# tools. So the mapping is GONE and this handler forwards a DESCRIPTION instead —
# the canonical tool name, the normalised edited path, and a scrubbed, truncated
# slice of the tool input — leaving every judgement to the server, which matches
# against the rules' own conditions and the tools the profile actually uses.
#
# #376 adds two more OBSERVABLES to that description — the working directory and
# the branch — and changes nothing about who decides. A rule's conditions name the
# targets they bind to; a target this request cannot see is UNOBSERVABLE and drops
# the rule off the lane, so what the client owes the server is a truthful account
# of what it can see, and silence about the rest. Still no classification here.
#
# The tool name it forwards is the NORMALIZED event.tool_name (the host has
# already mapped its native vocabulary onto the canonical names), so one shape
# serves every host. Whether a pre-tool injection reaches the model is a
# CAPABILITY (caps.injects_on_pre_tool), not a host name: a host that drops
# pre-tool context takes the documented no-op fallback.

import json
import re
from collections import namedtuple

from core import api, profile as profile_mod, sweepturn
from core.hostapi import Event, Host, Output


# How much of the tool input travels. The server matches a rule's condition
# against this text, so it needs enough to recognise the act ("git commit -m …",
# the Slack channel, the PR title) and no more — this rides the critical path
# before every gated call, and a whole tool_input would be both slow and a
# needless exfiltration surface.
INPUT_SLICE_MAX_CHARS = 300

# Keys whose VALUES never travel, whatever the tool. Matching is on the key name,
# so an unknown tool's secret-shaped field is redacted without anyone enumerating
# that tool.
_SECRET_KEY = re.compile(
    r"(token|secret|password|passwd|api[-_]?key|authorization|auth|credential|cookie|session[-_]?id|private[-_]?key)",
    re.I,
)

REDACTED = "[redacted]"

# TESTING ONLY THE TOP-LEVEL KEYS WAS ALSO NOT ENOUGH. A structured tool argument
# is serialised with json.dumps, and JSON puts a quote between a key and its colon
# — which breaks the flat `NAME=…` / `NAME: …` value patterns below. So a short or
# custom-format credential nested under a benign top-level key (`{"config":
# {"api_key": "…"}}`) passed BOTH layers and travelled verbatim; only the
# self-anchored well-known shapes (JWT, AKIA, sk-, gh*_) still caught it.
#
# The fix is to run the key test over EVERY key at EVERY depth, before anything is
# serialised, rather than hoping a value pattern recognises quoted JSON.
SECRET_NESTING_MAX_DEPTH = 8


def _drop_secret_keys(value, depth=0):
    """Recursively remove every secret-named field, at any depth, from a container."""
    if isinstance(value, dict):
        if depth >= SECRET_NESTING_MAX_DEPTH:
            # Deeper than we are willing to walk, so we can no longer prove this
            # branch holds no secret-named key. Unprovable is treated as secret.
            return REDACTED
        return {
            key: _drop_secret_keys(item, depth + 1)
            for key, item in value.items()
            if not _SECRET_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        if depth >= SECRET_NESTING_MAX_DEPTH:
            return REDACTED
        return [_drop_secret_keys(item, depth + 1) for item in value]
    return value


# KEY-NAME MATCHING IS NOT ENOUGH, and assuming it was is how this leaked. The two
# most frequently gated inputs are Bash `command` and Write `content` — free text
# under an innocuous key — so a credential typed into a command or written into a
# file passed _SECRET_KEY untouched and travelled verbatim. And it does not stop at
# our server: /rules/triggered's vector leg embeds this slice, and the embed path
# fails over from the local model server to DeepInfra, so "off the process" can be
# "off the network, to a third party".
#
# So values are scanned too. Over-redaction is the safe direction here: the slice
# only ever feeds a fuzzy match, so losing a token to a false positive costs a
# little ranking signal, while keeping one costs a credential.
_SECRET_VALUE = re.compile(
    r"""(
        # A PEM block is consumed WHOLE, to its footer or to the end of the slice.
        # Redacting only the header would leave the key material sitting behind it.
        -----BEGIN[^-]{0,40}PRIVATE\ KEY-----[\s\S]*?
        (?:-----END[^-]{0,40}PRIVATE\ KEY-----|$)
        # An Authorization header carries its scheme AND its credential, two
        # whitespace-separated tokens. Listed before the generic assignment below,
        # which would otherwise match `Authorization: Bearer` and stop at the space,
        # leaving the actual token in the clear.
      | authorization"?\s*[:=]\s*\S+(?:\s+\S+)?
      | (?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}
      | eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}   # JWT
      | AKIA[0-9A-Z]{16}                            # AWS access key id
      | sk-[A-Za-z0-9_-]{16,}                       # OpenAI / Anthropic style
      | gh[pousr]_[A-Za-z0-9]{20,}                  # GitHub PAT / OAuth
      | github_pat_[A-Za-z0-9_]{20,}
      | glpat-[A-Za-z0-9_-]{16,}                    # GitLab PAT
      | xox[abprs]-[A-Za-z0-9-]{10,}                # Slack
      | ://[^/\s:@]+:[^/\s@]+@                      # credentials inside a URL
      # Any assignment whose NAME looks secret, in free text: FOO_TOKEN=…,
      # "api_key": "…", password: …. This is the key-name test applied INSIDE a
      # value, which is where the Bash/Write leak actually lived. The optional
      # quote before the separator is what lets it also fire on JSON that arrives
      # as a STRING — the one nesting shape _drop_secret_keys cannot see into. The
      # name runs are LENGTH-BOUNDED because two unbounded `[\w.-]*` around a
      # literal is quadratic on word-dense text (41 KB measured at 2 s, and this
      # runs on the hook that blocks before the tool); 64 covers any real name.
      | [\w.-]{0,64}(?:token|secret|password|passwd|api[-_]?key|auth|credential|private[-_]?key)[\w.-]{0,64}
        "?\s*[=:]\s*"?[^\s"']+
      | \b[A-Fa-f0-9]{32,}\b                        # long hex blob
      | \b[A-Za-z0-9+/]{40,}={0,2}\b                # long base64 blob
    )""",
    re.I | re.X,
)


# The redactor runs BEFORE any truncation, so unbounded it scans the WHOLE field —
# a Write `content` is an entire file body, with no upstream bound — on the hook
# that blocks before every tool call, to produce text past char 300 that is then
# thrown away. So the scan gets a window. It is several times the emitted slice so
# that redaction shrinking the head still leaves 300 chars to emit.
REDACT_SCAN_MAX_CHARS = INPUT_SLICE_MAX_CHARS * 4

# A secret straddling the far edge of that window would otherwise have its head
# emitted unredacted, so the trailing partial token goes with the tail it was cut
# from. A window with no whitespace at all IS one token and drops whole — over-
# redaction, the safe direction here.
_TRAILING_TOKEN = re.compile(r"\S*\Z")


def _redact(text):
    """Strip secret-SHAPED substrings from a value, whatever its key was called."""
    if len(text) > REDACT_SCAN_MAX_CHARS:
        text = _TRAILING_TOKEN.sub("", text[:REDACT_SCAN_MAX_CHARS])
    return _SECRET_VALUE.sub(REDACTED, text)


# A shell command travels as its leading VERBS ONLY — never its arguments.
#
# The gate has to know WHAT KIND of action is about to happen (`git commit`,
# `gh pr create`, `terraform apply`); it does not need the flags, and the flags are
# where the credentials live. Redacting known token shapes out of a full command
# line would still be a blocklist, and a blocklist over arbitrary shell text is a
# losing game — an in-house deploy script's `--key` argument matches no published
# pattern. Taking only the verbs inverts that into an allowlist: nothing that is
# not a leading bare word can travel at all.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Deliberately narrow: letters, digits and the punctuation a real command name
# uses (`./deploy.sh`, `docker-compose`). A quote, a $VAR, a URL, a pipe or a
# redirect all fail it — which is exactly where arguments begin. A LEADING dash
# fails it too, so a flag ends the scan: `-m` is where `git commit -m "…"` stops,
# and without that rule the flag itself rides along and the next token is its
# value.
_BARE_WORD = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$")
_COMMAND_WRAPPERS = frozenset({"sudo", "env", "command", "time", "nice", "nohup", "exec"})

# THREE, because the deepest act the matcher recognises is three words long
# (`gh pr create`, `gh pr review` — see COMMAND_ACTS in services/voice-detect.ts).
# Two would silently break PR-body voice detection; more only widens the surface
# without naming any act we match on.
COMMAND_VERB_MAX_TOKENS = 3

# A CHAINED command hides its real verb behind the first one. Scanning only the
# leading run of the whole line makes `cd repo && git commit -m …` travel as
# `cd repo`, and the act that matters never reaches the server — the pre-#297
# `trigger_keys()` searched the WHOLE line (`re.search(r"\bgit\s+(commit|push)\b")`)
# and its own comment recorded that `cd x && git push` was covered. Losing it
# breaks silently in both directions: the server's condition matching loses the
# mutating verb, and COMMAND_ACTS in services/voice-detect.ts tests these exact
# patterns against this slice, so the register governing a commit message stops
# firing — and a register that never fires is indistinguishable from having none.
#
# So each top-level segment is scanned separately, under the SAME allowlist. What
# travels is still only leading bare words; there are just now up to
# COMMAND_SEGMENT_MAX of those runs instead of one.
_COMMAND_SEGMENT_MAX = 8


# Splitting is QUOTE-AWARE, which is the difference between describing the command
# and leaking its payload: the most frequently gated command of all is
# `git commit -m "…"`, and a `;` or `|` inside that message is prose, not a
# separator. Splitting on it would put the message's own words at the head of a
# segment, where the verb scan would forward them.
_COMMAND_SEPARATORS = ";|"
_COMMAND_SEPARATOR_PAIRS = ("&&", "||")


def _split_segments(command):
    """The command's top-level segments, split on shell separators outside quotes."""
    segments = []
    current = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif command[index : index + 2] in _COMMAND_SEPARATOR_PAIRS:
            segments.append("".join(current))
            current = []
            index += 2
            continue
        elif char in _COMMAND_SEPARATORS:
            segments.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    segments.append("".join(current))
    return segments


def _segment_verbs(segment):
    """The leading verbs of ONE segment, with arguments dropped entirely."""
    verbs = []
    for token in segment.split():
        if not verbs and (_ENV_ASSIGN.match(token) or token.lower() in _COMMAND_WRAPPERS):
            # `AWS_SECRET_ACCESS_KEY=… aws s3 ls` and `sudo gh pr create`: skip the
            # prefix so the real verb is still found. The assignment itself — the
            # classic inline-secret shape — never enters the list.
            continue
        if not _BARE_WORD.match(token):
            break
        verbs.append(token)
        if len(verbs) == COMMAND_VERB_MAX_TOKENS:
            break
    return " ".join(verbs)


def command_verbs(command):
    """The leading verbs of every segment of a shell command, arguments dropped."""
    # Rejoined with an explicit separator so no act pattern can match ACROSS a
    # boundary — `foo git && commit bar` must not read as `git commit`.
    found = (_segment_verbs(s) for s in _split_segments(str(command))[:_COMMAND_SEGMENT_MAX])
    return " && ".join(verbs for verbs in found if verbs)


def _scrub(value):
    """One tool_input value, flattened to a short string with secrets removed."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    try:
        return json.dumps(_drop_secret_keys(value), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""


# Fields that ARE a shell command, whatever tool carries them. Keyed on the field
# name as well as the tool name so an MCP server exposing its own `command` field
# gets the same treatment without being enumerated here.
_COMMAND_KEYS = frozenset({"command", "cmd"})


# The slice is a flat, UNESCAPED `key=value` join, so any value can contain
# `key=` and nothing downstream can tell that apart from a real field. Every
# consumer that needs one FIELD therefore gets it as its own value here, from the
# loop that already knows which key it came from — re-deriving it from the joined
# string is guesswork by construction, and it is guesswork that failed: a file
# whose `content` merely contained `command=gh pr create` was read as the command
# and resolved a communicative act for a call that runs no command at all.
#
# The slice keeps its shape. It feeds the server's cheap textual match and its
# embedding, which want the whole description and are fuzzy by design; only the
# consumers that need STRUCTURE take the structured fields.
CallDescription = namedtuple("CallDescription", ("slice", "command"))


def describe_call(tool_input, tool_name=""):
    """A scrubbed description of a tool call: the flat slice, and its shell command.

    Three layers build the slice, in order: a field is dropped when its KEY looks
    secret — at ANY depth, nested keys included — a shell command is reduced to its
    leading VERBS, and whatever survives is scanned for secret-shaped VALUES. The
    last two are what key-name matching alone missed; the depth is what top-level
    keys alone missed.

    Order-stable so the same call always produces the same text — the server's
    cheap textual match and its embedding both key on it, and a dict-order shuffle
    would silently change what a rule matches against.

    `command` is the same verbs the slice carries, handed over separately rather
    than left to be found inside it again. It is NOT truncated with the slice: what
    act a call performs must not depend on where the 300-char cut happens to land."""
    if not isinstance(tool_input, dict):
        return CallDescription(_redact(_scrub(tool_input))[:INPUT_SLICE_MAX_CHARS], "")
    # Bash's own field is literally named `command`, so the key test covers it; the
    # tool test is the backstop for a host that names it something else, where the
    # single string field IS the command line. A NAMED command field always wins
    # over that backstop, whatever the sort order put first.
    is_shell = tool_name.strip().lower() == "bash"
    parts = []
    named_command = ""
    shell_fallback = ""
    for key in sorted(tool_input):
        if _SECRET_KEY.search(str(key)):
            continue
        text = _scrub(tool_input[key]).strip()
        if not text:
            continue
        is_named = str(key).lower() in _COMMAND_KEYS
        is_command_field = is_named or (is_shell and key != "description")
        if is_command_field:
            text = command_verbs(text)
        text = _redact(text)
        if not text:
            continue
        if is_named and not named_command:
            named_command = text
        elif is_command_field and not shell_fallback:
            shell_fallback = text
        parts.append(f"{key}={text}")
        if sum(len(p) for p in parts) > INPUT_SLICE_MAX_CHARS:
            break
    return CallDescription(
        " ".join(parts)[:INPUT_SLICE_MAX_CHARS],
        named_command or shell_fallback,
    )


def input_slice(tool_input, tool_name=""):
    """The flat slice alone, for callers that do not need the structured fields."""
    return describe_call(tool_input, tool_name).slice


def handle(event: Event, host: Host):
    """Return an Output (inject the governing rules) or None (silent)."""
    # A host that does NOT surface pre-tool additionalContext to the model gets the
    # documented no-op fallback — nothing to inject means no reason to query.
    if not host.caps.injects_on_pre_tool:
        return None

    tool_name = event.tool_name or ""
    if not tool_name:
        return None

    # #549 — CONTAINMENT, BEFORE ANY RULE WORK. While a forced self-consolidation
    # turn is in flight, this conversation is one nobody asked to continue: the
    # model re-entered it to write records, with every unfinished thread from
    # before still in view. Only the capture's own calls run (core/sweepturn.py);
    # everything this gate intercepts — the shell, file edits, the communicative
    # MCP writes — is refused with a reason that says what the turn IS, so the
    # block reads as the end of its business rather than an obstacle to route
    # around. Checked before the rules query because a call that will not run
    # needs no rules, and this path must not acquire a network dependency.
    if not sweepturn.allows(tool_name) and sweepturn.armed_instruction(event.session_id):
        return Output(deny=True, deny_reason=sweepturn.DENIAL)

    described = describe_call(event.tool_input or {}, tool_name)
    params = {"tool": tool_name}
    # AN EMPTY SLICE IS NOT AN OBSERVATION, so it is omitted rather than sent blank
    # (#376). A call whose whole input reduces to nothing does exist — a host whose
    # file edit carries a unified diff under a `command` field leaves the verb scan
    # with no leading bare word, so the slice comes back "" for a real edit with a
    # real payload. Sent as an empty param that reads as observed-and-blank, an
    # `input contains …` leaf resolves FALSE: we would be asserting the input does
    # not contain something we never actually saw. Omitted, it resolves
    # unobservable — "cannot tell" — which is the truth, and which drops the rule
    # instead of wrongly clearing it. Omission is already the signal for every other
    # value this handler forwards; this makes `input` obey the same rule.
    if described.slice:
        params["input"] = described.slice
    if event.edited_path:
        params["path"] = event.edited_path
    # The session id keys the server-side gate CADENCE (#127): the matched
    # non-pinned rules ride on a rule set's first firing in this session and
    # periodically after, pinned-only on the firings in between. Without it the
    # server falls back to every matched rule on every call.
    if event.session_id:
        params["session"] = event.session_id
    # Scope to the session's profile the same way per-prompt recall does, with
    # precedence: /switch-profile session override > NEURONZAI_PROFILE env > cwd.
    # An explicit profile (override or env) wins via X-Profile; else pass the cwd so
    # the middleware resolves the same profile the bootstrap used. Under an override
    # resolve() returns cwd="" (no anchor) so a re-scoped session sees the switched
    # profile's rules.
    prof, cwd = profile_mod.resolve(event.session_id, event.cwd, api.ENV_PROFILE)
    if not prof and cwd:
        params["cwd"] = cwd

    # #376 — TWO MORE OBSERVABLE TARGETS. A rule's conditions can now bind to the
    # working directory and the branch, and a leaf whose target the request cannot
    # see is UNOBSERVABLE, which drops that rule off this lane entirely. So a branch
    # rule is dead weight until the gate says which branch this is; the server holds
    # no clone and has no other way to find out.
    #
    # `workdir` IS ITS OWN PARAM AND NOT THE `cwd` ABOVE. Those are two different
    # jobs wearing one word. `cwd` is the profile-ROUTING leg: the middleware feeds
    # it to resolveOrCreateProfile and keys the (profile, cwd) topic-mode anchor on
    # it, which is exactly why a /switch-profile session sends none — nothing may
    # map the directory to the profile it was switched to. Overloading it as the
    # observable would make the cwd target unobservable in precisely the switched
    # case, or fix that by breaking the invariant. `head` (guidance.py) already set
    # this precedent: an observe-only value gets its own name, resolves nothing and
    # is never persisted, so it can create no route and no anchor.
    #
    # Reuse of the RESOLVED cwd is what keeps this free: canonical_cwd shells out to
    # git, and resolve() has already paid for that call in every branch that returns
    # a cwd. The fallback runs only under an override, where resolve() returned
    # early and paid nothing — so the spawn count per gate call is unchanged.
    workdir = cwd or profile_mod.canonical_cwd(event.cwd)
    if workdir:
        params["workdir"] = workdir
    # Read off event.cwd, never `workdir`: canonical_cwd maps a linked worktree onto
    # the MAIN worktree's root, whose HEAD is a DIFFERENT branch. Resolving there
    # would report the wrong branch for every session running in a worktree. Never
    # raises, never spawns a process, never touches the network — "" (param omitted)
    # whenever there is no branch to name, which the server reads as unobservable.
    branch = profile_mod.git_branch(event.cwd)
    if branch:
        params["branch"] = branch
    # The SESSION model this call is running under, so a model-scoped rule condition
    # can bind to it — same omit-when-absent contract as branch/workdir above. The
    # value is the id exactly as the host names it (the adapter already lower-cased
    # it); we add and strip no provider prefix. Absent (host reports none) → param
    # omitted, which the server reads as unobservable so a model leaf does not ride.
    if event.model:
        params["model"] = event.model
    # NEITHER VALUE IS PASSED THROUGH _redact, deliberately. They are structural
    # LOCATION values, the same class as `path` and `cwd` above, which have always
    # travelled raw — none of them is derived from tool_input, so no input can reach
    # the wire around the redactor and the scrubbing contract is untouched. Running
    # them through it would also break them: the redactor is tuned for free text and
    # is deliberately over-eager, and its long-base64 rule matches an ordinary deep
    # path (`/`, letters and digits are all base64 characters), so measured against
    # real paths `/var/lib/containers/storage/overlay/abcdef/merged/usr/share`
    # becomes `/[redacted]`. A cwd target that silently stops matching for every
    # project with a deep path is a worse outcome than the leak it would prevent —
    # a directory name and a branch name are not credentials.

    rules = api.get_json("/api/memory/rules/triggered", params=params, profile=prof,
                         where="memory_gate")

    # VOICE BACKSTOP (#179). The primary lane is prompt time — for a posting tool the
    # message text IS the tool_input, so anything injected here arrives after the text
    # was composed. This catches the case the prompt-time detector missed. Its own
    # endpoint, not a widened /rules/triggered shape, which an older plugin parses
    # positionally as a bare array.
    # RULE ASK backstop (#178). A TOOL-TAGGED proposal surfaces at its action —
    # the same moment its rule would have fired, which is when the user can judge
    # it. Own endpoint, not a widened /rules/triggered (older plugins parse that
    # response positionally as a bare array).
    ask_params = {"tool": tool_name}
    if event.session_id:
        ask_params["session"] = event.session_id
    if "cwd" in params:
        ask_params["cwd"] = params["cwd"]
    ask = api.get_json("/api/memory/rules/ask", params=ask_params, profile=prof,
                       where="memory_gate_ask") or {}
    ask_question = (ask.get("question") or "").strip() if isinstance(ask, dict) else ""
    ask_rule = (ask.get("rule") or {}) if isinstance(ask, dict) else {}
    ask_block = (
        "<rule-ask>\n"
        "[A pending rule proposal covers the action you are about to take. Put it to the user in "
        "one line when you next speak, and call resolve_rule_ask(id=\""
        + str(ask_rule.get("id") or "")
        + "\", outcome=accepted|declined|deflected) with their answer. Silence is `deflected`.]\n"
        + ask_question
        + "\n</rule-ask>"
        if ask_question
        else ""
    )

    # The voice gate resolves an ACT: the tool name carries it for an MCP call, and
    # the COMMAND carries it for a shell one (a `git commit` is a Bash command, so
    # its name says nothing). It gets the command as its own value, never the whole
    # slice — the slice is a flat key=value join whose free-text values can contain
    # `command=`, and the gate has no way to tell that from the real field. Sending
    # only what this endpoint actually reads also keeps the rest of the input off
    # a second network path.
    voice_params = {"tool": tool_name}
    if described.command:
        voice_params["command"] = described.command
    if "cwd" in params:
        voice_params["cwd"] = params["cwd"]
    voice = api.get_json("/api/voice/gate", params=voice_params, profile=prof,
                         where="memory_gate_voice") or {}
    # #561 — THE VOICE IS A POINTER HERE TOO. The server sends which layers apply
    # and the exact call that composes them; the claims themselves are NOT in the
    # payload. `instruction` is composed server-side (one definition, same as the
    # per-prompt lane) and rendered VERBATIM — this hook words nothing itself.
    voice_instruction = (voice.get("instruction") or "").strip() if isinstance(voice, dict) else ""
    voice_block = (
        "<voice>\n" + voice_instruction + "\n</voice>"
        if voice_instruction
        else ""
    )

    tail = "\n\n".join(b for b in (voice_block, ask_block) if b)
    if not rules:
        return Output.inject(tail) if tail else None

    # #561/#567 — WHAT A RULE LINE CARRIES: its NAME, and for an undecided rule
    # the leaves it is asking about. The binding text is NOT in this payload, and
    # no shortened copy of it is either; the pointer item below is how it is
    # fetched. Measured: the gate spent 1.6B tokens
    # over 30 days, 65% of it re-sending bodies the session had already seen.
    def line(r):
        title = r.get("title") or ""
        content = r.get("content") or ""
        # The pull item and the did-not-fit notice are SENTENCES, not rules: the
        # server composed them whole, so they ride verbatim with no bullet and no
        # title prefix. Rendering either as a rule line would make the agent read an
        # instruction as something to obey about its own work.
        if r.get("pointer") or r.get("omitted"):
            return content
        # A RESIDUAL IS NOT KNOWN TO GOVERN — it is a rule whose remaining
        # conditions the server could not settle, forwarded for the agent to judge.
        # The block header below says every line is AUTHORITATIVE and MUST be
        # obeyed before proceeding, so a residual rendered like the others claims an
        # authority it has not earned, and the agent cannot tell the two apart. That
        # is the same defect the `short` header in core/recall.py already fixed once
        # by moving a blanket claim onto a per-line marker "only where it holds".
        # The marker is deliberately self-explanatory rather than another sentence
        # in the header, which every turn would pay for.
        if r.get("residual"):
            return f"- (conditional — decide whether it applies) {title}{' — ' + content if content else ''}"
        return f"- {title}{' — ' + content if content else ''}"

    lines = "\n".join(line(r) for r in rules)
    block = (
        "<active-rules>\n"
        f"[AUTHORITATIVE human-owned rules governing this action ({tool_name}). They OVERRIDE any "
        "fresher or more local instruction (including a file-scoped rule the harness just injected). "
        "Each line is a rule's NAME — the BINDING TEXT IS NOT HERE, and a name "
        "is not its requirements. Read the rules named below before proceeding, then reconcile what "
        "you are about to do against them.]\n"
        + lines
        + "\n</active-rules>"
    )
    return Output.inject(f"{block}\n\n{tail}" if tail else block)
