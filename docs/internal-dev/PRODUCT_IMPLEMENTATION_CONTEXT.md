# Product Implementation Context

Last reviewed: 2026-08-11

This document records the current product direction for future implementation
work. It is based on a code-level comparison between Kerkov and the local
`pentest-copilot` repository. Treat it as implementation context, not as proof
that every competing feature should be copied.

## Decision Summary

Kerkov should not pursue tool-count parity. Pentest Copilot registers many
separate wrappers for capabilities Kerkov already covers with `exec`, `process`,
`browser`, `web_search`, and `delegate`. Its meaningful advantages are persistent
structured engagement state and integrations with operator tools such as Burp,
Caido, and Mythic.

The next user-visible capability should be a typed engagement ledger. Before
adding active offensive integrations, Kerkov must close the safety-policy gaps
listed below.

## Capability Comparison

Pentest Copilot has first-class capabilities that Kerkov does not currently
have:

- Burp Suite: proxy history, Repeater, Intruder, and Collaborator.
- Caido: HTTP history, Replay, Automate, interception control, and OAST.
- Mythic C2: callbacks, tasks and results, pivots, payloads, listeners, loot,
  and GraphQL.
- Structured engagement state for hosts, services, credentials,
  vulnerabilities, shells, evidence, attempted approaches, and next steps.
- Multi-model CTF swarms with finding broadcasts, status, trace, steering, and
  wait controls.
- A managed security-tool installer/catalog.
- Native image observation and a higher-level semantic browser action.

The following apparent gaps are mostly differences in tool granularity:

- `run_bash` and `run_python_script` are covered by `exec`.
- Spawn/read/write/list/close shell tools are covered by `exec` plus `process`.
- `google_search` is covered by `web_search`.
- `spawn_subagent` is covered by typed, batch-capable `delegate` profiles.
- `ask_user` is normal conversational behavior, not a necessary model tool.
- Much of the semantic browser capability overlaps Kerkov's deterministic
  Playwright browser tool.

Kerkov has several stronger first-class primitives of its own:

- `cve_lookup` merges NVD, CISA KEV, and EPSS with provenance and caching.
- `http` supports generic requests and named credential profiles.
- `container_lifecycle` manages Compose targets and readiness.
- Skills package reusable procedures and declare tool, binary, and credential
  requirements.
- The browser exposes deterministic snapshots, network data, sources,
  screenshots, and DOM evaluation.
- Delegation uses typed agent profiles, explicit tool ceilings, depth limits,
  and batch limits.

## P0: Safety Invariants

Complete these before adding Burp, Caido, Mythic, or other active integrations.

1. Enforce the secure-default `EXECUTION_ENABLED` capability in the active
   `ExecTool` for one-shot, session, and daemon execution. Reject before creating
   or spawning a process and test all modes.
2. Do not let non-interactive execution bypass deny rules. Evaluate hard denies
   before any auto-approval path, and require an explicit opt-in for unattended
   active execution.
3. Treat a delegated agent's tool whitelist as a capability ceiling, not a
   replacement for runtime policy. Delegated calls must still respect hard deny
   rules, scope, and catastrophic-action checks.
4. Introduce an engagement scope owned by the user. The model may query scope
   but must not widen it. Network-facing tools and adapters must validate their
   targets against this scope.
5. Preserve an append-only audit trail separately from compacted conversation
   history.

## P1: Typed Engagement Ledger

Kerkov's current `EngagementStore` persists conversation messages. The new
ledger must be a separate domain model that survives context compaction and can
be queried without replaying the transcript.

Recommended entities:

- Engagement and immutable user-defined scope.
- Asset: IP address, domain, hostname, and relationships.
- Service and endpoint: port, protocol, product, version, URL, and notes.
- Observation: source tool, command or request, timestamp, confidence, and
  evidence links.
- Finding: title, target, severity, confidence, status, CVE/CWE/CVSS references,
  impact, reproduction steps, remediation, and evidence.
- Evidence artifact: file path or content reference, hash, media type, source,
  and timestamp.
- Attempted approach and next step.
- Credential reference: a redacted pointer into the credential store, never a
  plaintext secret copied into the ledger or prompt.

Prefer a small set of narrow model tools over a single loosely typed mega-tool:

- `record_asset`
- `record_observation`
- `record_finding`
- `attach_evidence`
- `query_engagement`

The service layer should normalize and deduplicate records using stable
fingerprints. Store full state locally, preferably in SQLite with an append-only
event or audit table and materialized current records. Inject only a compact,
relevant, redacted projection into the model context; retrieve details on demand.

Minimum acceptance criteria:

- Records persist across resume and context compaction.
- Repeated discoveries update or merge instead of multiplying duplicates.
- Every finding can retain evidence and provenance.
- Secret values never appear in automatically generated prompt state.
- The agent cannot create or widen engagement scope.
- CLI, web runtime, and delegated agents share the same service semantics.
- Unit and integration tests cover validation, deduplication, persistence,
  redaction, scope enforcement, and concurrent writers.

## P2: Intercepting-Proxy Adapter

After the ledger and safety invariants, add a vendor-neutral proxy interface and
implement Burp first. Avoid permanently exposing separate Burp and Caido copies
of every operation to the model.

Initial model-facing operations:

- `proxy_history`: search summaries and retrieve one full request/response.
- `proxy_replay`: send a request to a manual replay surface.
- `proxy_oast`: create a payload and poll for interactions.

Provider adapters can map those operations to Burp or Caido. Start with
read-only history, then replay, then OAST. Active actions require approval,
scope validation, provenance capture, and ledger evidence links.

## P3: Work-Host Abstraction

Make `exec`, `process`, file access, and artifacts operate through a selected
local, SSH, or container-backed work host. This should be an execution-provider
abstraction beneath the existing tools, not duplicated `ssh_exec`,
`ssh_process`, and `ssh_read` model tools.

## Explicit Non-Priorities

Do not prioritize the following until the earlier layers are proven:

- Mythic C2 integration.
- Multi-model racer swarms.
- A large hand-maintained catalog of installable binaries.
- Additional shell wrappers around `exec` and `process`.
- Vendor-specific copies of otherwise identical proxy operations.
- A single engagement-state tool with many permissive action/data variants.
- Injecting complete engagement state or plaintext credentials into every
  model prompt.
