# Vulhub E2E Testing

This repository includes a gated end-to-end Vulhub workflow for self-hosted runners:

- Workflow: `.github/workflows/vulhub-e2e.yml`
- Harness: `scripts/ci/run_vulhub_e2e.py`
- Inventory: `config/ci/vulhub_inventory.json`
- Allowlist: `config/ci/vulhub_e2e_cases.json`

## Purpose

This job is meant to validate the full operator loop:

1. The bot selects and applies the Vulhub lab skill
2. The lab boots locally with Docker Compose
3. The bot applies the exploit validation skill
4. CI verifies an external proof artifact created through the vulnerability

It is intentionally not a normal per-PR check. Real Vulhub labs are slower and less deterministic than unit tests.

## Runner Requirements

The current workflow targets a self-hosted macOS runner with:

- Docker Desktop with `docker compose`
- A persistent virtualenv at `~/.venvs/kerkov-bot`
- Codex auth state under `~/.config/kerkov-bot`

## Inventory Versus Allowlist

The repository now keeps two separate Vulhub data sets:

- `config/ci/vulhub_inventory.json` is the generated inventory of all labs discovered in the local `vulhub/` checkout.
- `config/ci/vulhub_e2e_cases.json` is the curated CI allowlist of cases that are safe and stable enough to run end-to-end.

Regenerate the broad inventory with:

```bash
python scripts/ci/generate_vulhub_inventory.py
```

Do not hand-edit the generated inventory unless you are debugging the generator itself.

## Allowlisted Cases

Only cases listed in `config/ci/vulhub_e2e_cases.json` may run in CI.

Each case should define:

- CVE
- Vulhub lab path
- service name for `docker compose exec`
- target or healthcheck URL
- a harmless proof artifact path
- a short exploit goal prompt

Prefer cases that:

- start quickly
- expose a simple HTTP service
- support a non-destructive proof
- can be verified externally in a stable way

## Current Starter Case

The default case is `phpunit-cve-2017-9841`.

The harness instructs the bot to create `/tmp/kerkov-ci-proof` through the vulnerability, then verifies:

- the target is reachable
- the lab is still running
- the proof file exists inside the container

## Extending The Matrix

When adding a new case:

1. Add it to `config/ci/vulhub_e2e_cases.json`
2. Confirm the lab already exists in `config/ci/vulhub_inventory.json`, or regenerate the inventory first
3. Make sure the proof is safe and easy to verify
4. Keep the bot prompt explicit about the expected artifact
5. Test it manually on the runner before scheduling it

Avoid heavy or flaky labs until the harness has proven reliable over multiple runs.
