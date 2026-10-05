<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/kerkov-readme-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/kerkov-readme-light.png">
    <img src="docs/assets/kerkov-readme-light.png" alt="Kerkov" width="480">
  </picture>
</p>

<h1 align="center">Kerkov</h1>

<p align="center">
  <strong>Open-source AI for penetration testing, security research, and CTFs.</strong>
</p>

<p align="center">
  Plan, execute, and document penetration tests from one conversational interface—with the model, tools, and level of human control you choose.
</p>

<p align="center">
  <a href="https://github.com/Kerkov-Security/kerkov/actions/workflows/unit-tests.yml"><img src="https://github.com/Kerkov-Security/kerkov/actions/workflows/unit-tests.yml/badge.svg" alt="Unit tests"></a>
  <img src="https://img.shields.io/badge/Python-3.11--3.14-3776AB?logo=python&logoColor=white" alt="Python 3.11 through 3.14">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-D22128" alt="Apache License 2.0"></a>
  <img src="https://img.shields.io/badge/status-alpha-F97316" alt="Project status: alpha">
  <a href="https://github.com/Kerkov-Security/kerkov/stargazers"><img src="https://img.shields.io/github/stars/Kerkov-Security/kerkov?style=flat&logo=github" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-kerkov-can-do">Features</a> ·
  <a href="#operational-modes">Modes</a> ·
  <a href="#model-providers">Providers</a> ·
  <a href="#documentation">Documentation</a>
</p>

> [!IMPORTANT]
> Kerkov can execute commands and perform active security testing. Use it only on systems you own or have explicit permission to test, and keep every action inside the agreed scope.

## Overview

Kerkov is an open-source, security-focused conversational AI that works alongside you in the terminal. It can inspect code, operate a browser, run and supervise processes, call security services, look up current vulnerability intelligence, and coordinate specialist agents—all while preserving the engagement context for later recall.

It is designed for security practitioners who want the speed of an AI agent without giving up visibility or control. Tool calls pass through configurable approval rules, secrets are collected outside the chat, and intrusive scanners stay behind explicit authorization boundaries by default.

## What Kerkov can do

| | |
| --- | --- |
| **⚡ Act, not just advise**<br>Run commands, edit files, drive browsers, make HTTP requests, and monitor long-running processes from the conversation. | **🧠 Coordinate specialist agents**<br>Delegate recon, analysis, exploitation, or container operations while the main conversation stays focused. |
| **🛡️ Keep the operator in control**<br>Choose strict, normal, permissive, or fully autonomous approval gating independently from narration verbosity. | **🔌 Use the model you prefer**<br>Switch between Anthropic, Gemini, NVIDIA NIM, OpenRouter, DeepSeek, Ollama, and Codex without changing the workflow. |
| **🎯 Work in the right posture**<br>Move between Recon, Audit, Offensive, and CTF modes, each with its own methodology and boundaries. | **📚 Remember the engagement**<br>Resume saved conversations, record verified findings in a ledger, and recall bulky tool output from a local SQLite knowledge base. |

Built for authorized application and infrastructure assessments, security reviews with controlled proof-of-concept validation, reconnaissance and attack-surface mapping, CTF and vulnerability-research workflows, and repeatable security automation from a CLI or CI job.

## Quick start

