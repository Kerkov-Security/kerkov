# Kerkov Agent Subsystem — Implementation Plan (v1)

Status: **Design / not started**
Supersedes the design half of `kerkov_vs_claude_agent_architecture.md` (keep that doc as
the original comparison reference; this is the doc to build from).

---

## 1. Goal

Let the main conversation **delegate a self-contained sub-task** to an isolated agent so
that the noisy intermediate work (tool calls, dead ends, long outputs) never enters the
main conversation's context. The main conversation receives only a **structured final
report**.

Motivating use case: a CTF or multi-path engagement where the master spawns several
agents to investigate different paths at once — each with its own context — and then
synthesizes their reports.

### Terminology

| Term | Meaning |
|---|---|
| **Agent** | The executor — an isolated conversation loop with a restricted tool set. |
| **Agent profile** | The reusable definition of an agent: name, system prompt, *maximum* tool whitelist. Built-in or user-defined. |
| **Task** | The delegated unit of work: an instruction + success criteria, supplied at spawn time. |
| **Master / parent** | The conversation (or agent) that spawned the agent. |
| **Report** | The structured result an agent returns: `status`, `findings`, `next_steps`. |

We keep the industry-standard term **agent** / **subagent**. It already means exactly an
ephemeral, isolated, task-scoped runner — no baggage to escape, and every contributor
already understands it.

---

## 2. v1 Scope (locked decisions)

- **Blocking execution.** When the master spawns agents, its turn **blocks until every
  spawned agent has finished**. There is no mid-run polling in v1.
- **Batch spawn — N agents per call.** A single `SpawnAgentTool` call takes a *list* of
  task specs and launches all of them **concurrently** (`asyncio.gather`); the master
  blocks until every agent in the batch completes, then receives all reports together.
  This is the v1 mechanism for "multiple agents at once" — see §7 for why batch (rather
  than N separate tool calls) is the chosen design.
- **Nested delegation also blocks.** If an agent spawns its own sub-agent, it blocks
  until that sub-agent finishes — same model, one level down.
- **Dynamic agents.** Agents are not limited to a fixed code-defined set. Three flavors
  are supported (see §4).
- **Per-agent persisted history.** Every agent run is saved as its own engagement file
  (see §6).

### Explicitly deferred to v2

- **Status-report-of-children.** Querying a running agent's progress requires a
  background job model (non-blocking spawn + registry polling). It is incompatible with
  v1's blocking model and is deferred. The architecture below leaves room for it.
- Shared "findings" scratchpad between sibling agents (v1: agents are fully isolated;
  the master synthesizes and re-delegates).
- Per-agent live progress UI / streaming.
- Recursion beyond the depth cap.

---

## 3. Architecture Overview

```
src/agents/
├── __init__.py
├── base_agent.py        # AgentProfile model + built-in profiles
├── agent_manager.py     # Loads built-in + user-defined (AGENT.md) profiles
├── agent_runner.py      # Runs ONE agent's isolated conversation loop
├── agent_registry.py    # Per-process run tracking: depth + total-agent budget
├── report_tool.py       # ReportTool — agents call this to finish with a structured report
└── agent_tool.py        # SpawnAgentTool (BaseTool) — the master calls this to delegate
```

### Data flow (v1, blocking)

```
Master turn
  → LLM emits one or more SpawnAgentTool calls in a single turn
  → SpawnAgentTool (REQUIRED approval) — user approves the delegation once
  → For each call: AgentManager resolves the profile, AgentRunner is constructed
  → asyncio.gather(runner.run() for each)        # concurrent, master blocks here
       each AgentRunner:
         - fresh Message history (agent system prompt + task instruction only)
         - restricted tool set (profile whitelist ∩ depth rules) + ReportTool
         - its own EngagementStore file (linked to parent)
         - runs the shared tool loop until ReportTool is called / no tool calls / cap
  → Each agent's structured report is returned as a ToolResult to the master
  → Master LLM continues with the agents' findings only
```

---

## 4. Agent Profiles (the three flavors)

An **agent profile** declares the agent's identity and — critically — its **maximum**
tool whitelist. This whitelist is the security boundary, enforced in code.

