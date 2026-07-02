# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

- openai-server: accept "parameters"/"params"/"input" as argument-key aliases (Haiku 4.5 live:
  {"function": {"name": ..., "parameters": {...}}} silently produced EMPTY arguments -- every
  call failed schema validation, "9 failed, 0 spawns"). _call_shaped normalizes every accepted
  shape to flat {name, arguments}; _to_calls honors the aliases for the legacy tool_calls
  wrapper too. Also: drop SlashCommand from the chat-only --disallowedTools list (not a valid
  tool name -- the CLI prepended a warning line to every answer). +4 tests.

- openai-server: accept alias wrapper keys around the call array -- {"actions":[...]},
  {"calls":[...]}, {"function_calls":[...]} (Fable 5 live: the Dungeon-win-logic scenario came
  as {"actions":[OpenAI-shaped calls]} and scored tools=0). Stricter than "tool_calls": the
  dict must contain ONLY the wrapper key and every element must be call-shaped (or the list a
  bare-args array). The live emitter streams all wrapper spellings incrementally. +4 tests.

- openai-server: accept a fenced JSON ARRAY of BARE ARGUMENT OBJECTS (Fable 5 live: a 75-object
  G6 castle as ```json [ {"action":"spawn","targetName":...}, ... ] ``` with no function name
  anywhere scored tools=0). Same exactly-one-tool key-fit gate as the bare-object-lines
  spelling; the live emitter also streams plain-array fences OBJECT BY OBJECT (both Opus-style
  call objects and Fable-style bare argument objects), so this spelling now execute-as-streams
  too. +5 tests.

- openai-server: accept the {"action": "<tool>", "arguments": {...}} call spelling (Opus 4.8
  live: G5 Ordered spawn scored tools=0 on a perfectly-shaped 3-call array using "action"
  instead of "name"). Exact-keys rule as for {name, arguments}; normalized in _call_shaped so
  every consumer (fence loop, JSONL, arrays, live emitter) inherits it. +5 tests.

- openai-server: REAL token streaming for the `claude` engine. `stream: true` now forwards
  live deltas while the CLI generates instead of replaying the finished answer: the claude
  provider runs `--output-format stream-json --include-partial-messages` piped through the new
  `stream_text_filter.py` (events -> plain answer text, written incrementally), so the task log
  grows during generation; the bridge tails the log (`_tail_task_log`, byte-offset + incremental
  UTF-8 decode, CR-normalized) and emits each piece as an SSE `delta.content` chunk. Canonical
  fenced `{"tool_calls":[...]}` calls are converted into native `delta.tool_calls` chunks AS
  EACH CALL'S JSON OBJECT CLOSES (`LiveToolCallEmitter` — a 100-call build turn streams its
  calls one by one); non-canonical fences are parsed complete at fence close, and an
  end-of-turn full-parser reconciliation emits anything the incremental scanner missed (late
  but correct, no double emits — matched by name + canonical args). The first ~350 chars are
  held back so a provider limit banner can still surface as HTTP 429 (SSE headers are sent
  lazily on the first real chunk); invisible retries/resume-fallbacks are only permitted while
  nothing has reached the client (`LiveStreamDied` finalizes with what was already sent
  otherwise). `--no-live-stream` reverts to the legacy replay; non-live engines keep it.
  Verified live on haiku: ~0.2 s chunk spacing, per-call tool_calls chunks, clean resume turn.
  +29 tests (emitter, canonical-prefix matcher, JSON object scanner, stream filter, log tail).

- openai-server: prompt rewritten to prescribe ONE canonical tool-call format — a single fenced
  ```json {"tool_calls":[...]} block — instead of offering multiple. Reduces the format sprawl
  that kept surfacing new unparsed spellings, and drops the "write the call text for the
  application to run" framing (which Claude Code's policy layer flagged for some models as
  duplicating tool-use); the new wording asks for "a structured JSON request the application
  carries out", which reads as ordinary structured output.
- openai-server: also accept a JSON ARRAY of call objects in one fence
  (```json [ {"name":...,"arguments":...}, ... ] ```) — Opus 4.8's dominant spelling, which
  scored tools=0 across G1/G3/G4/G6/G7 before this. Consumed only when every element is
  call-shaped, so a plain data array survives as content. +2 tests.

- openai-server: a language-tagged fence that IS the whole answer (little/no prose outside)
  holds real calls, not an example — spark wraps its actual multiline world_command(...) calls
  in ```python. Tagged fences are masked as examples only when there is real prose (>=40 chars)
  outside them. +1 test.