**Prerequisites:** [uv](https://docs.astral.sh/uv/) and Python 3.11+, and an account or local endpoint for one [supported model provider](#model-providers). Docker is only needed for features that use it (managed OWASP ZAP, SearXNG, container runs); Syft or Grype on `PATH` only for SBOM generation or vulnerability scanning.

On Intel Macs, install the [native build prerequisites](docs/get-started/install.md#intel-macos-build-prerequisites) before syncing dependencies.

Run from source:

```bash
git clone https://github.com/Kerkov-Security/kerkov.git
cd kerkov
uv sync --locked
uv run kerkov
```

On first use, choose a provider and follow the masked credential prompt. Kerkov validates the credential before saving it to your operating-system keyring (or, on headless systems, a user-only file under `~/.kerkov/`). No `.env` file is required for ordinary interactive use.

Once the first tagged release is published, a one-line installer (Linux and macOS) and a prebuilt wheel are also available. For those, container runs, Windows, and the optional web console and OpenGrep setup, see the **[installation guide](docs/get-started/install.md)**.

## A first engagement

Start in the default Audit mode and describe the authorized target and objective:

```text
$ uv run kerkov

kerkov › Review this local application for authentication and access-control flaws.
         Do not test anything outside http://127.0.0.1:3000.
```

Useful launch patterns:

```bash
uv run kerkov --mode recon                       # passive discovery and enumeration
uv run kerkov --continue                         # resume the most recent engagement
uv run kerkov --resume <engagement-id>           # resume a specific engagement
uv run kerkov --message "..." --no-welcome       # process messages without the interactive UI
```

> [!CAUTION]
> Non-interactive runs auto-approve tool calls. Use them only in an isolated environment with a tightly scoped prompt and target.

Inside the CLI, `/help` lists every command. For a ten-minute walkthrough against your own machine, see **[Your first engagement](docs/get-started/first-engagement.md)**.

## Operational modes

| Mode | Best for | Behavior |
| --- | --- | --- |
| **Recon** | Discovery and attack-surface mapping | Passive-first enumeration and analysis; no exploitation |
| **Audit** | Professional penetration tests | Methodical testing, controlled validation, evidence, and remediation guidance |
| **Offensive** | Red-team exercises | Objective-driven exploitation, chaining, and deeper attack-path analysis |
| **CTF** | Competitions and training labs | Fast hypothesis testing, scripting, and flag extraction |

`audit` is the default. Launch directly into Recon, Audit, or Offensive with `--mode`; switch to any mode, including CTF, with `/mode` during a session. See the [modes and autonomy guide](docs/guides/modes-and-autonomy.md).

## Model providers

| Provider | Authentication |
| --- | --- |
| **Anthropic** | API key |
| **Google Gemini** | API key |
| **NVIDIA NIM** | API key |
| **OpenRouter** | API key |
| **DeepSeek** | API key |
| **Ollama** | Local server URL; no key required by default |
| **Codex** | Browser-based sign-in |

Use `/provider` and `/model` to switch at runtime. API keys are entered in a separate masked prompt, so they never become part of the conversation history or normal logs. Environment variables remain available for CI and other non-interactive environments. See the [providers guide](docs/guides/providers.md).

## Human control by design

Approval gating and action narration are independent: keep terse output with strict confirmations, or detailed narration with a more permissive policy.

| Approval mode | Behavior |
| --- | --- |
| **Strict** | Deny rules apply; every other tool call asks first |
| **Normal** | Allow and deny rules apply; unmatched calls ask first |
| **Permissive** | Deny rules apply; every other call is approved |
| **YOLO** | Bypasses all prompts and rules; requires `ALLOW_AUTONOMOUS=true` |

Normal mode is the default. Project, user, and engagement-specific YAML rules make safe repetitive work frictionless without globally relaxing the policy. See the [approvals guide](docs/guides/approvals.md) for rule syntax and precedence.

## Toolkit and architecture

Kerkov exposes a focused tool suite—process execution with real PTY sessions, file reads and writes, HTTP with authenticated profiles, Playwright browser automation, CVE intelligence (NVD, CISA KEV, EPSS), Syft SBOM generation, Grype vulnerability scanning, OpenGrep SAST, managed OWASP ZAP, web search, skills, the findings ledger, and recall—and applies the current [approval policy](docs/guides/approvals.md) before anything runs. Every tool is documented in the **[tool reference](docs/reference/tools/index.md)**.

The CLI and experimental web console share one conversation engine: it gives each provider a normalized tool interface, routes calls through the approval layer, writes engagement history locally, and coordinates delegated agents with isolated conversations and restricted tool sets. For how it all fits together, see the **[architecture overview](docs/concepts/architecture.md)**.

## Experimental web console

A local web console serves the shared runtime over Django and Channels—no frontend framework or Node build required:

```bash
uv sync --locked --extra web
uv run --no-sync kerkov-web runserver   # then open http://127.0.0.1:8764/
```

The **Network map** tab visualizes saved resource relationships with search,
filters, zoom, and connection details. See the
[web console guide](docs/guides/web-console.md#network-map) for controls.

> [!WARNING]
> The web console is an alpha preview with no user authentication. It intentionally accepts loopback hosts only; do not publish it to a LAN, tunnel, or reverse proxy.

See the [web console guide](docs/guides/web-console.md).

## Documentation

The full documentation lives under [`docs/`](docs/index.md):

- **[Get started](docs/get-started/install.md)** — install, configure, and run your first engagement.
- **[Guides](docs/guides/modes-and-autonomy.md)** — modes and autonomy, approvals, providers, memory, agents, skills, engagements, non-interactive runs, and the web console.
- **[Tool reference](docs/reference/tools/index.md)** — every tool, plus the [CLI](docs/reference/cli.md), [slash commands](docs/reference/slash-commands.md), and [configuration](docs/reference/configuration.md).
- **[Concepts](docs/concepts/architecture.md)** — architecture, the conversation loop, the knowledge base and findings ledger, context and compaction, and the agent subsystem.

## Development

```bash
uv sync --locked --extra dev --extra test --extra web
uv run python -m flake8 --select=E301,E302,E303,E305,E306 src tests
uv run python -m pytest tests
```

When dependencies change, update `pyproject.toml`, run `uv lock`, and re-run the locked sync above. Issues and pull requests are welcome; please include a clear scope, reproduction steps for bugs, and tests for behavior changes where practical.

## Project status

Kerkov is alpha software. Interfaces, configuration, and stored data formats may change before the stable release. The CLI is the primary experience; the web console is experimental.

## Responsible use

Kerkov is built for legitimate security assessment, research, and education. It does not grant permission to test a target. You are responsible for obtaining authorization, defining scope, protecting collected data, and following applicable laws and engagement rules.

Only test systems you own or are explicitly authorized to assess.