1. **Built-in profiles** (`base_agent.py`) — code-defined, shipped with Kerkov. Their
   whitelists are fixed in source. Examples:

   | Profile | Max tools | Purpose |
   |---|---|---|
   | `recon` | Read, HTTP, Exec*, Process* | Enumeration, OSINT, scanning |
   | `analysis` | Read | Log/code/artifact review (no execution, no write) |
   | `exploit` | Read, Write, HTTP, Exec, Process | Exploitation, payload work |
   | `general` | Read, HTTP, Exec, Process | Default for ad-hoc delegation |

   *recon gets Exec/Process in a constrained form (see §7 stateful-tool note).

2. **User-defined profiles** — loaded from `AGENT.md` files, the same pattern Kerkov
   already uses for skills (`SkillManager` → `AgentManager`). An `AGENT.md` declares
   `name`, `description`, `system_prompt`, and `allowed_tools`. This is how a user
   "defines a subagent to do whatever task we want."

3. **Ad-hoc delegation** — the master spawns against a base profile (default `general`)
   and supplies the full instruction inline. No profile file needed.

### The one security rule

> An agent profile declares its **maximum** tool set. A spawn call may **narrow** that
> set (request a subset) but may **never widen** it.

The effective tool set is `requested_tools ∩ profile.allowed_tools`, computed in code.
Requested tools outside the profile whitelist are rejected instead of silently dropped.
This keeps "fully dynamic" agents from quietly escalating privilege — important in a
pentest tool where `recon` genuinely must not be able to call `WriteTool`.

### `AgentProfile` model (Pydantic)

```python
class AgentProfile(BaseModel):
    name: str
    description: str            # shown to the master LLM so it can pick a profile
    system_prompt: str          # stripped-down, task-completion focused
    allowed_tools: List[str]    # MAX whitelist — tool names
    max_iterations: int = 30    # per-agent loop cap (lower than the master's 100)
```

`AgentManager` mirrors `SkillManager`: discovers built-in + `AGENT.md` profiles, exposes
`get_profile(name)` and `get_index_summary()` (a compact list injected into the master's
system prompt so the LLM knows what profiles exist).

---

## 5. Refactoring `kerkov_cli.py` — extracting the conversation engine

The tool-execution loop is `KerkovCLI.get_response()` (`src/cli/kerkov_cli.py:329-528`).
It is the engine we want to reuse, but it currently reads ~15 things off `self` and the
module-global `console`. An agent must run the *same* loop against a *different* history,
tool set, engagement file, approval policy, and output target. So the loop has to be
extracted. This refactor is approved and is **Phase 1**.

### What the loop is currently entangled with

| Concern | Today | Agent needs |
|---|---|---|
| Output | module-global `console.print` / `console.status` everywhere | quiet — buffered/prefixed, no spinner contention across concurrent agents |
| History + persistence | `self.conversation_history`, `self.engagement_store` via `_append_message_to_conversation` | its **own** history + its **own** engagement file |
| Tools / lookup | `self.tools`, `self._get_tool` | a restricted, per-agent tool list |
| Approval | `self.tool_approval_helper` + `self.approval_mode_manager` | auto-approve-within-whitelist (§7) — never an interactive prompt |
| Interrupts | `self.input_manager` start/stop/`run_cancellable` | none — agents are not interactively interruptible in v1 |
| Transient prompts / non-interactive retry | `self._transient_system_prompts`, `_looks_like_deferred_execution` | not needed (agent prompt already enforces action) |
| Compaction / usage | `_check_compaction`, `_record_usage`, `_output_token_usage` | reused against the agent's own history + tracker |
| Per-turn caps | `_resolve_max_response_tokens`, `max_iterations = 100` | per-agent `max_iterations` (≈30) |

### Target structure

Extract a provider-agnostic engine — proposed `src/core/conversation_engine.py`:

```python
@dataclass
class LoopSession:
    provider:          BaseLLMProvider
    history:           List[Message]          # mutated in place
    tools:             List[BaseTool]
    approval:          ApprovalStrategy        # §7
    output:            OutputSink              # §7 — console adapter | quiet adapter
    max_iterations:    int
    engagement_store:  Optional[EngagementStore] = None
    compactor:         Optional[ContextCompactor] = None
    token_tracker:     Optional[TokenTracker] = None
    interrupt:         Optional[InterruptHandle] = None   # master sets it; agents pass None
    transient_prompts: List[str] = field(default_factory=list)

class ConversationEngine:
    async def run_loop(self, session: LoopSession) -> LLMResponse:
        """The loop currently inlined in get_response(), now reading only `session`."""
```

Two small abstractions decouple the loop from the CLI:

- **`OutputSink`** (protocol) — `thinking()`, `tool_start(name, params)`,
  `tool_succeeded(result)`, `tool_failed(result)`, `assistant_text(md)`, `info(msg)`,
  `final(md)`. `ConsoleSink` wraps today's `console` calls verbatim (master behavior
  unchanged). `QuietSink` logs to the agent's logger + an in-memory buffer, no spinner.
- **`ApprovalStrategy`** (protocol) — `async approve(tool, params) -> ApprovalResult`.
  `InteractiveApproval` wraps `ToolApprovalCliHelper` + `ApprovalModeManager` (current
  behavior). `WhitelistAutoApproval` is the agent strategy (§7).

Helpers `_extract_raw_blocks` and `_format_tool_result` move to the engine module
(they are pure functions already).

### Result

- `KerkovCLI.get_response()` shrinks to: build a `LoopSession` from `self` (with
  `ConsoleSink`, `InteractiveApproval`, `interrupt=self.input_manager`,
  `max_iterations=100`, transient prompts), call `engine.run_loop(session)`, done.
  Master behavior is byte-for-byte unchanged — verified by existing CLI tests.
- `AgentRunner` builds a `LoopSession` with `QuietSink`, `WhitelistAutoApproval`,
  `interrupt=None`, its own fresh `history`/`EngagementStore`, the profile's
  `max_iterations`, and the restricted tool list.

One loop, two callers, zero divergence. The non-interactive retry path
(`_looks_like_deferred_execution`) stays master-only — gated on
`session.transient_prompts` being non-empty, which agents leave empty.

---

## 6. Persistence — answering "separate engagement_store output file?"

**Recommendation: do not invent a new format. Reuse `EngagementStore` and the JSONL
engagement format — one file per agent run — linked to the parent by metadata.**

Reasoning:

- `EngagementStore` is already designed as **one conversation per `.jsonl` file**. An
  agent run *is* a conversation. It fits with zero format work.
- A separate file (rather than appending agent messages into the parent file) keeps the
  parent transcript clean — which is the whole point of delegation — and keeps each
  agent independently inspectable and `/resume`-able later.
- Existing tooling (`/engagements`, `/resume`, the JSONL reader, raw-block handling)
  works on agent transcripts for free.

### Changes required

1. **`EngagementMetadata`** — add optional link fields (default `None`, so existing files
   still parse):

   ```python
   kind: str = "conversation"          # "conversation" | "agent"
   parent_engagement_id: Optional[str] = None
   parent_call_id: Optional[str] = None   # the SpawnAgentTool call_id
   agent_type: Optional[str] = None       # profile name
   task_instruction: Optional[str] = None # truncated, for listings
   ```

2. **`EngagementStore.start_engagement(...)`** — accept the new optional fields (or add a
   sibling `start_agent_engagement(...)` that sets `kind="agent"`).

3. Each `AgentRunner` gets its **own `EngagementStore` instance** and calls
   `start_engagement(kind="agent", parent_engagement_id=..., parent_call_id=..., ...)`.
   Files land in the same `~/.kerkov/engagements/` directory as conversations.

4. **`/engagements` listing** — filter out `kind == "agent"` by default so the list isn't
   flooded with sub-runs; optionally add `/engagements --agents` or nest them under their
   parent in the display.

Directory grouping (e.g. `~/.kerkov/engagements/<parent_id>/agents/...`) is a possible
later polish; v1 keeps the flat directory + metadata link because `EngagementStore` is
already flat and the link fields make grouping a pure display concern.

---

## 7. Key mechanics

### Batch spawn — why one call with N tasks, not N calls

The master must be able to launch N agents "at a given time." The natural-looking option
— have the LLM emit N separate `SpawnAgentTool` calls in one turn — does **not** give
concurrency: `ConversationEngine.run_loop` executes a turn's tool calls **serially**
(`for tool_call in response.tool_calls`, `src/cli/kerkov_cli.py:402`). N separate spawn
calls would therefore run one agent fully before starting the next.

So v1 makes `SpawnAgentTool` **batch-native**: a single call carries a list of task
specs, and the tool's own `run()` does the `asyncio.gather` over N `AgentRunner`s
internally. Concurrency lives inside one tool call — no change to the engine's serial
tool-execution semantics is required. (Generalized concurrent execution of read-only
tool calls within a turn is a separate, larger change; explicitly out of scope here.)

Every agent gets a **`ReportTool`** (always in its tool set, never whitelisted away). The
agent's system prompt instructs it to finish by calling `report` with:

