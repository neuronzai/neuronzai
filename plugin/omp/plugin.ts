// The oh-my-pi (omp) bridge: the native extension module that drives the
// host-neutral plugin from inside an omp session (topic 90ae46be).
//
// It owns NO memory logic. Every handler is `python3 entry.py --host oh-my-pi`,
// wired from the GENERATED `omp/hooks.json` manifest, so the lifecycle here can
// never diverge from `registry.py`.
// What this file owns is the translation in both directions: omp's event shapes
// in, omp's per-event result shapes out.
//
// Two omp facts shape the whole design:
//
//  1. `session_shutdown` fires reliably on exit, so there is NO watchdog process
//     and no terminal-claim bookkeeping. But omp caps an AWAITED shutdown handler
//     at 2s (SESSION_SHUTDOWN_HANDLER_TIMEOUT_MS, deliberately, so an extension
//     cannot hold Ctrl+C hostage). The sweep alone budgets 200s. So the terminal
//     handlers are SPAWNED DETACHED and outlive the omp process — the manifest's
//     timeouts are the detached child's own budget, not a window omp waits out.
//  2. `session_stop` is explicitly Claude-Code-compatible: its result carries
//     `continue`/`additionalContext` (and `decision`/`reason` aliases) and buys a
//     real extra agent turn. That is what makes in-session self-consolidation
//     work here.

