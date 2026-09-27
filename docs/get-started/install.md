# Install

Kerkov needs [uv](https://docs.astral.sh/uv/) and Python 3.10+. A few features
have extra prerequisites, all optional:

- **Docker** with a running daemon — only for the managed OWASP ZAP and managed
  SearXNG integrations, and for running Kerkov itself in containers.
- **Syft / Grype** on `PATH` — for `sbom_generate` and `vulnerability_scan`.
- **Chromium** — for the `browser` tool (`uv run python -m playwright install chromium`).

You do not need a `.env` file for ordinary interactive use; on first run Kerkov
prompts for a provider credential and stores it in your OS keyring.

## From source

The reliable path today, and what contributors use:

```bash
git clone https://github.com/Kerkov-Security/kerkov.git
cd kerkov
uv sync --locked
uv run kerkov
```

On first launch, choose a provider and follow the masked credential prompt.
Kerkov validates the key before saving it to your OS keyring; on headless
systems without a keyring it can fall back to a user-only file under
`~/.kerkov/`. See [Configuration](configuration.md) for how credentials and
settings are resolved.

## Install directly from GitHub

To install the current `main` branch as an isolated uv tool without a working
tree:

```bash
uv tool install --python 3.13 git+https://github.com/Kerkov-Security/kerkov.git
kerkov
```

## One-line installer (Linux and macOS)

!!! note "Available at the first tagged release"
    The one-line installer pulls a versioned wheel from GitHub Releases, so it
    works once the first release is published. Until then, use **From source**
    or **Install directly from GitHub** above.

```bash
curl -fsSL https://raw.githubusercontent.com/Kerkov-Security/kerkov/main/install.sh | bash
kerkov
```

The installer supports x86_64 and arm64. It installs uv if needed, provisions
Python when missing, and installs the wheel into an isolated uv tool
environment with commands in `~/.kerkov/bin`. It sets up `PATH` for bash, zsh,
fish, and sh login profiles; open a new terminal or run the printed `PATH`
command before launching. Re-run it to upgrade; uninstall with
`uv tool uninstall kerkov` (your `~/.kerkov/` data is left intact).

Common variations:

```bash
# Pin a release
curl -fsSL .../install.sh | VERSION=0.1.0 bash
# Include the web console and download Chromium
curl -fsSL .../install.sh | bash -s -- --web --chromium
# Unattended: no Chromium, no shell-profile edits
curl -fsSL .../install.sh | bash -s -- --no-chromium --no-modify-path
```

It also seeds missing skills, agent profiles, `config/http_profiles.yaml`, and
an empty `approvals.yaml` template under `~/.kerkov/`, never overwriting files
you already have.

## Manual install and Windows

Download the universal wheel from
[GitHub Releases](https://github.com/Kerkov-Security/kerkov/releases) and, with
a compatible Python (3.10–3.14) in an activated virtual environment:

```bash
python -m pip install /path/to/kerkov-<version>-py3-none-any.whl
kerkov --version
kerkov
```

Windows users run the same commands with their downloaded wheel path. PyPI
publication is not configured.

## Docker Compose

Build the local image and start the hardened Kerkov + ZAP stack:

```bash
docker buildx build --load --file image/Dockerfile --tag kerkov:local .
export ZAP_API_KEY="$(openssl rand -hex 32)"
KERKOV_IMAGE=kerkov:local docker compose run --rm --service-ports kerkov
docker compose down
```

Kerkov runs as a non-root user on a read-only root filesystem. Runtime state
lives in the `kerkov-state` volume; `./workspace` is mounted at `/workspace`.
The ZAP API stays internal to the Compose network.

## The web console (optional)

The proof-of-concept web console needs the `web` extra:

```bash
uv sync --locked --extra web
uv run --no-sync kerkov-web runserver   # http://127.0.0.1:8764
```

See the [web console guide](../guides/web-console.md) for what it does and does
not support.

## OpenGrep (optional, for `sast_scan`)

`sast_scan` uses the local OpenGrep engine — no account or API key. Kerkov uses
an `opengrep` already on `PATH`, or you can install the official binary
explicitly:

```bash
kerkov setup opengrep
```

This verifies the download against the SHA-256 digest in GitHub's release
metadata and installs under `~/.kerkov/tools/opengrep/` without `sudo`. Scans
never install software themselves: if OpenGrep is missing, `sast_scan` reports
this command and stops before producing artifacts.

## Next

- [Your first engagement](first-engagement.md) — a ten-minute walkthrough.
- [Configuration](configuration.md) — credentials, `.env`, and precedence.