```python
class ReportParams(ToolParams):
    status: Literal["completed", "partial", "stuck"]
    findings: str           # what was discovered / done
    next_steps: str = ""    # recommended follow-up, esp. when partial/stuck
```

`AgentRunner` ends the loop when `report` is called and returns that structured payload.
Fallbacks:

- Agent stops emitting tool calls without calling `report` → wrap its final text as
  `status="completed", findings=<text>`.
- `max_iterations` hit → `status="stuck"` with whatever text is available.

Telling the agent to call `report(status="stuck", ...)` instead of spinning is the
primary "stuck" signal; the iteration cap is the backstop. (An LLM rarely volunteers
"I'm stuck" — the prompt must explicitly make `report` the way out.)

The `SpawnAgentTool` returns the report to the master as a `ToolResult` — `success` maps
from `status` (`completed`/`partial` → success, `stuck` → failure), `output` is a
formatted `findings` + `next_steps`, `metadata` carries the raw fields plus the agent's
engagement id (so the user can open the full transcript).

### Depth and breadth limits (preventing runaway delegation)

Two independent failure modes, two independent caps — tracked in `AgentRegistry`:

- **Depth cap** — config `AGENT_MAX_DEPTH` (default `2`: master → agent → agent). `depth`
  is passed into each `AgentRunner` and incremented. At the cap, `SpawnAgentTool` is
  **excluded from the agent's tool set** — it simply cannot delegate further.
- **Total-agent budget** — config `AGENT_MAX_TOTAL` (default `20`): the registry counts
  every agent spawned in the current process across the whole run. Past the budget,
  `SpawnAgentTool` returns a failure `ToolResult` instead of spawning. This catches wide
  fan-out, which a depth cap alone misses.
- **Per-agent iteration cap** — `min(AgentProfile.max_iterations, AGENT_MAX_ITERATIONS)`.
  A stuck agent dies on its own without burning the whole budget.

### Approval model during agent runs

Concurrent agents must not interleave approval prompts on the shared console. v1 rule:

> **The `SpawnAgentTool` call itself carries `ToolApprovalLevel.REQUIRED`.** The user
> approves the *delegation* once (seeing the profile, instruction, and tool whitelist).
> Once approved, the agent runs with approvals **auto-granted for tools within its
> whitelist** — the whitelist is the constraint the user already signed off on.

This is implemented via the injected `ApprovalStrategy`: the master uses the existing
interactive `ToolApprovalCliHelper`; an agent uses an `AutoApproveWithinWhitelist`
strategy. (A tool outside the whitelist can never be called anyway — it isn't in the
agent's tool list.) `REQUIRE_SUDO_APPROVAL` and similar hard gates still apply inside the
agent.

### Concurrency safety

- **Provider** — agents share the master's provider instance. The Anthropic async client
  is safe under concurrent `complete()`; confirm the same for Gemini/Ollama/Codex paths,
  else give each runner its own provider instance. Concurrent runs multiply rate-limit
  and cost exposure — the total-agent budget is the guard.
- **Stateful tools (`ExecTool` / `ProcessTool`)** — these share the process supervisor and
  `session_registry`. Two concurrent agents must not collide on session names. v1
  mitigation: each `AgentRunner` is assigned a unique `agent_run_id`, and stateful tools
  given to agents namespace their session ids with that prefix. If that is not ready in
  time, the conservative v1 fallback is to withhold `ExecTool`/`ProcessTool` from
  concurrently-spawned agents and grant them only to serially-spawned ones. **Decision
  needed — see §10.**

### Output / UI

v1 keeps it simple: on spawn, print `→ Delegating N task(s): <profiles>`; while
`asyncio.gather` runs, a single `console.status` spinner (`Running N agent(s)...`); on
completion, print each agent's report (status badge + findings, truncated) plus its
engagement id. Per-agent live streaming is deferred.

---

## 8. `SpawnAgentTool` interface (batch)

```python
class AgentTaskSpec(BaseModel):
    agent_type: str = Field(description="Profile name: built-in, user-defined, or 'general'")
    instruction: str = Field(description="Clear, self-contained task for the agent")
    success_criteria: str = Field(description="How the agent knows it is done")
    allowed_tools: Optional[List[str]] = Field(
        default=None,
        description="Optional subset of the profile's tools; cannot widen the profile.",
    )

class SpawnAgentParams(ToolParams):
    tasks: List[AgentTaskSpec] = Field(
        description="One or more tasks to delegate. All run concurrently; "
                    "the master blocks until every agent reports back.",
        min_length=1,
    )
```

Behavior:

- `get_approval_level()` → `REQUIRED`. The approval prompt shows **all** tasks in the
  batch (profile, instruction, effective tool set per task) so the user signs off on the
  whole fan-out at once.
- A single batch is capped at `AGENT_MAX_BATCH` tasks (config, default `5`) to keep the
  fan-out and the approval prompt sane; the total-agent budget (§7) still applies across
  batches.
- The tool excluded from every agent's own tool set at `depth >= AGENT_MAX_DEPTH`.
- `run()` resolves each task's profile via `AgentManager`, constructs an `AgentRunner`
  per task, and `asyncio.gather`s them. Per task: `instruction` + `success_criteria` are
  formatted into the **first user message** of the agent's fresh history; the profile
  `system_prompt` is the agent's system message.
- Returns one `ToolResult` whose `output` concatenates each agent's report (labelled by
  agent_type + a short index) and whose `metadata` carries the structured reports +
  per-agent engagement ids. `success` is `True` if every agent finished
  `completed`/`partial`; `False` if any came back `stuck` (the master still sees all
  reports either way).
