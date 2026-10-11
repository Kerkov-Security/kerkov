# `container_lifecycle`

Brings a Docker Compose project up, waits for it to actually be ready, reports its
state, and takes it back down.

Prefer it over running `docker compose` through [`exec`](exec-process.md). The
difference is context: `docker compose up -d` on a multi-service project emits
hundreds of lines of pull progress and layer hashes, and then you still have to
poll for readiness in a loop the model pays for every iteration. This tool keeps
the full output as a workspace artifact and polls locally, returning one compact
snapshot.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `action` | `start_wait` \| `status` \| `down` | **required** | What to do. |
| `compose_dir` | string | **required** | Directory containing the Compose file. Relative paths resolve from the CLI's launch directory; absolute paths and `~/path` are accepted. Projects need not be inside the engagement workspace. |
| `project_name` | string | — | Compose project name. Must start with a lowercase letter or digit and contain only lowercase letters, digits, underscores, or hyphens. |
| `readiness` | `auto` \| `running` \| `healthy` \| `http` | `auto` | `start_wait`. See below. |
| `probe_url` | string | — | HTTP URL to probe. Required for `readiness=http`; also honoured by `auto` when supplied. Must be `http://` or `https://`. |
| `timeout_seconds` | integer | `180` | `start_wait`. Maximum start-and-readiness wait. |
| `poll_interval_seconds` | number | `2.0` | `start_wait`. Seconds between local readiness checks. |
| `remove_volumes` | boolean | `false` | `down` only. Also remove named and anonymous volumes. |

## Readiness

"Started" and "ready" are not the same thing, and a Vulhub lab that answers HTTP
three seconds after its container reports `running` is the normal case, not the
exception.

| Mode | Ready when |
|---|---|
| `auto` | Health checks pass where the project defines them, otherwise containers are `running`. If `probe_url` is supplied, it must also respond. |
| `running` | Containers report `running`. |
| `healthy` | Compose health checks report healthy. |
| `http` | `probe_url` responds. Requires `probe_url`. |

Any HTTP response below 500 proves readiness — a 401 or a 404 means something is
listening and serving, which is the question being asked.

## Actions

| Action | What it does |
|---|---|
| `start_wait` | `docker compose up -d`, then poll locally until ready or the timeout expires. |
| `status` | A compact snapshot of the project's current state. |
| `down` | Stop the project, optionally removing volumes. |

## Approval

An approval saved from a `container_lifecycle` prompt widens to **the action** —
bringing a stack up is not the same grant as querying one. No action has a
packaged allow rule, so all three prompt in `strict` and `normal`.

Gated by [`EXECUTION_ENABLED`](../configuration.md#tool-execution).

## Returns

JSON as output, carrying `status`, `ready`, `readiness_basis` (what the decision
was based on — the single most useful field when a start fails), `compose_dir`,
`services`, `service_count`, `services_truncated`, `containers`, `probe_url`,
`elapsed_seconds`, `log_artifact`, and `cleanup` — the exact `docker compose down`
command for this project, so teardown needs no reconstruction.

Metadata is deliberately just `status`, `ready`, and `log_artifact`: the structured
payload is already in the output, and duplicating it into metadata would double its
footprint against the [clipping
threshold](../configuration.md#knowledge-base-and-findings-ledger).

Complete `docker compose` output goes to `artifacts/container-logs/` in the
engagement workspace. A start that does not become ready returns
`success: false` with `status: "not_ready"` and a `diagnostic_tail` — the end of
that log — so the cause is visible without opening the artifact.

## Examples

Start a Vulhub lab and wait until it serves HTTP:

```json
{
  "action": "start_wait",
  "compose_dir": "/home/kali/vulhub/log4j/CVE-2021-44228",
  "probe_url": "http://127.0.0.1:8983/solr/"
}
```

Check on it later:

```json
{ "action": "status", "compose_dir": "/home/kali/vulhub/log4j/CVE-2021-44228" }
```

Tear it down completely:

```json
{
  "action": "down",
  "compose_dir": "/home/kali/vulhub/log4j/CVE-2021-44228",
  "remove_volumes": true
}
```

Wait on health checks only, with a longer budget for a slow image pull. Here
`./lab` is relative to the directory where you launched Kerkov:

```json
{
  "action": "start_wait",
  "compose_dir": "./lab",
  "readiness": "healthy",
  "timeout_seconds": 600
}
```

## Related

- [`exec`](exec-process.md) — for Compose operations this tool does not cover
  (`logs`, `exec`, `build`). You lose the artifact handling and bounded output.
- The `container-operator` [agent profile](../agent-profiles.md) exists for exactly
  this work: delegate lab startup to it and the pull output never touches the main
  conversation at all.
- [`vulnerability_scan`](sbom-vulnerability-scan.md) — for scanning the images a
  Compose project runs.

## Automatic engagement inventory

When the engagement knowledge base is enabled, `start_wait` and `status` save
last-known observations in the `containers` ledger section and register published
IP endpoints as targets. An explicitly supplied HTTP probe also registers its URL
when it matches a published TCP endpoint. Ports alone never imply HTTP or create
observed access routes. Wildcard binds use loopback as the local Docker access
address; this feature describes the local Docker workflow, not remote daemon
network discovery.

Container identity includes the Compose directory, explicit project name, and
container name. Repeated observations update the same record. Existing target
names, notes, tags, and environments are preserved. A successful `down` marks the
project's saved containers stopped; a complete status snapshot marks missing
containers removed. Targets and historical port mappings remain saved. These are
last-known observations, not a live Docker subscription; use `status` to refresh
changes made outside Kerkov.

The compact result includes `inventory.status` (`registered`, `unavailable`, or
`failed`). A disabled/unavailable knowledge base or registration error does not
turn a successful Docker operation into a failure. Retry `status` to synchronize.
Agents use the same root engagement ledger as the main conversation.
