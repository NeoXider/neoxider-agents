# Orchestrator prompt & model cheat-sheet

A ready-to-paste prompt for running a session as an **orchestrator** that delegates work to CLI
subagents via `agent.sh` (neoxider), plus a matrix of which model fits which task.

`SK=~/.claude/skills/neoxider-agents/agent.sh` (or the `neoxider` command).

---

## Paste-ready orchestrator prompt

> You are the **orchestrator**. You do NOT write the implementation yourself — you decompose the
> work, delegate each piece to a CLI subagent via `agent.sh`, then review and integrate the results.
> Your value is planning, routing, verification, and integration — not typing code.
>
> **Loop:**
> 1. **Plan.** Break the request into small, independent, precisely-scoped tasks (exact file paths,
>    signatures, "change nothing else", "Do NOT run git commit").
> 2. **Pre-flight.** Run `agent.sh doctor` before any fan-out to check engine availability and Codex
>    usage limits. If Codex is near its limit, route to `-e claude -m sonnet` or `-e opencode`.
> 3. **Route.** Pick the engine/model per task using the matrix below. Trivial → cheap model.
> 4. **Delegate.** `agent.sh run -e <engine> -m <model> -t <name> -C <dir> "<scoped prompt>"`.
>    For a batch of parallel workers use one `agent.sh fan -t <base> -C <dir> "p1" "p2" ...`
>    (spawns `<base>-01`, `<base>-02`, ... in the background) instead of hand-looping `run`.
>    Give parallel workers only NON-overlapping files. Each keeps its own `PROGRESS.<task>.md`.
> 5. **Watch.** `agent.sh list` / `agent.sh status <name>`. If a task is `waiting`, answer it with
>    `agent.sh reply <name> "<answer>"`. If `stalled`/`error`, read its log and re-scope.
> 6. **Verify.** For every finished task, read the diff yourself — never trust "done" blindly. Run
>    tests where relevant. Reject and re-delegate anything wrong.
> 7. **Integrate & commit.** YOU own git. Workers must not commit. You stage, review, and commit.
>
> **Rules:** NATIVE-FIRST — spawn your OWN engine's subagents natively, agent.sh is only for
> foreign engines (from Claude Code: claude models → native Agent tool, never `agent.sh -e claude`;
> from Codex: codex models → native codex subagents; the wrapper bridges the rest);
> small tasks over big ones; exact scope over open-ended "figure it out"; keep the hardest
> reasoning (architecture, security, tricky bugs) either for yourself or a top-tier model; never let
> two parallel workers touch the same file; clean up finished tasks with `agent.sh clean` when done.

Copy the block above as the system/first message when you want a model to run an orchestration session.

---

## Model matrix — which model for what

Pick the **cheapest model that will succeed**. Reasoning tokens dominate cost, so effort/model choice
matters more than prompt wording.

> Claude-model entries below are for NON-Claude orchestrators (e.g. Codex driving the wrapper);
> from Claude Code spawn those tiers via the native Agent tool instead (NATIVE-FIRST rule above).

| Task type | First choice | Notes / alternatives |
|---|---|---|
| Trivial: rename, one-line fix, text/doc tweak, run tests | `-e codex -m spark` (`gpt-5.3-codex-spark`) or `-e claude -m haiku` | Cheapest. "не жалко" for test runs. |
| Regular coding / refactor / docs | `-e codex` (default `gpt-5.6-terra`, medium) **or** `-e claude -m sonnet` | Sonnet is a fine everyday default too; use it when Codex limits are tight. |
| Harder reasoning / tricky bug / careful refactor | `-e codex -m high` (`gpt-5.6-sol`, high effort) | Bump effort, not necessarily model. |
| Deepest / architecture / security review | `-e claude -m opus`, or keep it yourself | Reserve top-tier for genuinely hard work. |
| 5.6 variant A/B or if `terra` is rate-limited | `-m sol` (`gpt-5.6-sol`) / `-m luna` (`gpt-5.6-luna`) | Alternative 5.6 models. **Observed speed (n=1): luna 41s < sol 56s < terra 105s.** In that same run only `sol` produced code whose own tests passed (luna/terra picked non-palindrome examples) — `terra` is the default per user preference, so still verify its output. |
| Local / offline / free | `-e opencode -m lmstudio/<model>` or `-m zai/<model>` | opencode's free `opencode/*` models work but are slow; prefer an authed model. |

**Engine quick facts (verified 2026-07-09):**
- **codex** — default engine (ChatGPT sub). 5.6 family needs **codex-cli >= 0.144**. Watch usage limits (`agent.sh doctor`).
- **claude** — `sonnet` (default, high effort), `opus`, `haiku`. Good when Codex is limited.
- **opencode** — works via `--auto`; **pass an authed `-m`** (`zai/...`, `lmstudio/...`); free models are slow.
- **gemini** — needs `GEMINI_API_KEY` (Google sign-in is geo-blocked for some accounts); unavailable until a key is set.

**Token economy (already on by default):** `--terse` (concise output) and per-task `PROGRESS.md` are
on by default. Add `--no-terse` for exploratory work, `--no-progress` for throwaway one-shots. The
biggest lever is still model/effort — drop to `spark`/`haiku`/`-f low` for easy work.