import { spawn } from "node:child_process"
import { readFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

type HookOutput = {
  context?: string
  system_message?: string
  systemMessage?: string
  deny?: boolean
  deny_reason?: string
  reload_assets?: boolean
  /** The topic badge. Absent = leave it alone; "" = clear it; text = show it. */
  status_badge?: string
}

type HookSpec = {
  handler: string
  timeout: number
  matcher?: string
  extra_args?: string[]
  legacy_script?: string
  retry_if_interrupted?: boolean
}

type HookManifest = { events: Record<string, HookSpec[]> }

type DispatchResult = HookOutput

/** What every dispatch needs to identify the session it belongs to. */
type Identity = {
  session_id: string
  cwd: string
  transcript_path: string
}

const pythonExecutable = () => (process.platform === "win32" ? "python" : "python3")

/**
 * The live session model, as omp names it: "<provider>/<id>" (e.g.
 * "anthropic/claude-fable-5-1"). Read PER DISPATCH from `ctx.models.current()`,
 * which omp reads lazily so it reflects `/model` switches — never cached. "" when
 * omp exposes no model facade (older host, headless embed); the Python adapter then
 * reports no model and every request omits the `model` param. The provider/id shape
 * is how omp identifies a concrete model, so it is the id verbatim, not a prefix to
 * strip. This is the SESSION model, unrelated to the pinned capture model.
 */
const currentModel = (ctx: any): string => {
  try {
    const model = ctx?.models?.current?.()
    if (!model) return ""
    const provider = String(model.provider ?? "").trim()
    const id = String(model.id ?? "").trim()
    return [provider, id].filter(Boolean).join("/")
  } catch {
    return ""
  }
}

/**
 * omp's tool vocabulary is lowercase (`bash`, `edit`, `write`) and its MCP tools
 * arrive either colon-namespaced (`neuronzai:fact_add`) or flat (`neuronzai_fact_add`).
 * Core matches the canonical Claude-style names, so normalize here — the SAME
 * mapping `hosts/oh_my_pi.py::_canonical_tool` applies to the payload it parses.
 * Both sides are asserted equal by the contract matrix.
 */
const MCP_COLON = /^([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)$/
const MCP_UNDERSCORE = /^(neuronzai)_(.+)$/
// With omp's default `tools.xdev=true` an MCP tool is a DEVICE: the model calls
// `write` with `path: xd://mcp__<server>_<server>_<tool>` and the JSON arguments
// in `content`. The proxy prefixes every tool with its server, so the repeated
// server name is what splits the device back into `mcp__<server>__<tool>`.
const DEVICE_PREFIX = "xd://"
const MCP_DEVICE = /^mcp__([A-Za-z0-9.-]+)_\1_(.+)$/
const TOOL_ALIASES: Record<string, string> = {
  bash: "Bash",
  edit: "Edit",
  write: "Write",
  multiedit: "Edit",
  apply_patch: "apply_patch",
  skill: "Skill",
  task: "Task",
}

const canonicalTool = (name: unknown): string => {
  const raw = typeof name === "string" ? name.trim() : ""
  if (!raw) return ""
  if (raw.startsWith("mcp__")) {
    const device = MCP_DEVICE.exec(raw)
    return device ? `mcp__${device[1]}__${device[2]}` : raw
  }
  const alias = TOOL_ALIASES[raw.toLowerCase()]
  if (alias) return alias
  const colon = MCP_COLON.exec(raw)
  if (colon) return `mcp__${colon[1]}__${colon[2]}`
  const underscore = MCP_UNDERSCORE.exec(raw)
  if (underscore) return `mcp__${underscore[1]}__${underscore[2]}`
  return raw
}

/**
 * A `write` to an `xd://` device is the DEVICE's call, not a file write: the
 * manifest matchers must see the MCP tool name (so `log_action` reaches capture
 * and `enter_topic` reaches the topic sync) and the handler must see the JSON
 * body as the tool input, never a path under the project. Mirrors
 * `hosts/oh_my_pi.py::_normalize_call` for the shapes that decide dispatch here.
 */
const unwrapCall = (name: unknown, input: unknown): { toolName: string; toolInput: unknown } => {
  const toolName = canonicalTool(name)
  const args = input && typeof input === "object" ? (input as Record<string, unknown>) : {}
  if (toolName !== "Write") return { toolName, toolInput: input ?? {} }
  const path = String(args.path ?? args.filePath ?? args.file_path ?? "").trim()
  if (!path.startsWith(DEVICE_PREFIX)) return { toolName, toolInput: input ?? {} }
  const device = path.slice(DEVICE_PREFIX.length).trim().replace(/^\/+|\/+$/g, "")
  if (!device) return { toolName, toolInput: input ?? {} }
  let body: unknown = {}
  try {
    const parsed: unknown = JSON.parse(String(args.content ?? ""))
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) body = parsed
  } catch {
    // A device body that is not a JSON object carries nothing a handler reads.
  }
  return { toolName: canonicalTool(device), toolInput: body }
}

/**
 * What the forced self-consolidation turn is allowed to call, and why there has
 * to be a list at all.
 *
 * omp's continuation is NOT a fresh turn. `session_stop` returning
 * `{continue: true}` makes omp append the instruction as a hidden
 * `session-stop-continuation` message and keep the SAME agent loop running
 * (`#To` in `session/agent-session.ts` — it pushes the message, sets its
 * `stop_hook_active` latch and returns "keep going"). So the model re-enters with
 * the whole conversation live, every unfinished thread in view, under a system
 * prompt that tells it never to yield while work remains. Finishing the capture
 * then reads as licence to carry on.
 *
 * Measured on 2026-09-14 (session 01a0a06b-ca34-74ef-8356-1b821adb09c0): the last
 * capture write landed at 16:23:18Z and 15 seconds later THE SAME TURN ran
 * `gh pr view`, spawned subagents and drove a PR for another 30 minutes, with no
 * user message and no host reminder anywhere in the transcript between the two.
 * The turn also acted on an offer the agent itself had made BEFORE the sweep,
 * which the user had never answered.
 *
 * Wording cannot close that: the instruction is one line of user-visible interface
 * on Claude Code (see `selfsweepInstruction`), and "end the turn" is advice a
 * resumption-biased loop can lose. So the sweep turn is CONTAINED instead: it is
 * bound to its Neuronz.ai tools rather than asked to behave. This runs in-process
 * off a latch this bridge already keeps, so an ordinary turn pays nothing for it.
 *
 * Allowed: the Neuronz.ai MCP family (the capture's records — on omp these arrive
 * as a `write` to an `xd://` device, which `unwrapCall` has already turned back
 * into `mcp__neuronzai__<tool>`), and read-only lookups, because the workflow
 * loads itself with `read skill://capture` and reads the artifacts of its own
 * spilled output. Denied: everything that executes, mutates or spawns — which is
 * exactly how a sweep turn resumes work.
 */
const SWEEP_TURN_READS: Record<string, true> = { read: true, grep: true, glob: true }
const SWEEP_TURN_MCP_PREFIX = "mcp__neuronzai__"
const sweepTurnAllows = (toolName: string): boolean =>
  SWEEP_TURN_READS[toolName.toLowerCase()] === true || toolName.startsWith(SWEEP_TURN_MCP_PREFIX)
// Model-visible: a denial on this turn has to say what the turn IS, or the model
// reads the block as an obstacle to work around rather than as the end of its
// business here.
const SWEEP_TURN_DENIAL =
  "Neuronz.ai memory sweep turn: only Neuronz.ai record calls and read-only lookups run here. " +
  "Nobody asked for this turn, so do not resume the previous task and do not act on an offer " +
  "made before it — an unanswered question is still unanswered. Finish the capture and end the " +
  "turn with an empty message."

/**
 * Handlers speak one JSON object per line on stdout. Read the LAST parseable
 * object so a stray print from a library cannot displace the real result.
 */
function parseOutput(raw?: string): HookOutput | undefined {
  const lines = (raw ?? "").split("\n").map(line => line.trim()).filter(Boolean)
  for (const line of lines.reverse()) {
    try {
      const parsed: unknown = JSON.parse(line)
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as HookOutput
    } catch {
      continue
    }
  }
  return undefined
}

async function runProcess(
  args: string[],
  stdin: string,
  cwd: string,
  timeoutSeconds: number,
): Promise<string | undefined> {
  return await new Promise<string | undefined>(resolve => {
    const child = spawn(pythonExecutable(), args, {
      cwd,
      env: process.env,
      stdio: ["pipe", "pipe", "ignore"],
    })
    const chunks: Buffer[] = []
    let bytes = 0
    let finished = false
    const done = (output?: string) => {
      if (finished) return
      finished = true
      clearTimeout(timer)
      resolve(output)
    }
    const timer = setTimeout(() => {
      child.kill("SIGKILL")
      done()
    }, Math.max(1, timeoutSeconds) * 1000)
    child.stdout.on("data", (chunk: Buffer) => {
      // Bound the buffer: a runaway handler must not grow the session's memory.
      const remaining = 1024 * 1024 - bytes
      if (remaining <= 0) return
      const bounded = chunk.subarray(0, remaining)
      chunks.push(bounded)
      bytes += bounded.length
    })
    child.stdin.on("error", () => {})
    child.once("error", () => done())
    child.once("close", () => done(Buffer.concat(chunks).toString("utf8")))
    child.stdin.end(stdin)
  })
}

export default function NeuronzaiExtension(pi: any): void {
  // `omp/plugin.ts` -> the plugin root that holds entry.py and the manifest.
  const root = dirname(dirname(fileURLToPath(import.meta.url)))
  const manifestPath = join(root, "omp", "hooks.json")
  // omp expands `${CLAUDE_PLUGIN_ROOT}` only inside a plugin's .mcp.json. The
  // command and skill bodies it sub-discovers from this same package still read
  // `sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh"`, and its bash tool inherits this
  // process's environment -- so export the root here, or every shell-out workflow
  // (login, logout, remember, capture, switch-profile, ...) hands the agent an
  // unset variable and it goes hunting for SOME copy of the plugin (on a box that
  // also has Claude Code it found and ran Claude's cached copy). Set, not
  // defaulted: inside omp this package IS the plugin those bodies belong to.
  process.env.CLAUDE_PLUGIN_ROOT = root
  // Same inheritance, different question: a shared command body runs a helper with
  // no --host, and CLAUDE_PLUGIN_ROOT alone reads as Claude Code — which would
  // rewrite <cwd>/.claude and print a skill-rescan promise this harness cannot
  // keep. Name the harness so the helper resolves it instead of defaulting to one.
  process.env.NEURONZAI_HOST = "oh-my-pi"

  let manifest: HookManifest | undefined
  let manifestFailed = false
  // SessionStart context has no lane of its own in omp: the BOOTSTRAP rides the
  // system prompt. The bootstrap ONLY — per-prompt recall takes the message lane
  // instead, and
  // `before_agent_start` below says why the two must never share a lane.
  const queued: string[] = []
  // The bootstrap text once it has been handed to omp, kept so EVERY later turn can
  // return the identical string: omp's `systemPrompt` result is a per-turn override,
  // so a bootstrap returned once both disappears from the next turn's system block
  // and changes that block's shape, which costs a full prefix rewrite.
  let pinned: string | null = null
  // Keyed by SESSION, not latched per extension instance. omp switches sessions
  // in-process -- `/new`, `fork()` and resume each emit `session_switch` on the
  // live agent-session while this module stays loaded -- and omp's own bundled
  // extensions all subscribe to `session_switch`/`session_branch`/`session_tree`
  // alongside `session_start` and re-key on each. A boolean latch would give
  // every session after the first no bootstrap at all (no rules, no repo brief,
  // no assets) while `identify()` reports its new id to recall and the sweep, and
  // would spill the previous session's queued context into it.
  let started: Identity | null = null
  const swept = new Set<string>()
  // The forced self-consolidation turn: null when none is in flight, otherwise the
  // EXACT text we asked omp to inject. Three jobs off one fact — the continuation
  // will itself settle and fire `session_stop` again (so this stops it recursing),
  // while it is set the turn is CONTAINED to the capture's own calls
  // (`sweepTurnAllows`), and the TEXT is how a real user prompt is told apart from
  // the continuation's own: omp triggers the extra turn by sending that message,
  // so `before_agent_start` fires for it with our string as the prompt.
  let continuation: string | null = null

  // "" is a real key, not a missing one: print mode exposes no session id, and
  // that session still must sweep exactly once.
  const sessionKey = (identity: Identity): string => identity.session_id || "<no-session>"

  const loadManifest = async (): Promise<HookManifest | undefined> => {
    if (manifest || manifestFailed) return manifest
    try {
      manifest = JSON.parse(await readFile(manifestPath, "utf8")) as HookManifest
    } catch {
      // Fail OPEN. A missing or unreadable manifest disables the plugin for this
      // session; it must never take the user's session down with it.
      manifestFailed = true
    }
    return manifest
  }

  const specsFor = async (event: string, toolName?: string): Promise<HookSpec[]> => {
    const loaded = await loadManifest()
    const specs = loaded?.events[event] ?? []
    if (!toolName) return specs
    return specs.filter(spec => {
      if (!spec.matcher) return true
      try {
        return new RegExp(`^(?:${spec.matcher})$`).test(toolName)
      } catch {
        return false
      }
    })
  }

  const argsFor = (spec: HookSpec, event: string): string[] =>
    spec.legacy_script
      ? [join(root, spec.legacy_script)]
      : [
          join(root, "entry.py"),
          "--host", "oh-my-pi",
          "--event", event,
          "--handler", spec.handler,
          ...(spec.extra_args ?? []),
        ]

  /**
   * omp gives an extension no session id on the event payload — `SessionStartEvent`
   * is an empty object — but the session manager on the context carries the id, the
   * cwd, and the path of the session JSONL that `hosts/oh_my_pi.py::iter_transcript`
   * already knows how to walk. Read it per dispatch: a session switch or fork
   * replaces all three mid-process.
   */
  const identify = (ctx: any): Identity => {
    const manager = ctx?.sessionManager
    const read = (fn: unknown): string => {
      try {
        return typeof fn === "function" ? String(fn.call(manager) ?? "") : ""
      } catch {
        return ""
      }
    }
    return {
      session_id: read(manager?.getSessionId),
      cwd: read(manager?.getCwd) || String(ctx?.cwd ?? ""),
      transcript_path: read(manager?.getSessionFile),
    }
  }

  const notify = (ctx: any, message?: string) => {
    if (!message) return
    try {
      ctx?.ui?.notify?.(message, "info")
    } catch {
      // A status note is never worth failing a turn over.
    }
  }

  /**
   * The topic badge, in omp's own status bar (#488). This is the whole reason the
   * host reports `custom_statusline`: a handler ASKS for a badge through the
   * neutral `Output.status_badge` and this is the only place that knows how to
   * render one, so nothing edits a file the user owns — the Claude Code path has to
   * rewrite `~/.claude/settings.json` because a Claude plugin cannot declare a
   * status line.
   *
   * `undefined` CLEARS the item (omp's own contract), which is what an empty badge
   * from an exit_topic means. A handler that says nothing sends no field at all and
   * never reaches here, so a failed sync leaves the bar as it was.
   */
  const BADGE_KEY = "neuronzai-topic"
  type StatusSurface = { setStatus(key: string, text: string | undefined): void }
  const statusSurface = (ctx: unknown): StatusSurface | undefined => {
    const ui = ctx && typeof ctx === "object" && "ui" in ctx ? ctx.ui : undefined
    if (!ui || typeof ui !== "object" || !("setStatus" in ui)) return undefined
    return typeof ui.setStatus === "function" ? (ui as StatusSurface) : undefined
  }
  const setBadge = (ctx: unknown, text: string | undefined) => {
    try {
      statusSurface(ctx)?.setStatus(BADGE_KEY, text || undefined)
    } catch {
      // Print mode, RPC, a subagent runner: no status surface, nothing to show.
    }
  }

  const dispatch = async (
    event: string,
    payload: Record<string, unknown>,
    ctx: any,
    toolName?: string,
  ): Promise<DispatchResult> => {
    const output: DispatchResult = {}
    const cwd = String(payload.cwd || root)
    // #490 — inject the live session model once, centrally, so EVERY event that
    // flows through here (session_start, user_prompt, pre_tool, post_tool) carries
    // it consistently. Without this, a tool+model rule could never fire on omp:
    // the prompt lane cannot see the tool and the action lane never saw the model.
    const model = currentModel(ctx)
    if (model && payload.model === undefined) payload.model = model
    // #527 — the directory the USER launched omp in, which a RESUMED session's
    // `cwd` is not: omp restores the session's original directory (and `--cwd`
    // does not override it), so the profile and the on-disk assets follow the repo
    // the session was born in. `process.env.PWD` is what survives that, because
    // omp's own `process.chdir()` does not rewrite it; `process.cwd()` is the
    // fallback for a launch with no shell-set PWD. Sent on every event for the
    // same reason the model is — one place, one shape — and read by SessionStart.
    if (payload.launch_cwd === undefined) {
      payload.launch_cwd = process.env.PWD || process.cwd()
    }
    for (const spec of await specsFor(event, toolName)) {
      const raw = await runProcess(argsFor(spec, event), JSON.stringify(payload), cwd, spec.timeout)
      const current = parseOutput(raw)
      if (!current) continue
      if (current.context) {
        output.context = [output.context, current.context].filter(Boolean).join("\n\n")
      }
      const message = current.system_message || current.systemMessage
      if (message) {
        output.system_message = [output.system_message, message].filter(Boolean).join("\n")
      }
      if (current.deny) {
        output.deny = true
        output.deny_reason = current.deny_reason || "Neuronz.ai blocked this tool call."
      }
      // Last handler to ask wins. Unlike `context` there is nothing to concatenate:
      // one bar shows one topic, and the field is absent unless a handler decided.
      if (typeof current.status_badge === "string") output.status_badge = current.status_badge
    }
    notify(ctx, output.system_message)
    if (typeof output.status_badge === "string") setBadge(ctx, output.status_badge)
    return output
  }

  /**
   * Terminal dispatch. omp allows an awaited `session_shutdown` handler only ~2s,
   * so these are spawned DETACHED and left to finish after omp exits. Nothing
   * reads their output: at session end there is no turn left to inject into and
   * no user left to notify.
   *
   * Detaching means omp can no longer enforce the manifest timeout — it is gone
   * before the child is. So the deadline is handed to the child, which arms it on
   * itself (`entry.py:_arm_deadline`). Without that the timeout describes nothing:
   * a hung handler would outlive the agent indefinitely, and on a CI runner or any
   * long-lived host they would pile up one per session.
   *
   * The handlers race rather than run in sequence. That is the same shape the
   * native Claude Code hooks have (its SessionEnd hooks fire in parallel too), and
   * the server tolerates it by design: presence is soft-ended while the sweep still
   * resolves the topic inside its grace window.
   */
  const dispatchDetached = async (event: string, payload: Record<string, unknown>) => {
    const cwd = String(payload.cwd || root)
    const body = JSON.stringify(payload)
    for (const spec of await specsFor(event)) {
      try {
        const child = spawn(pythonExecutable(), argsFor(spec, event), {
          cwd,
          env: { ...process.env, NEURONZAI_HANDLER_DEADLINE: String(spec.timeout) },
          detached: true,
          stdio: ["pipe", "ignore", "ignore"],
          windowsHide: true,
        })
        child.once("error", () => {})
        child.stdin.on("error", () => {})
        child.stdin.end(body)
        child.unref()
      } catch {
        // One handler failing to spawn must not stop the others, and must never
        // surface at exit — the user is already on their way out.
      }
    }
  }

  const ensureSession = async (ctx: any, source = "startup") => {
    const identity = identify(ctx)
    if (started && sessionKey(started) === sessionKey(identity)) return
    const outgoing = started
    started = identity
    // Per-session state belongs to the session that produced it. Carrying a
    // queued bootstrap or a half-finished continuation across a switch injects
    // one session's context into another's prompt.
    queued.length = 0
    pinned = null
    continuation = null
    // Topic mode is per-session and ends with the session (the presence row is
    // soft-ended at SessionEnd), so a badge the OUTGOING session left on a bar
    // this process still owns would name a topic nobody is in. Only a handover
    // can leave one, hence `outgoing`: the first session of a process has
    // nothing to clear, and a compaction keeps the session id and never reaches
    // here, which is right — that topic mode is still live.
    if (outgoing) setBadge(ctx, undefined)
    const startPayload: Record<string, unknown> = { ...identity, source }
    const result = await dispatch("session_start", startPayload, ctx)
    if (result.context) queued.push(result.context)
  }

  /**
   * Terminal handlers for ONE session, at most once. A switch ends the outgoing
   * session as surely as exiting does, so it sweeps there too — otherwise every
   * `/new` silently discards the work of the session it replaces.
   */
  const finishSession = async (identity: Identity | null) => {
    if (!identity) return
    const key = sessionKey(identity)
    if (swept.has(key)) return
    swept.add(key)
    await dispatchDetached("session_end", identity)
  }

  const guard = (handler: (event: any, ctx: any) => Promise<unknown>) =>
    async (event: any, ctx: any) => {
      try {
        return await handler(event, ctx)
      } catch {
        // Fail open, always: a Neuronz.ai outage or a bad payload degrades the
        // session to plain omp rather than breaking it.
        return undefined
      }
    }

  pi.on("session_start", guard(async (_event: any, ctx: any) => {
    await ensureSession(ctx)
  }))

  // The three ways omp replaces the live session WITHOUT reloading this module:
  // `/new` (reason "new"), `fork()` (reason "fork") and resume (reason "resume")
  // all emit `session_switch`; branching and the session tree get their own
  // events. omp's own extensions subscribe to exactly this set beside
  // `session_start`. Each ends the outgoing session and starts a new one, so
  // sweep the old and bootstrap the new — `finishSession`/`ensureSession` are
  // both keyed, so an event that turns out not to change the id does nothing.
  for (const event of ["session_switch", "session_branch", "session_tree"]) {
    pi.on(event, guard(async (_e: any, ctx: any) => {
      const outgoing = started
      if (outgoing && sessionKey(outgoing) !== sessionKey(identify(ctx))) {
        await finishSession(outgoing)
      }
      await ensureSession(ctx, event)
      return undefined
    }))
  }

  pi.on("before_agent_start", guard(async (event: any, ctx: any) => {
    await ensureSession(ctx)
    const prompt = String(event?.prompt ?? "")
    // omp triggers the consolidation turn by SENDING our instruction as a message,
    // so this fires for the continuation too, carrying that exact text (verified
    // live, omp 18.0.5 — see `features.json` selfsweep/oh-my-pi). Anything ELSE is
    // the user back at the keyboard, which ends the forced turn even when it never
    // reached a stop — an interrupted one never does, and a latch left armed there
    // would contain the user's own next call.
    if (continuation !== null && prompt !== continuation) continuation = null
    const identity = identify(ctx)
    const promptPayload: Record<string, unknown> = {
      ...identity,
      prompt,
    }
    const result = await dispatch("user_prompt", promptPayload, ctx)
    // TWO LANES, and which one a payload takes is a cache-cost decision. Anthropic's
    // prompt cache is a prefix match over [tools, system, messages...], so the system
    // block sits in FRONT of the whole history: changing it between two turns discards
    // the cached prefix and the next request pays to write every token of it again.
    // Measured on the live relay 2026-09-15 — one such rewrite was 236k cache-write
    // tokens ($2.36 at opus-5's 1h rate), 163 of them in one day, a THIRD of the day's
    // spend, and not one of those requests read a single cached token.
    //
    // The BOOTSTRAP (`session_start`, plus the re-seed after a compaction) does not
    // vary per prompt, so it belongs in the cached prefix — and it is PINNED there
    // for the rest of the session rather than spent on one
    // turn. Measured 2026-09-15 on a 4-turn session: turn 2 still paid a full rewrite
    // (31,425 cache-write tokens, zero cache read) purely because turn 1's override
    // was gone by then. Re-returning the identical text holds the hash stable from
    // the first turn AND keeps the rules briefing in front of the model all session.
    if (queued.length > 0) {
      pinned = [pinned, ...queued].filter(Boolean).join("\n\n")
      queued.length = 0
    }
    const output: Record<string, unknown> = {}
    if (pinned) {
      // omp threads the CURRENT system prompt through each handler in turn, so
      // appending to the array it hands us composes with other extensions instead
      // of clobbering them (or omp's own prompt).
      output.systemPrompt = [...(event?.systemPrompt ?? []), pinned]
    }
    // The per-prompt RECALL varies with every prompt by construction (its own text says
    // "N stored records match THIS prompt"), so it rides the MESSAGE lane: a
    // `before_agent_start` result may carry one injected message, and omp's context
    // builder converts `custom_message` entries into model context
    // (`buildSessionContext` step 5) while `display: false` keeps it out of the TUI.
    // Landing after the cache breakpoint means a new turn EXTENDS the prefix instead
    // of invalidating it. `attribution: "agent"` because the extension initiated it,
    // not the user.
    if (result.context) {
      output.message = {
        customType: "ai.neuronz.recall",
        content: result.context,
        display: false,
        attribution: "agent",
      }
    }
    return Object.keys(output).length > 0 ? output : undefined
  }))

  pi.on("tool_call", guard(async (event: any, ctx: any) => {
    await ensureSession(ctx)
    const identity = identify(ctx)
    const { toolName, toolInput } = unwrapCall(event?.toolName, event?.input)
    // The sweep turn may only write records and look things up (see
    // `sweepTurnAllows`). Checked BEFORE the pre_tool handlers: a call that is
    // not part of the capture has no gate to consult, and the rule gate is not
    // wired on this host anyway.
    if (continuation !== null && !sweepTurnAllows(toolName)) {
      return { block: true, reason: SWEEP_TURN_DENIAL }
    }
    const result = await dispatch("pre_tool", {
      ...identity,
      tool_name: toolName,
      tool_input: toolInput,
    }, ctx, toolName)
    // omp's pre-tool result can block a call but cannot add model-visible context
    // (`injects_on_pre_tool=False`), so a handler's context has nowhere to go here.
    if (result.deny) return { block: true, reason: result.deny_reason }
    return undefined
  }))

  pi.on("tool_result", guard(async (event: any, ctx: any) => {
    await ensureSession(ctx)
    const identity = identify(ctx)
    const { toolName, toolInput } = unwrapCall(event?.toolName, event?.input)
    await dispatch("post_tool", {
      ...identity,
      tool_name: toolName,
      tool_input: toolInput,
      tool_response: event?.content,
    }, ctx, toolName)
    return undefined
  }))

  pi.on("session_before_compact", guard(async (_event: any, ctx: any) => {
    await ensureSession(ctx)
    const identity = identify(ctx)
    // The pre-compact sweep distills the ledger; it injects nothing, so the
    // result is deliberately discarded rather than pushed into the summary.
    await dispatch("pre_compact", identity, ctx)
    return undefined
  }))

  // The POST-compaction notification, and the only honest moment to tell the server
  // this session's delivered context is gone: `session_before_compact` above can be
  // answered with `{cancel: true}`, so acting on it would rotate the rules
  // pointer's delivery token and re-push everything for a compaction that never
  // ran. This fires only once one has, and it is dispatched as a compact-sourced
  // session start — the same shape a host with a native compact-sourced start
  // produces, which is the payload core/guidance reads to invalidate.
  //
  // The bootstrap chunks it returns ARE injected here (this host keeps
  // additionalContext across a compaction), queued for the next prompt like every
  // other queued context: the compaction summarized the briefing away, so re-seeding
  // it is the point. `ensureSession` cannot do this job — the session id survives a
  // compaction, so it is keyed to a session already started and returns immediately.
  pi.on("session_compact", guard(async (_event: unknown, ctx: unknown) => {
    await ensureSession(ctx)
    const identity = identify(ctx)
    const result = await dispatch("session_start", { ...identity, source: "compact" }, ctx)
    if (result.context) queued.push(result.context)
    return undefined
  }))

  pi.on("session_stop", guard(async (event: any, ctx: any) => {
    await ensureSession(ctx)
    const identity = identify(ctx)
    const active = continuation !== null || Boolean(event?.stop_hook_active)
    const result = await dispatch("stop", {
      ...identity,
      session_id: String(event?.session_id || identity.session_id),
      transcript_path: String(event?.session_file || identity.transcript_path),
      stop_hook_active: active,
    }, ctx)
    if (active) {
      continuation = null
      return undefined
    }
    if (!result.context) return undefined
    continuation = result.context
    return { continue: true, additionalContext: result.context }
  }))

  pi.on("session_shutdown", guard(async (_event: any, ctx: any) => {
    // The live session at exit, falling back to the one we started if omp has
    // already torn its session manager down by the time this fires.
    const identity = identify(ctx)
    await finishSession(identity.session_id ? identity : started ?? identity)
    return undefined
  }))
}
