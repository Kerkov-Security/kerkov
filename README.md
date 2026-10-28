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
  <a href="https://kerkov-security.github.io/kerkov/"><img src="https://img.shields.io/badge/docs-kerkov--security.github.io-0A7E8C" alt="Documentation"></a>
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

On Intel Macs, install the [native build prerequisites](https://kerkov-security.github.io/kerkov/get-started/install/#intel-macos-build-prerequisites) before syncing dependencies.

Run from source:

```bash
git clone https://github.com/Kerkov-Security/kerkov.git
cd kerkov
uv sync --locked
uv run kerkov
```

On first use, choose a provider and follow the masked credential prompt. Kerkov validates the credential before saving it to your operating-system keyring (or, on headless systems, a user-only file under `~/.kerkov/`). No `.env` file is required for ordinary interactive use.

Once the first tagged release is published, one-line installers (Linux, macOS and Windows) and a prebuilt wheel are also available. For those, container runs, and the optional web console and OpenGrep setup, see the **[installation guide](https://kerkov-security.github.io/kerkov/get-started/install/)**.

The rest of this README writes commands as `kerkov` and `kerkov-web`, the names an installed build puts on your `PATH`. From a source checkout, prefix them with `uv run`.

## A first engagement

Start in the default Audit mode and describe the authorized target and objective:

```text
$ kerkov

kerkov › Review this local application for authentication and access-control flaws.
         Do not test anything outside http://127.0.0.1:3000.
```

Useful launch patterns:

```bash
kerkov --mode recon                       # passive discovery and enumeration
kerkov --continue                         # resume the most recent engagement
kerkov --resume <engagement-id>           # resume a specific engagement
kerkov --message "..." --no-welcome       # process messages without the interactive UI
```

> [!CAUTION]
> Non-interactive runs auto-approve tool calls. Use them only in an isolated environment with a tightly scoped prompt and target.

Inside the CLI, `/help` lists every command. For a ten-minute walkthrough against your own machine, see **[Your first engagement](https://kerkov-security.github.io/kerkov/get-started/first-engagement/)**.

## Operational modes

| Mode | Best for | Behavior |
| --- | --- | --- |
| **Recon** | Discovery and attack-surface mapping | Passive-first enumeration and analysis; no exploitation |
| **Audit** | Professional penetration tests | Methodical testing, controlled validation, evidence, and remediation guidance |
| **Offensive** | Red-team exercises | Objective-driven exploitation, chaining, and deeper attack-path analysis |
| **CTF** | Competitions and training labs | Fast hypothesis testing, scripting, and flag extraction |

`audit` is the default. Launch directly into Recon, Audit, or Offensive with `--mode`; switch to any mode, including CTF, with `/mode` during a session. See the [modes and autonomy guide](https://kerkov-security.github.io/kerkov/guides/modes-and-autonomy/).

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

Use `/provider` and `/model` to switch at runtime. API keys are entered in a separate masked prompt, so they never become part of the conversation history or normal logs. Environment variables remain available for CI and other non-interactive environments. See the [providers guide](https://kerkov-security.github.io/kerkov/guides/providers/).

Automatic model routing for delegated agents is opt-in. Set
`AGENT_ROUTING_SMALL_MODEL`, `AGENT_ROUTING_MEDIUM_MODEL`, and
`AGENT_ROUTING_LARGE_MODEL` to distinct model IDs supported by the active
provider, then run `/model auto`. The main conversation keeps its current model.
Each new delegate, including nested delegates, receives a fixed model for its
run. SMALL handles eligible literal extraction and formatting; MEDIUM handles
eligible routine analysis of supplied facts; LARGE handles general, complex,
security-sensitive, or uncertain work.

SMALL and MEDIUM require the built-in analysis profile, tools restricted to
`read` and `recall`, bounded instructions, and nonempty success criteria. The
task must explicitly request `task_kind="extract"` or `"format"` for SMALL, or
`task_kind="analyze"` for MEDIUM. Additional rules screen for unsuitable work;
these are conservative heuristics, not a guarantee of task difficulty. SMALL
and MEDIUM agents cannot delegate further; LARGE agents retain nested
delegation. Agent details and reports include the selected model and routing
reason. See the [agent guide](https://kerkov-security.github.io/kerkov/concepts/agent-subsystem/#choosing-a-model-for-delegated-tasks)
for eligibility details.

`AGENT_ROUTING_ENABLED=false` is the default; set it to `true` to enable routing
at startup in the CLI or web console. Invalid or duplicate tier model settings
log a warning and fall back to manual delegation. Choosing a concrete model in the CLI
returns future delegates to manual selection, including choosing the current
model in the picker. Changing provider also disables routing. Running agents
retain their selected model. `/model openrouter/auto` selects OpenRouter's model
and is separate from `/model auto`.

Use `/usage` to inspect saved token usage for the active engagement, including
the main conversation and its delegated agents, or `/usage <engagement-id>` for
a saved run. The summary groups usage by provider, model and routing tier and
shows each agent's outcome. Missing usage remains unknown; a completed agent
report does not establish that its answer is correct.

Cost estimates require an explicit price file via
`MODEL_PRICING_FILE=~/.kerkov/model-prices.json`. Prices are USD per million
tokens, keyed by `provider/model`. For example, this uses **hypothetical rates
and a placeholder model**, not current provider prices:

```json
{
  "version": 1,
  "models": {
    "anthropic/example-model": {
      "input": 1.0,
      "output": 2.0,
      "cache_read": 0.1,
      "cache_write": 1.25
    }
  }
}
```

Use canonical provider names such as `anthropic`, `gemini`, or `openrouter`
and the model from the usage summary, with rates appropriate to your account.
Legacy class-name keys (for example `AnthropicProvider/model`) and saved usage
remain supported; conflicting rates for aliases of one provider/model are rejected. `input` and `output` are required; cache rates are needed when
those tokens are reported. Missing prices produce unknown costs. These are
estimates based on reported usage and the supplied rates, not provider bills.
Provider token totals are preserved. If a reported total differs from its
input/output breakdown, the cost stays unknown rather than guessing how to
price unclassified tokens.
See [routing evaluation](https://kerkov-security.github.io/kerkov/routing-evaluation/) for repeatable comparisons
of automatic routing against the large-model baseline.

## Human control by design

Approval gating and action narration are independent: keep terse output with strict confirmations, or detailed narration with a more permissive policy.

| Approval mode | Behavior |
| --- | --- |
| **Strict** | Deny rules apply; every other tool call asks first |
| **Normal** | Allow and deny rules apply; unmatched calls ask first |
| **Permissive** | Deny rules apply; every other call is approved |
| **YOLO** | Bypasses all prompts and rules; requires `ALLOW_AUTONOMOUS=true` |

Normal mode is the default. Project, user, and engagement-specific YAML rules make safe repetitive work frictionless without globally relaxing the policy. See the [approvals guide](https://kerkov-security.github.io/kerkov/guides/approvals/) for rule syntax and precedence.

## Toolkit and architecture

Kerkov exposes a focused tool suite—process execution with real PTY sessions, file reads and writes, HTTP with authenticated profiles, Playwright browser automation, CVE intelligence (NVD, CISA KEV, EPSS), Syft SBOM generation, Grype vulnerability scanning, OpenGrep SAST, managed OWASP ZAP, web search, skills, the findings ledger, and recall—and applies the current [approval policy](https://kerkov-security.github.io/kerkov/guides/approvals/) before anything runs. Every tool is documented in the **[tool reference](https://kerkov-security.github.io/kerkov/reference/tools/)**.

The CLI and the web console share one conversation engine: it gives each provider a normalized tool interface, routes calls through the approval layer, writes engagement history locally, and coordinates delegated agents with isolated conversations and restricted tool sets. For how it all fits together, see the **[architecture overview](https://kerkov-security.github.io/kerkov/concepts/architecture/)**.

## Web console

A local web console serves the shared runtime over Django and Channels—no frontend framework or Node build required.

The console needs the `web` extra — pass `--web` to the installer, or run
`uv sync --locked --extra web` in a checkout:

```bash
kerkov --interface web   # then open http://127.0.0.1:8764/
kerkov-web runserver     # or start it directly
```

On first launch `kerkov` asks whether to start the terminal CLI or the web
console and can remember your choice; `--interface cli|web` overrides it.

The **Network map** tab visualizes saved resource relationships with search,
filters, zoom, and connection details. See the
[web console guide](https://kerkov-security.github.io/kerkov/guides/web-console/#network-map) for controls.

> [!WARNING]
> The web console has no user authentication. It intentionally accepts loopback hosts only; do not publish it to a LAN, tunnel, or reverse proxy.

See the [web console guide](https://kerkov-security.github.io/kerkov/guides/web-console/).

## Documentation

The full documentation is published at
**[kerkov-security.github.io/kerkov](https://kerkov-security.github.io/kerkov/)**,
and its source lives under [`docs/`](docs/):

- **[Get started](https://kerkov-security.github.io/kerkov/get-started/install/)** — install, configure, and run your first engagement.
- **[Guides](https://kerkov-security.github.io/kerkov/guides/modes-and-autonomy/)** — modes and autonomy, approvals, providers, memory, agents, skills, engagements, non-interactive runs, and the web console.
- **[Tool reference](https://kerkov-security.github.io/kerkov/reference/tools/)** — every tool, plus the [CLI](https://kerkov-security.github.io/kerkov/reference/cli/), [slash commands](https://kerkov-security.github.io/kerkov/reference/slash-commands/), and [configuration](https://kerkov-security.github.io/kerkov/reference/configuration/).
- **[Concepts](https://kerkov-security.github.io/kerkov/concepts/architecture/)** — architecture, the conversation loop, the knowledge base and findings ledger, context and compaction, and the agent subsystem.

## Development

```bash
uv sync --locked --extra dev --extra test --extra web
uv run python -m flake8 --select=E301,E302,E303,E305,E306 src tests
uv run python -m pytest tests
```

When dependencies change, update `pyproject.toml`, run `uv lock`, and re-run the locked sync above. Issues and pull requests are welcome; please include a clear scope, reproduction steps for bugs, and tests for behavior changes where practical.

## Project status

Kerkov is alpha software. Interfaces, configuration, and stored data formats may change before the stable release. The CLI and the web console are supported interfaces over the same runtime.

## Responsible use

Kerkov is built for legitimate security assessment, research, and education. It does not grant permission to test a target. You are responsible for obtaining authorization, defining scope, protecting collected data, and following applicable laws and engagement rules.

Only test systems you own or are explicitly authorized to assess.
