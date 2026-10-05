# Data handling

Kerkov collects sensitive material — tool output, recovered credentials, flags,
screenshots — and most of it lives in cleartext on local disk. This page is
precise about where it goes and what leaves the machine, because an engagement's
data is often as sensitive as the target.

## Where things are stored

All paths are defaults; the environment variables in parentheses override them.

| Data | Location |
|---|---|
| Conversations | `~/.kerkov/engagements/<engagement-id>.jsonl` |
| Knowledge base + findings ledger | `~/.kerkov/engagements/<root-engagement-id>.kb.sqlite` |
| Run workspace (artifacts, reports, screenshots) | `~/.kerkov/workspaces/<root-id>/` (`WORKSPACE_DIR`) |
| Credentials | OS keyring, or `~/.kerkov/credentials.json` mode `0600`, or in-memory (`KERKOV_CREDENTIAL_STORE`, `KERKOV_CREDENTIAL_FILE`) |
| User settings (ZAP URL, search provider) | `~/.kerkov/settings.json` |
| Codex OAuth tokens | `data/providers/codex/auth.json` |
| Audit log | `data/logs/audit.log` (`AUDIT_LOG_PATH`) |
| CVE cache | `~/.kerkov/cache/intel` (`INTEL_CACHE_DIR`) |
| Approval overrides | `~/.kerkov/approvals.yaml` (`USER_APPROVALS_PATH`) |
| OpenGrep binary | `~/.kerkov/tools/opengrep/` |
| Managed SearXNG config | `~/.kerkov/searxng/` |

## It is stored in cleartext

The knowledge base and the findings ledger share one SQLite file, and it is
**plaintext**. Harvested credentials are stored there, and the ledger
re-injects them into the model's context **unmasked** on every turn — a masked
credential is not usable, so masking is not done. The credential *store* for
provider and tool API keys is the exception: it prefers the OS keyring, and the
file backend is written `0600` and refuses to load if its permissions are
broader. The engagement data around it is not encrypted; protect it with
filesystem permissions and disk encryption, and delete engagements you no longer
need.

## The precision trap: "local" is only the storage

It is true that Kerkov *stores* this data locally and adds no telemetry. It is
**not** true that the data stays on your machine. The conversation is sent to
the LLM provider you chose, and the conversation includes:

- the tool output the model reasons over,
- any artifact you pull back with `recall`, and
- the injected findings ledger — **credentials and flags included**.

So against a hosted provider (Anthropic, Gemini, NVIDIA NIM, OpenRouter,
DeepSeek, Codex), every turn transmits the ledger's secrets in cleartext to that
provider, and expanding an artifact that contains harvested credentials
transmits them too. The control is **which provider you point Kerkov at** — a
local Ollama endpoint keeps the conversation on your machine; a hosted API does
not. This is inherent to a model-driven tool, not a Kerkov choice, but it is
easy to forget when a page says "nothing leaves the machine" about storage.

## Other outbound traffic

Beyond the provider conversation, these tools and setup steps contact the
network when you use them:

- **`web_search`** sends your query to Tavily (the commercial API) or to the
  SearXNG instance you configured.
- **`cve_lookup`** calls NVD, CISA-KEV, and EPSS.
- **`read_url` and `http`** contact whatever host you point them at.
- **Docker image pulls** happen the first time Kerkov manages OWASP ZAP or
  SearXNG.
- **`kerkov setup opengrep`** downloads the OpenGrep release from GitHub (and
  verifies it against the SHA-256 digest in GitHub's release metadata).

## No telemetry

Kerkov sends no telemetry, analytics, or crash reports of its own — there is no
such code in the source. It goes one step further for its bundled scanner: a
`sast_scan` run clears `SEMGREP_APP_TOKEN` from the subprocess environment and
sets `SEMGREP_SEND_METRICS=off` and `SEMGREP_ENABLE_VERSION_CHECK=0`, so
OpenGrep does not phone home either. Outbound traffic is limited to the provider
conversation and the explicit tool and setup actions listed above.

## The web console

The optional web console is an alpha preview with no authentication; it binds
loopback only and must not be exposed to a LAN, tunnel, or reverse proxy. Its
isolation model — the access token, allowed hosts, and why it refuses non-local
origins — is documented in the
[Security Boundary section of the web architecture page](../concepts/web-architecture.md#security-boundary).

## Related

- [Responsible use](responsible-use.md) — scope, authorization, and the approval
  control surface.
- [Knowledge base](../concepts/knowledge-base.md) and
  [findings ledger](../concepts/findings-ledger.md) — what is stored, and why
  unmasked.
- [Configuration](../reference/configuration.md) — every path variable above.
