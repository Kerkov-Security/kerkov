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
  <a href="#features">Features</a> ·
  <a href="#providers-and-modes">Providers & modes</a> ·
  <a href="#documentation">Documentation</a>
</p>

Kerkov brings security tools, specialist agents, and engagement evidence into a shared
CLI and local web workspace. Investigate targets, validate findings, and automate
reviewed workflows with your choice of model and approval policy.

> [!IMPORTANT]
> Use only on systems you own or have explicit permission to test. Keep testing within the agreed scope. Kerkov is alpha software; interfaces, configuration, and stored data formats may change.

## Features

- **One engagement workspace.** Search and resume saved sessions, manage assets and
  targets, inspect resource profiles, and explore recorded relationships on an
  interactive network map.
- **Tools that do the work.** Run commands and supervised processes, edit files,
  make authenticated HTTP requests, automate browsers, and manage container labs.
- **Browser control from the console.** Preview pages, take control to navigate or
  sign in, then return control to Kerkov. Choose fresh sessions or persistent profiles.
- **Specialist agents.** Delegate work, inspect live output, send instructions,
  stop agents, and review reports. Optional model routing selects from configured
  tiers for delegated tasks.
- **Evidence that stays with the engagement.** Record findings in a shared ledger,
  recall saved tool output, and browse generated scripts and workspace files in
  the console.
- **Security integrations.** Use CVE intelligence, OWASP ZAP, OpenGrep SAST, Syft
  SBOMs, Grype vulnerability scans, and web search.
- **Automation and compliance.** Run reviewed Ansible playbooks and snapshot reusable
  engagement profiles. Assess SSH hosts with containerized Cinc Auditor and import
  Cinc/InSpec JSON reports with control-level evidence.
- **Operator control and usage visibility.** Choose strict, normal, permissive, or
  YOLO approvals independently of output verbosity. Inspect engagement and agent
  token usage with `/usage`; optional local pricing enables cost estimates.

## Quick start

You need [uv](https://docs.astral.sh/uv/), Python 3.11–3.14, and access to a
[supported provider](#providers-and-modes). On Intel Macs, install the
[native build prerequisites](https://kerkov-security.github.io/kerkov/get-started/install/#intel-macos-build-prerequisites)
first.

```bash
git clone https://github.com/Kerkov-Security/kerkov.git
cd kerkov
uv sync --locked
uv run kerkov --interface cli
```

Choose a provider and follow setup. API keys use a masked prompt and are saved
through the OS keyring or a protected local-file fallback; Codex uses browser
sign-in. No `.env` file is required for ordinary interactive use.

Start with a scoped request:

```text
Review this local application for authentication and access-control flaws.
Do not test anything outside http://127.0.0.1:3000.
```

### Launch the web console

From the same checkout:

```bash
uv sync --locked --extra web
uv run kerkov --interface web
```

The console opens at `http://127.0.0.1:8764/`. Use its engagement, inventory,
network map, Browser, Scripts, and agent views alongside the conversation.
Browser automation also needs Chromium:

```bash
uv run python -m playwright install chromium
```

> [!WARNING]
> The console has no user-account authentication and is loopback-only. Do not expose it through a LAN, tunnel, or reverse proxy.

Docker, scanner binaries, and Ansible are optional prerequisites for their
respective tools. See the [installation guide](https://kerkov-security.github.io/kerkov/get-started/install/),
[Ansible setup](https://kerkov-security.github.io/kerkov/reference/tools/ansible/#setup),
and [compliance guide](https://kerkov-security.github.io/kerkov/reference/tools/compliance/).
The compliance starter profile is an integration example, not a CIS/STIG benchmark.

### Continue an engagement

The examples below use installed command names. From a checkout, prefix them with
`uv run`.

```bash
kerkov --continue                # resume the most recent engagement
kerkov --resume <engagement-id>   # resume a specific engagement
kerkov --mode recon              # start with passive-first reconnaissance
```

Use `/help` for commands, `/provider` and `/model` to switch models, and
`/usage` to inspect saved usage. For scripted runs, see
[non-interactive use](https://kerkov-security.github.io/kerkov/guides/non-interactive/).

> [!CAUTION]
> Non-interactive CLI runs auto-approve tool calls. Use an isolated environment and a tightly scoped prompt and target.

## Providers and modes

**Providers:** Anthropic, Google Gemini, NVIDIA NIM, OpenRouter, DeepSeek, Ollama,
and Codex. Ollama supports a local endpoint; Codex uses browser-based sign-in.
See [provider setup](https://kerkov-security.github.io/kerkov/guides/providers/).

| Mode | Focus |
| --- | --- |
| **Recon** | Passive-first discovery and attack-surface mapping; no exploitation |
| **Audit** (default) | Methodical testing, controlled validation, evidence, and remediation guidance |
| **Offensive** | Red-team objectives, exploitation chains, and attack-path analysis |
| **CTF** | Fast hypothesis testing and flag extraction in competitions and labs |

Start with `--mode recon|audit|offensive`; use `/mode` during a session to
switch, including to CTF.

Normal approvals are the default: configured allow/deny rules apply, and unmatched
calls ask first. Strict prompts for every non-denied call; permissive approves
every non-denied call. YOLO bypasses prompts and rules and requires
`ALLOW_AUTONOMOUS=true`. See [approvals](https://kerkov-security.github.io/kerkov/guides/approvals/)
and [modes](https://kerkov-security.github.io/kerkov/guides/modes-and-autonomy/).

## Documentation

- **[First engagement](https://kerkov-security.github.io/kerkov/get-started/first-engagement/)** — a walkthrough against your own machine.
- **[Web console](https://kerkov-security.github.io/kerkov/guides/web-console/)** — engagements, resources, browser controls, scripts, and agents.
- **[Tool reference](https://kerkov-security.github.io/kerkov/reference/tools/)** — tools, prerequisites, and execution boundaries.
- **[Agents and model routing](https://kerkov-security.github.io/kerkov/concepts/agent-subsystem/)** — delegation, routing setup, and usage accounting.
- **[Configuration](https://kerkov-security.github.io/kerkov/reference/configuration/)** — settings, credentials, and optional cost estimates.
- **[Architecture](https://kerkov-security.github.io/kerkov/concepts/architecture/)** — the shared runtime, persistence, and approval layer.

## Contributing

```bash
uv sync --locked --extra dev --extra test --extra web
uv run python -m flake8 --select=E301,E302,E303,E305,E306 src tests
uv run python -m pytest tests
```

Issues and pull requests are welcome. See the
[development guide](https://kerkov-security.github.io/kerkov/contributing/development/)
for setup and contribution details.

Licensed under [Apache 2.0](LICENSE). See [SECURITY.md](SECURITY.md) to report a vulnerability.
