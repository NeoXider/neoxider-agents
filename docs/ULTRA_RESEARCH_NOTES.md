# Ultra research notes

Public source review, 2026-11-18. Local contracts were read from the spec
worktree. External tools and providers were not installed or executed.

| Source and license | What Ultra takes from it |
| --- | --- |
| **Claude Code Workflow** · Commercial product documentation. [Workflows and Ultracode](https://code.claude.com/docs/en/workflows) · [Official code-review plugin](https://github.com/anthropics/claude-code/blob/main/plugins/code-review/commands/code-review.md) | Code-owned control flow, schema-constrained handoffs, per-item verification, progress phases and completed-call replay. Its review plugin validates findings in fresh parallel calls and filters out claims that fail validation. Ultra uses an approved launch envelope and dependency-keyed reuse across independent branches. |
| **opencode-swarm** · MIT. [License](https://github.com/ZaxbyHub/opencode-swarm/blob/main/LICENSE) · [README](https://github.com/ZaxbyHub/opencode-swarm) · [Architecture](https://github.com/ZaxbyHub/opencode-swarm/blob/main/docs/architecture.md) | Plan before dispatch, disjoint waves, and phase advancement based on recorded tests, review artifacts and exact route receipts. Reuse the evidence pattern. The in-harness permission hooks and project-local plan store belong to that plugin. |
| **Claude Flow / Ruflo** · MIT. [License](https://github.com/ruvnet/ruflo/blob/main/LICENSE) · [Workflow plugin](https://github.com/ruvnet/ruflo/tree/main/plugins/ruflo-workflows) · [Verified-outcome changelog](https://github.com/ruvnet/ruflo/blob/main/CHANGELOG.md) · [Releases](https://github.com/ruvnet/ruflo/releases) | Persist lifecycle transitions and feed routing only from verified outcomes. Its MCP workflow state machine and native JavaScript surface are distinct. Keep P0 as the sole routing/memory integration and leave adaptive neural routing and swarm consensus out of P1. |
| **Pi Subagents** (tintinweb) · MIT. [License](https://github.com/tintinweb/pi-subagents/blob/master/LICENSE) · [Workflow guide](https://github.com/tintinweb/pi-subagents/blob/master/docs/workflows.md) | Closest open-source script analogue. Adopt explicit labels, actual-route/attempt telemetry and visible replay provenance. Its changed-prefix invalidation and child session continuation suit its own runtime. Ultra keys completed calls by inputs and dependencies. |
| **DSH Subagents** (geekyfoxlab) · MIT. [License](https://github.com/geekyfoxlab/dsh-subagents/blob/main/LICENSE) · [Review loop](https://github.com/geekyfoxlab/dsh-subagents/blob/main/skills/review-loop.md) · [Upstream Pi extension and license](https://github.com/nicobailon/pi-subagents) | This DSH plugin is a port of nicobailon's separate Pi extension. Its Markdown workflow skills specify a parent-controlled, bounded review loop, fresh specialist contexts and a one-writer default. Adopt bounded loops and concrete severity/evidence handoffs. DSH's fork/native tool behavior remains host-specific. |
| **LangGraph** · MIT. [Repo and license](https://github.com/langchain-ai/langgraph) · [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) · [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) · [Fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance) | Separate run checkpointing from cross-run memory, and bound retry at the step boundary. The general graph runtime and model adapters add dependencies P1 can avoid. |
| **OpenHands Software Agent SDK** · MIT. [Repo and license](https://github.com/OpenHands/software-agent-sdk) · [Maintainers' SDK paper](https://arxiv.org/abs/2511.03690) | Model-configured delegated conversations, event-backed persistence and separate usage telemetry. Ultra uses the existing neoxider process/session runtime and keeps a checkpoint at each managed call boundary. |

Claude's size guideline is advisory. The official documentation describes
fewer than 5, 10, or 50 agents for small, medium, and large. It separately
describes a default concurrency of up to 16 and an advisory large-run warning
above 25 agents or 1.5 million projected tokens in applicable modes. Completed
calls replay within a session under the documented ordering and version rules.
A failed verifier leaves a claim unverified. [Claude Workflow docs](https://code.claude.com/docs/en/workflows)

Local references: [skill and ledger](../SKILL.md), [SDK](ORCHESTRATOR_API.md),
[core](CORE_INTEGRATION.md), [events](DURABLE_EVENTS.md), [fan startup barrier](../neoxider_agents/fan.py).
The P0 plan path was absent from the committed target branch, the local P0 ref,
and reachable history. The P0 remote branch was unpublished when checked.
P0's exact field names and record semantics remain an integration check.