- One agent failing or raising does not abort the batch — `gather` is collected with
  per-task error capture so a crashed agent becomes a `status="stuck"` report.

The master LLM is told (via `AgentManager.get_index_summary()` injected into its system
prompt) which profiles exist and that a single `delegate` call may carry several tasks
for parallel investigation.

---

## 9. Implementation phases

1. **Phase 1 — `kerkov_cli.py` refactor (see §5).** Create `ConversationEngine` +
   `LoopSession` in `src/core/conversation_engine.py`; add `OutputSink`
   (`ConsoleSink`/`QuietSink`) and `ApprovalStrategy` (`InteractiveApproval`/
   `WhitelistAutoApproval`) abstractions; move `_extract_raw_blocks`/`_format_tool_result`
   to the engine module; reduce `KerkovCLI.get_response()` to a `LoopSession` builder +
   `engine.run_loop()` call. Master behavior must be unchanged — existing CLI tests are
   the regression gate. This is a standalone PR with no agent code yet.
2. **Phase 2 — Profiles.** `AgentProfile`, built-in profiles, `AgentManager` (built-in +
   `AGENT.md` discovery, `get_index_summary()`). Tests: profile loading, whitelist
   parsing.
3. **Phase 3 — Persistence + Runner.** Extend `EngagementMetadata`/`EngagementStore` with
   link fields; build `ReportTool` and `AgentRunner` (fresh history, restricted tools,
   own engagement file, runs `run_tool_loop`). Tests: an agent run produces a linked
   `kind="agent"` engagement file; `report` terminates the loop.
4. **Phase 4 — Batch spawn + concurrency.** `AgentRegistry` (depth + total budget),
   batch `SpawnAgentTool` with internal `asyncio.gather` over N `AgentRunner`s,
   per-task error capture. Wire `SpawnAgentTool` into `KerkovCLI` (append after
   `SkillTool`, like the skill tool). Tests: a batch of N agents runs concurrently and
   all reports return; depth cap excludes the tool; total + batch caps fail gracefully;
   widening the whitelist is rejected; a crashing agent becomes a `stuck` report without
   aborting siblings.
5. **Phase 5 — Polish + docs.** `/engagements` filtering, output formatting, an example
   `AGENT.md`, update `CLAUDE.md` repo layout, `docs/` page.

---

## 10. Open decisions to confirm before Phase 4

1. **Stateful tools for concurrent agents** — go with per-agent session namespacing
   (preferred, more capable) or the conservative withhold-from-concurrent fallback for
   v1? This affects whether parallel `recon`/`exploit` agents can use `ExecTool`.
2. **Loop extraction vs. slimmer copy** — full `run_tool_loop` extraction in Phase 1
   (preferred), or ship `AgentRunner` with a slimmer copied loop and converge later?
3. **Config defaults** — `AGENT_MAX_DEPTH=2`, `AGENT_MAX_TOTAL=20`, `AGENT_MAX_BATCH=5`,
   default `max_iterations=30` per agent. Confirm or adjust.
4. **Compaction inside agents** — do agent runs get context compaction
   (`_check_compaction`)? Recommended yes, since a deep agent run can grow long; it
   reuses the existing compactor against the agent's own history.