- openai-server: accept the ONE-FENCED-JSON-PER-CALL spelling — each call in its own fenced
  block shaped like an OpenAI tool-call object ({"type":"function","function":{...}}), no
  `tool_calls` wrapper. Observed live from Sonnet 5: every world_command scenario scored
  tools=0 while the transcripts held perfectly-shaped calls (suite 56.3 → 76.1 after the fix).
  Fences are collected in order, the flat {"name":...,"arguments":{...}} shape is accepted too,
  the name must be a KNOWN tool (a plain JSON answer is never eaten), and an explicit
  `tool_calls` block still wins. +5 tests.

- openai-server: a CLI answer that IS the provider's usage-limit banner ("You've hit your
  session limit · resets 7:40am") now surfaces as an OpenAI-style HTTP 429 `rate_limit_error`
  instead of a normal 200 completion — a live benchmark run scored every scenario ~0 as a MODEL
  failure when the account was simply rate-limited. Narrow gate: short text matching the banner
  wording; prose that merely discusses limits is untouched. +4 tests.

- openai-server: recognize the NAMELESS call spelling — the entire message is bare JSON argument
  objects, one per line, with no function name at all. Observed live from gpt-5.3-codex-spark in
  the single-tool G6 scenario ({"action":"spawn","targetName":...} x~400 — the model "saved" the
  redundant tool name; the run scored tools=0/Fail 15.2 with a fully designed castle in the
  transcript). Deterministic gate, deliberately strict: EVERY non-blank, non-fence-marker line
  must parse as a non-empty JSON object AND the keys must fit (subset of schema parameter names)
  exactly ONE tool across all lines — any prose line or ambiguity rejects the whole message, so
  ordinary answers can never trigger it. Echo dedup applies. +6 tests (115 total), including the
  verbatim live-failure lines.

