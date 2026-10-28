# Kerkov

Open-source AI for penetration testing, security research, and CTFs. Kerkov runs
in your terminal, executes real tools against real targets, and keeps the
engagement — findings, credentials, bulky tool output — recoverable across
context compaction and across sessions.

!!! danger "Authorized use only"
    Kerkov executes commands and performs active security testing. Use it only
    on systems you own or have explicit written permission to test, and keep
    every action inside the agreed scope.

!!! note "Written from the source"
    Every page here was written against the code as it ships and is checked
    against it in CI ([issue #200](https://github.com/Kerkov-Security/kerkov/issues/200)):
    a tool, slash command or setting that exists without a page fails the build.
    Pages that cannot be verified are deliberately unpublished rather than
    shipped. Still to come: the lab-based walkthroughs — a Vulhub CVE
    reproduction, a Hack The Box machine, and a SQL-injection target — which
    land once each has been run end to end.

## What it is

Kerkov is a conversational CLI that pairs a model of your choosing — Anthropic,
Gemini, NVIDIA NIM, OpenRouter, DeepSeek, Ollama or Codex — with a tool suite
built for security work: process execution with real PTY sessions, HTTP,
browser automation, SBOM and vulnerability scanning, SAST, CVE intelligence,
and web search. Every tool call passes through approval rules you control, on
an axis independent of how much narration you want.

## Where to go next

<div class="grid cards" markdown>

-   **[Install](get-started/install.md)**

    From source, one-line installer, Docker Compose, and the optional
    extras — get Kerkov running in a few minutes.

-   **[Your first engagement](get-started/first-engagement.md)**

    A ten-minute walkthrough: launch, scope a target, approve tool calls, and
    record findings.

-   **[Guides](guides/modes-and-autonomy.md)**

    Task-shaped how-tos — modes and autonomy, approvals, providers, engagements,
    skills, memory, and non-interactive use.

-   **[Reference](reference/cli.md)**

    Every flag, slash command, and configuration variable, checked against the
    source.

</div>

## Install

```bash
git clone https://github.com/Kerkov-Security/kerkov.git
cd kerkov
uv sync --locked
uv run kerkov
```

See [Install](get-started/install.md) for the one-line installer, Windows,
Docker Compose, and the optional extras.
