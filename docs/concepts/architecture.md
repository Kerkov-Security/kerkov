# Architecture

!!! note "Contributor-facing"
    This page and the rest of *Concepts* describe how Kerkov is built and why.
    To *use* Kerkov, start with [Get started](../get-started/install.md) and the
    [guides](../guides/modes-and-autonomy.md).

Kerkov is a conversational CLI that pairs a language model with a suite of tools
for security work. A model you choose drives the conversation; the tools execute
real work against real targets; and everything between the two — approval, output
handling, memory, compaction — is machinery in the middle that the rest of these
pages describe.

## The shape of a session

```
  You ─── prompt ───▶  KerkovCLI
                          │
                          ▼
                   ConversationEngine.run_loop
                     │           ▲
          provider   │           │  tool results
          completion ▼           │
                   Provider    Tools ──▶ approval ──▶ execute
                (Anthropic,   (read, exec, http,
                 Gemini, …)    zap, delegate, …)
                          │
                          ▼
                  knowledge base · findings ledger · compaction
```

One turn: your prompt goes into the history, the engine asks the provider for a
completion, and if the response contains tool calls the engine approves and runs
each one, appends the results, and asks the provider again — looping until the
model answers with prose instead of a tool call, or a cap is hit. That loop is the
subject of [the conversation loop](conversation-loop.md); the pieces around it are
below.

## Providers

Kerkov talks to seven LLM backends, all behind one `BaseLLMProvider` interface, so
the engine never knows which one it is driving:

`anthropic` (alias `claude`), `gemini` (`google`, `google-gemini`), `nvidia`
(`nim`, `nvidia-nim`), `openrouter` (`open-router`), `deepseek`, `ollama`, and
`codex` (`openai-codex`).

`SUPPORTED_PROVIDERS` and the `create_provider_from_name` factory
(`src/kerkov/providers/__init__.py`) define the set; `ProviderManager`
(`provider_manager.py`) owns initialization and the runtime switch behind
`/provider`. A provider resolves its API key from config or the credential
manager, which can prompt and save; Codex is different, using an OAuth flow that
starts automatically when it is selected.

Providers are not quite interchangeable to the engine in one respect: a provider
declares `caches_prompt_prefix`, and the engine uses that to decide where to place
the mutable [findings ledger](findings-ledger.md) in the request. Anthropic and
DeepSeek cache a stable prefix; the rest do not. That is the one provider
capability the loop reads directly.

An in-flight agent runs against a *shallow copy* of the provider, so a `/model` or
`/provider` switch mid-run does not change the model an agent started on.

## Tools

Tools all extend `BaseTool` and return a `ToolResult`. The suite is assembled by
`ToolSuite.get_tools()`, with a handful more registered by the CLI once the session
exists. The full set, their parameters, and their approval behaviour are in the
[tools reference](../reference/tools/index.md); the registration order and the
conditional tools are in its [overview](../reference/tools/index.md#the-suite).

Two design choices matter at the architecture level:

- **Every tool declares its own approval breadth** (`approval_scope`) — how far a
  single approval may generalize into a saved rule. It is abstract, not defaulted,
  so a new tool cannot inherit someone else's idea of "broad enough". See
  [approvals](../guides/approvals.md).
- **Bulky tools keep their full output as workspace artifacts** and return bounded
  summaries, so a scan cannot flood the context window. This ties into the
  [knowledge base](knowledge-base.md) and [compaction](context-and-compaction.md).

## Modes and the two autonomy axes

A **mode** (`RECON`, `AUDIT`, `OFFENSIVE`, `CTF`) carries a system prompt and a
posture. Independently, **autonomy** is a preset over two axes that can each be
pinned on their own: narration **verbosity** and approval **gating**. Keeping them
independent is deliberate — "how much it tells me" and "how much it asks me" are
different questions. See [modes and autonomy](../guides/modes-and-autonomy.md) and
[approvals](../guides/approvals.md).

## Memory that outlives the context window

Two subsystems keep an engagement's facts recoverable past the point where old
messages are summarized away:

- The [knowledge base](knowledge-base.md) (`recall`) — a searchable archive of
  every tool result, so bulky output stays retrievable after it leaves the
  conversation.
- The [findings ledger](findings-ledger.md) (`ledger`) — a small structured record
  of confirmed facts, re-injected into context every turn so it survives
  [compaction](context-and-compaction.md) verbatim.

Both are stored locally per engagement tree, and both are shared down to delegated
agents.

## Agents

Work can fan out to isolated sub-agents through `delegate`: each runs the same
`ConversationEngine` loop with a restricted tool set and reports back a structured
result, keeping its noisy intermediate work out of the master's context. The
[agent subsystem](agent-subsystem.md) covers this in full; the operator's view is
the [agents guide](../guides/agents.md).

## Persistence

Conversations are stored as JSONL under `~/.kerkov/engagements/`, resumable with
`--continue` or `--resume`. Each engagement tree also gets a SQLite database
alongside it for the knowledge base and ledger. See [engagements](../guides/engagements.md).

## The web console

A Django/Channels web console (`kerkov-web`) runs the same runtime over a
loopback-only, unauthenticated UI. See [web architecture](web-architecture.md).

## Where to go next

- [The conversation loop](conversation-loop.md) — the engine at the centre of the
  diagram, in detail.
- [Context and compaction](context-and-compaction.md) — how a long engagement
  stays inside the context window.
- [Knowledge base](knowledge-base.md) and [findings ledger](findings-ledger.md) —
  the two memory subsystems.
- [Agent subsystem](agent-subsystem.md) — delegation internals.