- doctor/GUI: the Claude limits panel now shows a LOCAL USAGE ESTIMATE instead of "no data" —
  tokens burned in the current 5h window and the last 7 days (in+out and cache separately),
  summed ccusage-style from the CLI's own transcript files (~/.claude/projects/**/*.jsonl usage
  blocks). The Claude CLI exposes no remaining-limit API, so this reports what was SPENT, not
  what remains (the plan cap isn't available locally); wired through the existing doctor `note`
  field, so the GUI needed no changes.

- openai-server: 8 parser/robustness defects fixed after an independent adversarial audit (every
  one reproduced live against the real functions before fixing):
  - **String argument ending in a backslash silently killed the whole call** (a JSON-escaped
    Windows path like `"C:\\Games\\"`): the quote scanner's single-char lookbehind couldn't tell
    `\"` from `\\"`, so the string "never closed" and the call was dropped. Now an odd/even
    backslash-run check (`_is_escaped`) in both the call scanner and `_split_top_level`.
  - **A known-tool name inside another call's string argument became a phantom second call**
    (`execute_lua(code="world_command(action=1)")` executed BOTH) — scan cursor now skips
    matches inside an already-accepted call's span.
  - **A malformed `tool_calls` fence swallowed a following valid one** (non-greedy DOTALL match
    ran past its own closing fence) — fence bodies now exclude backticks.
  - **Display-text corruption when stripping multiple fences** with leading whitespace (a
    mid-loop `.strip()` shifted the remaining reversed-match offsets) — strip once at the end.
  - **Format-2 call syntax inside a language-tagged code fence was executed** (a ```lua example
    mentioning `world_command(...)` ran for real) — tagged fences are masked (same-length, so
    spans stay valid) before the func-syntax scan; untagged fences still parse, since models
    often wrap genuine call lines in a bare fence.
  - **```JSON (uppercase tag) fences were not recognized** — fence regex is case-insensitive.
  - **Bare unfenced `{"tool_calls":...}` followed by trailing prose was lost** (`endswith("}")`
    gate) — `raw_decode` now takes the JSON object and keeps the prose as display text.
  - **Resume-after-timeout hole**: a session whose meta state was still `running` (wrapper
    killed by timeout, orphaned CLI grandchild possibly still appending to the log) counted as
    "healthy" and could be resumed, misattributing orphan output — healthy now means positively
    finished (`done`/`waiting`).
  - Dedup false-positive contract made explicit: a Format-2 line identical to an executed call
    is ALWAYS summary prose; the prompt now documents Format 1 (fenced JSON, dedup-exempt) as
    the deliberate-repeat escape hatch. `--retries` is clamped non-negative. +13 tests
    (109 total in test_openai_server.py) covering every defect above plus pins for
    multiline/two-per-line calls that already worked.

- openai-server stability (toward "almost a real OpenAI API" for benchmark use):
  - **Retry-on-empty**: a completion whose CLI invocation came back empty or in an `error` meta
    state is re-run (`--retries`, default 1, 2s backoff) before the bridge gives up — a real
    OpenAI endpoint effectively never returns an empty 200, and one transient CLI hiccup
    (rate-limit blip, session startup race) should not zero a whole benchmark scenario. A resume
    that "succeeds" but produces an EMPTY answer now also falls back to a fresh run instead of
    returning `""`.
  - **OpenAI-style error surface**: an unexpected exception inside the bridge now returns
    `{"error": {"message": ..., "type": "server_error"}}` with HTTP 500 instead of a bare
    connection reset the client can't distinguish from a network failure.
  - **Estimated `usage`**: responses now carry ~4-chars/token estimates (flagged
    `"neoxider_estimated": true`) instead of hardcoded `0/0/0` — useful for cost panels, not
    billing-grade.
  - **Anti-echo prompt**: `TOOLCALL_INSTRUCTIONS` now explicitly tells the model not to restate
    already-executed calls in call syntax after a tool result — the prompt-side half of the echo
    defense (the parser-side dedup landed earlier).
  - `GET /health` additionally reports `timeout_seconds`/`retries`. +9 tests (96 total in
    test_openai_server.py): retry/fallback behavior (H._run exercised unbound with fakes),
    usage estimates, anti-echo prompt regression guard.

- openai-server: the Format-2 call parser now accepts a single positional JSON object argument —
  `world_command({"action":"spawn","targetName":"Enemy1"})` — in addition to `name=value` pairs.
  This is gpt-5.5's DOMINANT spelling (literally how an OpenAI SDK call is written); before this,
  every such line was silently dropped as prose, which zeroed whole CoreAI benchmark groups
  (a live gpt-5.5 run scored G5 50/100 with 4 scenarios at `tools=0` whose transcripts contained
  perfectly good calls — replaying the fixed parser over those exact captured outputs recovers
  10/10 previously-dropped call-shaped messages, 0 still dropped). A single positional SCALAR now
  also maps onto the function's sole parameter when the OpenAI `tools` JSON schema says it has
  exactly one (`execute_lua("print(1)")` → `{"code":"print(1)"}`); a scalar that looks like a
  failed `{...}` parse is deliberately NOT wrapped that way (would double-wrap into nonsense).
  New `tool_param_names()` helper; `extract_tool_calls()` takes an optional `tools=` argument
  (old call sites without it keep working). `TOOLCALL_INSTRUCTIONS` documents the accepted
  spelling. +12 parser tests (79 total), including verbatim shapes from the live failing run.

- Security/correctness hardening (the bridge is now genuinely a chat-only completion endpoint,
  not just "a CLI told not to use its other tools"): every subprocess the bridge launches now gets
  `AGENT_CHAT_ONLY=1` in its env, which `providers/{codex,claude}/provider.sh` react to with REAL
  CLI-level restrictions rather than a prompt-only ask. codex runs with `--sandbox read-only
  --ignore-user-config` (blocks file writes/shell execution; skips `~/.codex/config.toml`, where
  this machine's real `[mcp_servers.*]` are defined). claude runs with `--strict-mcp-config` (zero
  MCP servers) plus `--disallowedTools Bash,Edit,Write,NotebookEdit,Task,WebFetch,WebSearch`.
  Motivation: a CLI subagent has its own real, separately-configured tool access (this machine has
  a live `unityMCP` registered for both codex and claude) — without this, a model could reach for
  the REAL tool instead of answering in the format the calling application expects, silently
  mutating live state the calling application never asked it to touch. Verified live:
  `-c mcp_servers={}` on the codex command line did NOT actually stop a real `unityMCP.manage_tools`
  call from succeeding against a live Unity Editor (list_groups came back with real data) --
  `--ignore-user-config` does (list_mcp_resources came back empty, and the model correctly reported
  no MCP tools available). Asking the hardened bridge directly to "use unityMCP" got a correct
  refusal from both engines, no side effects, no hang. A normal `agent.sh run`/`reply` OUTSIDE the
  bridge (AGENT_CHAT_ONLY unset) is completely unaffected and keeps full file/shell/MCP access,
  confirmed live (wrote a real file successfully). `openai_server.py`'s prompt was also rewritten:
  a new `BASE_INSTRUCTIONS` block (prepended to every prompt, tools or not) states plainly that the
  session has no MCP/skills/tools, and `TOOLCALL_INSTRUCTIONS` was reframed away from "You are
  acting as X, NOT an autonomous agent" -- that identity-override phrasing was OBSERVED LIVE to get
  refused by Claude Code as a prompt-injection attempt; the new framing ("the external application
  wants you to draft a call for its own downstream execution") does not. opencode/gemini have no
  equivalent restriction yet -- their CLIs weren't verified to have an analogous flag, documented
  as a known gap rather than guessed at. Tests: new `ChatOnlyEnvTests` (python) +
  "AGENT_CHAT_ONLY sandboxing" section (bash).
- Fix (tool-calling recognition — the big benchmark win): the bridge's tool-call reparser now
  ALSO recognizes literal `name(arg=value, ...)` call syntax, not only a JSON
  `{"tool_calls":[...]}` block. Root cause surfaced by running CoreAI's Game-Creation Benchmark
  through the bridge on `gpt-5.3-codex-spark`: the model actually SOLVED scenarios (e.g. a full
  50+ object castle in G6, `world_command`/`execute_lua` calls in G1–G5) but wrote the calls the
  way a CLI agent naturally would — `world_command(action="spawn", targetName="Tower_NW", x=-6,
  ...)` as plain text — instead of the prompted JSON, so the JSON-only reparser saw zero tool
  calls and scored those scenarios 0%. `extract_func_calls` parses every `name(...)` whose name
  is a known tool (parenthesis-balanced, quote-aware, top-level arg split so a value containing
  commas/braces like `"{10,20,30}"` stays intact; values typed via JSON→literal→bare-string), and
  `extract_tool_calls(text, names)` falls back to it after the JSON paths. Engine-agnostic (helps
  claude/opencode/gemini too). Verified live: a multi-object `world_command` build returned 3
  correct `tool_calls`. Covered by new `FuncCallSyntaxTests`.
- Stronger tool-calling prompt: it now states the model has no shell/filesystem and the ONLY way
  to act is to emit a call; that *describing* an action in prose ("I called X", "Execution
  succeeded") is IGNORED and treated as a failed turn; that it may emit as MANY calls as the task
  needs; and it advertises BOTH accepted formats (JSON block or `name(arg=value)` lines). Observed
  live steering codex to emit clean JSON on its own.
- Note on speed (asked about while benchmarking): each bridge turn is a full cold `codex`/`claude`
  CLI process (no warm daemon exists to reuse), so per-turn latency (~15–50s) is inference +
  process start, not something the bridge can cache away — measured `-c mcp_servers={}` made no
  difference. The existing session model already resumes (sends only the new tail) within one
  conversation; across the benchmark's many independent scenarios a fresh session per scenario is
  correct, not waste. The real throughput win here is fewer FAILED scenarios (hence fewer retries)
  from the tool-call fix above.
- Hardening (from a second independent codex audit of the fixes below):
  - The codex provider resolves a working `python3`/`python`/`py` (with a real smoke test, so a
    Windows WindowsApps alias stub is skipped; override via `AGENT_PYTHON`) and, if none works,
    degrades to a raw `cat` passthrough instead of hard-failing the whole codex run.
  - `_provider_codex_emit` now exits non-zero when there is no `agent_message` (auth/rate-limit/
    schema drift), so the `PIPESTATUS[1]` guard marks the task failed instead of "done with junk";
    and it neutralizes the pathological case where the answer itself contains a line exactly equal
    to `---------- output ----------` (a trailing space stops `last_output` truncating there).
  - `reply_agent` (bridge) additionally rejects a reply whose task did not end in a good state
    (`done`/`waiting`): agent.sh writes the reply header before dispatch, so a provider that fails
    AFTER the header grows the log — the earlier length-only guard would have returned that failed
    block as the answer. Now `_run` falls back to a fresh run.
  - `openai_server.last_output` is line-anchored (whole-line marker match) like agent.sh's awk,
    instead of a substring `rfind` that could truncate an answer containing the marker text
    mid-sentence.
  - Verified live after these changes against `gpt-5.3-codex-spark`: a 6-turn growing-history
    chain stayed ONE underlying session (1 `[run]` + 5 `[reply]` blocks) and recalled facts from
    turns 1–3 at turn 6; fresh/continuation/tool-calling all still clean. Tests: 84 python + 58
    bash.
- Fix (codex clean output): the `codex` provider now runs `codex exec --json` and extracts
  ONLY the final agent message (`_provider_codex_emit` in `providers/codex/provider.sh`),
  instead of letting codex's plaintext `exec` dump its startup banner / session id / ERROR-log
  lines / "tokens used" footer / cp866-mojibake OS-notification line into the same stream as the
  answer. This supersedes the earlier "documented, not fixed" codex-chrome and mojibake notes
  below: `agent.sh last`, the GUI chat view, and the openai-server bridge's `content` now all
  get a clean codex answer. The parser re-emits a `session id: <uuid>` line (so agent.sh's
  resume grep keeps working) plus a synthetic `---------- output ----------` marker so
  `last_output` slices to just the answer; on any failure with no agent message it passes the
  raw stream through so auth/rate-limit errors stay visible. Verified live end-to-end against
  `gpt-5.3-codex-spark`: fresh completion, multi-turn context recall via resume, and a
  divergent unrelated turn (new session) all returned clean one-word answers. Covered by new
  unit tests (`_provider_codex_emit` cases in `tests/test_agent_sh.sh`).
- Fix (bridge stale-answer guard): `reply_agent` in `openai_server.py` returned
  `last_output(read_log(name))` unconditionally, so if a resume appended nothing to the log
  (e.g. `agent.sh reply` died before writing a block because no session id resolved) the bridge
  silently echoed the PREVIOUS answer as if it were the new one. It now detects that the log
  didn't grow, returns `None`, and `_run` falls back to a fresh run with the full history.
  Covered by new `ReplyAgentStaleGuardTests` in `tests/test_openai_server.py`.
- Fix (codex provider exit code): `provider_codex_run_cmd`/`provider_codex_resume_cmd` now
  surface a `_provider_codex_emit` (Python parser) failure via `PIPESTATUS[1]` instead of
  masking it behind codex's own exit code, so a missing/crashed Python interpreter can't mark a
  task "done" with empty output.
- Known trade-off (accepted, systemic): the answer boundary is still the exact line
  `---------- output ----------`; a provider answer that literally contains that line on its own
  would be truncated by `last_output`. This has always been true for every provider (agent.sh
  writes one such marker per turn) — the codex `--json` path adds a second synthetic one but no
  new failure mode, since the trigger is the same never-observed literal string.
- Fix: `codex` sessions resumed via `agent.sh reply` silently drifted to a different
  model/effort than they started with (a `-m spark` session came back as `gpt-5.5` on
  resume) — `codex exec resume` does support `-m`/`-c model_reasoning_effort=`
  (confirmed via `--help`), it just wasn't being forwarded.
  `provider_codex_resume_cmd` now passes both, matching `claude`'s existing resume
  behavior (`PROVIDER_CODEX_RESUME_NEEDS_MODEL=1`). Verified live: the resumed
  session's own banner correctly reports the original model/effort after the fix.
  Caveat: a bare `reply` with no explicit `-m` still resolves to the provider's
  default rather than the task's original model — this was already true for
  `claude` and is now consistent for `codex` too.
- New `tests/live_smoke_openai_server.py`: a standalone, deliberately-manual end-to-end
  smoke test for `openai_server.py` against a real CLI subagent (not part of the fast
  unit suites, since it costs real subscription usage) — health/error responses, a
  fresh completion, session continuation with real context recall, a tool-call round
  trip, divergence, `/reset`, idle-timeout expiry, streaming, and concurrency, all
  against a scratch `AGENT_CLI_LOGS` that never touches the real one. Verified live:
  23/23 checks passed against `claude`.
- Stable, documented GUI port: `gui.py` resolves explicit CLI arg > `$AGENT_GUI_PORT`
  env var > `8765` default, instead of drifting across manual invocations.
- `neoxider` bare invocation now prints a usage summary instead of auto-opening the
  browser GUI; `neoxider gui [port]` opens it explicitly, `neoxider help` prints the
  full `agent.sh` command reference.
- New `/api/stream?task=<name>` (Server-Sent Events) and `/api/wait?task=<name>&timeout=<sec>`
  endpoints — real-time log tailing and a synchronous blocking-poll convenience call,
  so the API can be consumed without a manual polling loop.
- Provider plugin architecture: `providers/<name>/provider.sh` + `provider.json`,
  `agent.sh provider-info <engine>` — adding a provider is now one new directory, zero
  edits to `agent.sh`/`gui.py`.
- GUI refactor: modular `static/*.js`/`static/style.css` instead of one large
  `gui.html`, i18n (English default, Russian second locale, easy to add more),
  cached + manually-refreshable doctor/rate-limit panels, dropped the redundant
  status dot, prettier scrollbars.
- Concurrency-safety fixes for shared `LOGDIR` access from multiple concurrent
  processes/installs (atomic `meta_set`, collision-resistant auto task names).
- Separate model + effort selectors in the GUI (today effort is baked into the model
  alias string).
- Audited full-auto/non-interactive flags for every provider; documented in the README.
- New `agent.sh openai-server` command + standalone `openai_server.py`: an
  OpenAI-compatible `/v1/chat/completions` HTTP bridge backed by a CLI subagent
  (claude/codex/opencode/gemini), so any OpenAI-compatible client can use a CLI-agent
  subscription as its "model" instead of a real provider API key.
- `stream: true` and `tools`/function-calling support in the bridge, both emulated on
  top of the underlying CLI: streaming replays an already-finished answer as
  word-sized SSE chunks (connection closed after `data: [DONE]`); tool-calling is
  prompted (fenced JSON tool-call block) and reparsed into a real OpenAI `tool_calls`
  response.
- One bridge process = one fixed engine/model/effort; run several instances on
  different ports to compare models/providers side by side.
- Verified live: non-streaming, streaming, tool-calling, multi-turn history, and a
  full tool-call → tool-result → final-answer round-trip against a real CLI subagent
  (Claude) all confirmed working end-to-end via curl, including two concurrent
  requests with no task-name collision. Wire-compatibility with CoreAI's
  Game-Creation Benchmark integration point (`COREAI_TEST_BASE_URL`) was confirmed by
  design/code-reading, not by running the actual Unity benchmark suite.
- Fix: the bridge could leak a stray fenced ```` ```json {"tool_calls":[]} ```` ````
  block into a plain-prose `content` string — observed live when a tool result was
  fed back and the model (correctly) decided no further call was needed but still
  echoed an empty tool-call block out of habit. `extract_tool_calls` now strips any
  recognized tool-call JSON fence from the displayed text regardless of whether it
  produced a real (non-empty) call, and the prompt instructions were tightened to
  discourage emitting it in the first place.
- Added a `messages` array required/non-empty validation (`400` instead of silently
  running an agent with an empty prompt).
- `/v1/chat/completions` requests now correctly return `400` for both an empty and a
  missing `messages` field, and `404` for any path that doesn't end in
  `/chat/completions` — verified live.
- Documented a real, pre-existing caveat surfaced by manual testing: `codex`'s
  non-interactive `exec` mode mixes its own startup banner/session-id/error-log lines
  into the same output stream as the answer (same raw text `agent.sh last`/the GUI's
  chat view already show for codex tasks) — this bridge does not attempt
  engine-specific cleanup, so `claude`/`opencode`/`gemini` are recommended when a
  clean `content` string matters to the caller.
  **(Superseded above: the codex provider now uses `codex exec --json` and returns a clean
  answer for all three surfaces.)**
- Root-caused an occasional garbled/mojibake line in `codex`'s raw output (e.g.
  `ᯥ譮: ,  䨪஬ ...`): it's a Windows OS notification ("process N terminated") printed
  in the console's cp866 codepage, mis-decoded as UTF-8 by the `utf-8`-assuming
  subprocess capture shared with `agent.sh`/`gui.py`. Documented, not fixed (project-
  wide capture behavior, out of scope for this bridge alone).
  **(Superseded above for the openai-server/`agent.sh last` codex path: the `--json` parser
  reads with `errors="ignore"` and keeps only the structured final message, so the mojibake
  line no longer reaches the answer.)**
- Fix: `model` in responses/`/health`/`/v1/models` showed the bare CLI alias with no
  version number (`"claude/sonnet-low"`, `"claude/opus"`), not which real model that
  alias resolves to. Added a `model_labels` alias→display-name map to
  `providers/{claude,codex,gemini}/provider.json` (`"sonnet"` → `"Sonnet 5"`, `"opus"`
  → `"Opus 4.8"`, `"haiku"` → `"Haiku 4.5"`, `"spark"` → `"GPT-5.3 Codex Spark"`, etc.);
  `model_label()` now shows `"claude/Sonnet 5 (low)"` / `"claude/Opus 4.8"`. Verified
  live end-to-end for both aliases.
- Confirmed (live, outside this bridge entirely) that `opencode` currently fails with
  `UnknownError: Unexpected server error` on every model tried, including an
  authenticated one (`zai/glm-4.5-flash`) — reproduces identically via the raw
  `opencode run` CLI with zero `agent.sh`/bridge involvement, so it's an
  environment/opencode-side issue, not a bug in this project.
- **Session-continuation model for `openai-server`, replacing the earlier stateless
  design**: one bridge process now keeps one ongoing chat session, not a fresh agent
  every call. The bridge remembers the `messages` array from the previous call, and
  when a new call's `messages` is a deterministic extension of it (exact prefix check,
  not a guess), only the new tail is sent to the *same* underlying CLI session via
  `agent.sh reply` (resume) instead of resending the whole growing history through a
  brand-new `agent.sh run`. Any mismatch (edited/rolled-back history, a genuinely
  different conversation, the first call ever, or a previous session that ended in
  `error`/`stalled`) falls back safely to a fresh `agent.sh run` with the full history.
  This both avoids resending an ever-growing prompt and lets the underlying provider's
  own prompt caching apply, since the CLI sees one real growing conversation instead of
  a brand-new mega-prompt every time.
- Added `"supports_resume"` to every `provider.json` (`claude`/`codex`: `true`;
  `opencode`/`gemini`: `false`) — engines without resume support always take the
  fresh-run path, every call.
- New `POST .../reset` endpoint: clears the remembered session (drops the remembered
  `messages`/task, wipes the scratch working dir unless `--dir` was pinned to a real
  project) so the next call starts completely fresh. `GET /health` and `GET /` now also
  report `session_active` (bool) and `session_turns` (message count in the remembered
  array).
- New `--session-ttl` flag (default `1800` = 30 minutes): an idle session is treated
  exactly like a dead one once it's gone unused longer than this, so an abandoned
  conversation can't be resumed forever or grow unbounded. `GET /health` now also
  reports `session_idle_seconds`/`session_ttl_seconds`. Verified live with
  `--session-ttl 8`: an extension call sent 12s after the last one correctly fell back
  to a fresh `agent.sh run` with the full history instead of resuming (task count
  incremented, log showed a `[run]` block, not `[reply]`) — same correct answer either
  way, just without the token-saving continuation.
- The session's working directory now persists for the session's whole lifetime
  (previously a disposable per-call scratch dir) — wiped and recreated only when a
  brand-new session starts (divergence, reset, or first-ever call), never touched when
  `--dir` pins a real project path.
- Verified live: Claude — a 2-turn history followed by a 3rd-turn recall question
  produced the correct answer, and the task log showed exactly one `[run]` block
  followed by one `[reply]` block containing only the new tail; the task-file count
  stayed at 1 across 4 sequential calls (8 messages of session state). A genuinely
  different conversation sent next correctly triggered a new session (task count
  1→2, `session_turns` reset to 1). `POST .../reset` correctly cleared the session
  (`session_active` back to `false`), and the next call after reset started yet
  another new session (task count → 3). Streaming (`stream: true`) works on a
  continuation call too, not just fresh sessions.
- Verified live: Codex — the same continuation mechanism reused the same underlying
  session id across 2 calls, task count stayed at 1, and correctly recalled a fact
  from 2 turns earlier.
- Verified live: concurrency safety — two genuinely concurrent, unrelated one-shot
  requests both got their own correct answers with zero cross-contamination; the
  `SESSION_LOCK` serializes overlapping requests, and since the second request's
  messages don't extend the first's, it correctly falls back to its own fresh session.
- Verified live: Gemini (no resume support) — every call, including an "extension"
  one, correctly created a brand-new task, with zero errors, confirming graceful
  degradation for engines without `supports_resume`.
- Documented a new, pre-existing `codex`/`agent.sh` quirk surfaced by this work:
  `provider_codex_resume_cmd` does not forward the `--effort`/model flags on resume
  (unlike `claude`, which needs and gets them re-sent) — a resumed `codex` session may
  silently run at a different reasoning effort than the one it started with.
  **(Superseded above: `provider_codex_resume_cmd` now forwards `-m`/`-c
  model_reasoning_effort=`, `PROVIDER_CODEX_RESUME_NEEDS_MODEL=1`.)**

## [0.1.0] - 2026-07-01

Initial public version.

### Added

- `agent.sh`: non-interactive CLI-subagent wrapper (Codex, Claude Code, opencode,
  Gemini CLI) — `run`/`reply`/`log`/`last`/`status`/`list`/`doctor`/`gui`. One
  thread-per-task log+meta model, durable markdown checkpoints, liveness/state
  detection ("did the agent ask a question?"), `-p` `PROGRESS.md` protocol for
  long-running tasks.
- `gui.py` + `gui.html`: zero-dependency local web GUI — project/subagent tree,
  chat-style thread view with markdown, provider/model picker with an adaptive
  rate-limit panel, folder browser, resizable panels, toast notifications with
  history, optional "open in a real terminal" checkbox.
- `neoxider` launcher command (bash + PowerShell): no-arg opens the GUI, any other
  argument passes through to `agent.sh`.
- Claude Code plugin packaging (`.claude-plugin/plugin.json` + `marketplace.json`) —
  installable via `/plugin marketplace add` + `/plugin install`, no file relocation
  needed (root-level `SKILL.md` auto-detects as a single-skill plugin).
- MIT license.

### Fixed

- `<name>.meta`'s `model=` field now records the *resolved* model + effort (e.g.
  `claude-sonnet-5-high`, `gpt-5.3-codex-spark-medium`) instead of the raw CLI alias
  or the literal string `"default"`.
- Pinned Claude's default model to the explicit id `claude-sonnet-5` (the `sonnet`
  CLI alias was resolving to a stale `claude-sonnet-4-6` on this account/CLI version)
  with `effort high` by default.
