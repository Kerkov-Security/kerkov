# Agent Subsystem

Self-contained sub-task delegation: the master conversation hands a clear
instruction to an isolated agent, which runs with a restricted tool set
and reports back a structured result. The noisy intermediate work never
enters the master's context.

## Where things live

| Module | Responsibility |
|---|---|
| `src/kerkov/core/conversation_engine.py` | Shared tool-execution loop (`ConversationEngine.run_loop`). Drives both the master CLI and any agent. |
| `src/kerkov/agents/models.py` | `AgentProfile` pydantic model + `effective_tools()` whitelist intersection. |
| `src/kerkov/agents/base_agent.py` | Built-in profiles: `recon`, `analysis`, `exploit`, `general`, `container-operator`. |
| `src/kerkov/agents/agent_manager.py` | Loads built-ins + `AGENT.md` files; exposes the index injected into the master's system prompt. |
| `src/kerkov/agents/report_tool.py` | `report` tool — every agent has it; calling it terminates the agent loop. |
| `src/kerkov/agents/agent_runner.py` | Runs one agent's isolated loop and returns an `AgentRunReport`. |
| `src/kerkov/agents/agent_registry.py` | Process-wide depth + total-agent caps. |
| `src/kerkov/agents/agent_tool.py` | `delegate` tool — the master calls this to spawn N agents at once. |

## How a delegation flows

1. Master LLM emits a `delegate` tool call with a list of `AgentTaskSpec`s.
2. `SpawnAgentTool` (REQUIRED approval) shows the user the batch — profile,
   instruction, effective tools per task — and asks for one approval.
3. On approval, `SpawnAgentTool.run()`:
   - Checks the depth + total caps via `AgentRegistry`.
   - For each task, validates that `task.allowed_tools` only narrows the
     profile's whitelist, builds an `AgentRunner`, and includes a nested
     `delegate` tool only if the depth cap permits another level down.
   - `asyncio.gather`s all runners — the caller blocks.
4. Each `AgentRunner`:
   - Builds a fresh history (profile system prompt + instruction).
   - Creates its own `EngagementStore` (linked to the parent by
     `parent_engagement_id`).
   - Runs the shared `ConversationEngine` with a `QuietSink` and
     `WhitelistAutoApproval`.
   - The engine's `terminate_after_tool` hook stops the loop immediately when
     the agent calls `report`.
5. `SpawnAgentTool` concatenates the reports into one `ToolResult` and
   returns it to the master. Each agent's `engagement_id` is in the
   metadata so the user can open the full transcript.

## Container lifecycle delegation

Docker Compose startup, readiness checks, status, and shutdown are delegated to
the built-in `container-operator` profile. It uses `container_lifecycle` rather
than streaming Docker through `exec`. The lifecycle tool performs its polling
locally, writes complete pull/build/status output under
`artifacts/container-logs/` in the agent workspace, and returns one compact
result containing readiness, containers, ports, the artifact path, and cleanup
command. This keeps repetitive progress output out of both the agent and master
contexts.

## Defining your own agent

Drop an `AGENT.md` into `agents/<name>/AGENT.md`:

```yaml
---
name: my-agent
description: One-line summary the master LLM sees in the index.
allowed_tools:
  - read
  - http
max_iterations: 20
---

(System prompt body — the agent reads this verbatim.)
```

The `allowed_tools` list is the **maximum** tool whitelist for spawns of
this profile. A spawn call may narrow it but never widen it; names not in
the whitelist are rejected.

`recall` is subject to the same rule. Every built-in profile whitelists it,
so agents can read what the engagement already gathered instead of re-running
it; a user-defined profile that omits the name does not get the tool.

A user-defined profile may shadow a built-in (a warning is logged) — the
user opted in by writing it.

## Configuration

Env vars (see `src/kerkov/core/config_manager.py`):

| Key | Default | Purpose |
|---|---|---|
| `AGENT_MAX_DEPTH` | 2 | Master → agent → agent. At the cap, `delegate` is excluded from the agent's tool set. |
| `AGENT_MAX_TOTAL` | 20 | Hard ceiling on agents spawned in this process. |
| `AGENT_MAX_BATCH` | 5 | Largest task list one `delegate` call may carry. |
| `AGENT_MAX_ITERATIONS` | 30 | Process-wide cap on each agent loop; profile limits can be lower. |

## Persistence

Agent runs write to the same `~/.kerkov/engagements/` directory as
conversations, with `kind="agent"` and back-links to the parent engagement
and parent `delegate` tool call. The
`/engagements` command hides agent sub-runs by default; the trailing
note shows how many were elided.
