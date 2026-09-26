# Kerkov

Open-source AI for penetration testing, security research, and CTFs. Kerkov runs
in your terminal, executes real tools against real targets, and keeps the
engagement — findings, credentials, bulky tool output — recoverable across
context compaction and across sessions.

!!! danger "Authorized use only"
    Kerkov executes commands and performs active security testing. Use it only
    on systems you own or have explicit written permission to test, and keep
    every action inside the agreed scope.

!!! note "This site is being rebuilt"
    Kerkov's documentation is being rewritten from the source code
    ([issue #200](https://github.com/Kerkov-Security/kerkov/issues/200)). Only
    pages verified against the code as it ships today appear in the navigation;
    everything else is still in the repository but deliberately unpublished.
    Until the rebuild reaches the operator guides, the
    [README](https://github.com/Kerkov-Security/kerkov#readme) is the most
    complete starting point.

## What it is

Kerkov is a conversational CLI that pairs a model of your choosing — Anthropic,
Gemini, NVIDIA NIM, OpenRouter, DeepSeek, Ollama or Codex — with a tool suite
built for security work: process execution with real PTY sessions, HTTP,
browser automation, SBOM and vulnerability scanning, SAST, CVE intelligence,
and web search. Every tool call passes through approval rules you control, on
an axis independent of how much narration you want.

## Where to go next

<div class="grid cards" markdown>

-   **[Agent subsystem](agent-subsystem.md)**

    Delegating work to isolated agents: spawning, naming, attaching, operator
    review, and the budgets that bound a run.

-   **[Process management](concepts/process-management.md)**

    The PTY and subprocess adapters behind `exec` and `process` — spawning,
    timeouts, output buffering, key encoding and process-tree teardown.

-   **[Web architecture](concepts/web-architecture.md)**

    The Django/Channels console: its runtime boundary, event contract, and the
    security posture of a single-operator loopback UI.

</div>

## Install

```bash
uv tool install kerkov
kerkov
```

Full installation options — manual, Windows, from source, Docker Compose — are
in the [README](https://github.com/Kerkov-Security/kerkov#quick-start) until the
`get-started` section of this site lands.
