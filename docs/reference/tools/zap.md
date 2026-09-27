# `zap`

Drives OWASP ZAP for web-application scanning: crawl a target, passively analyse
what the crawl saw, then optionally run ZAP's intrusive active scan and collect
the alerts.

!!! danger "Active scanning modifies target state"
    `spider_start` and `active_scan_start` send real traffic, and an active scan
    submits forms, fuzzes parameters, and can change or destroy data. Run them
    only against targets you are explicitly authorized to test.

## Parameters

| Parameter | Type | Default | Applies to |
|---|---|---|---|
| `action` | see below | **required** | — |
| `target` | string | — | Required for `spider_start` and `active_scan_start`; an optional base-URL filter for `alerts`. Must be an absolute `http(s)` URL. |
| `scan_id` | string | — | Required for the `*_status` actions and `scan_stop`. Returned by a start action. |
| `scan_kind` | `spider` \| `active` | — | Required by `scan_stop`, to say which kind of scan the id belongs to. |
| `recurse` | boolean | `true` | Crawl or scan child resources. |
| `subtree_only` | boolean | `true` | `spider_start`. Keep crawling below the target path. |
| `max_children` | integer | — | `spider_start`. Cap children per node. |
| `in_scope_only` | boolean | `false` | `active_scan_start`. Restrict to URLs already in ZAP's configured scope. Leave false unless you have configured a ZAP context. |
| `scan_policy` | string | — | `active_scan_start`. A named ZAP active-scan policy. |
| `start` | integer | `0` | `alerts`. Zero-based result offset. |
| `count` | integer | `25` | `alerts`. Maximum findings to return. |

## Actions

| Action | What it does | Default approval |
|---|---|---|
| `status` | Validate the API and return the ZAP version. | Allowed |
| `spider_start` | Crawl the target, feeding responses through passive scanning. | Prompts |
| `spider_status` | Crawl progress, then wait for queued passive scanning. | Allowed |
| `active_scan_start` | Launch intrusive vulnerability tests against discovered resources. | Prompts |
| `active_scan_status` | Active-scan completion percentage. | Allowed |
| `alerts` | Retrieve normalized, bounded findings. | Allowed |
| `scan_stop` | Stop a spider or active scan by id. | Prompts |

The order matters. **Spider first, wait for it to finish, then active-scan**, so
ZAP knows what resources exist before it starts testing them. An active scan
against an uncrawled target tests almost nothing.

`spider_status` reports `passive_scanning` at 99% when crawling has reached 100%
but passive records are still queued, and reports completion only once the queue
drains. Treating 100% crawl as "done" collects alerts before passive analysis has
produced them.

## Approval

An approval saved from a `zap` prompt widens to **the action** — starting a scan
is not the same grant as reading its status. The four read-only actions ship
auto-approved:

```yaml
- allow: "zap({'action': 'status'})"
- allow: "zap({'action': 'spider_status'})"
- allow: "zap({'action': 'active_scan_status'})"
- allow: "zap({'action': 'alerts'})"
```

The three that send traffic — `spider_start`, `active_scan_start`, `scan_stop` —
are deliberately left to prompt.

## The managed runtime

There is no setup command. The first approved `spider_start` or `active_scan_start`
starts ZAP for you: Kerkov generates a random API key, runs the official stable
ZAP Docker image, binds its API port to `127.0.0.1`, waits for readiness, then
saves the key through the credential manager and the service URL to
`~/.kerkov/settings.json`. The next scan reuses that runtime.

Only those two actions auto-start. `status` against a ZAP that is not running
reports that it starts automatically with the first scan, rather than starting one
as a side effect of being asked a question.

How the key is handled:

- Streamed over stdin into a mode-`0600` config file inside the container. It
  never appears in Docker argv, environment metadata, tool parameters, API URLs,
  ordinary logs, or user settings.
- Sent in the `X-ZAP-API-Key` header on every request.

How the container is isolated:

- Only ZAP's authenticated API listener is published. The unauthenticated forward
  proxy stays on container loopback.
- It runs on its own labeled bridge network with inter-container communication
  disabled.
- It carries an `org.kerkov.managed=zap` label, and `/zap stop` refuses to remove
  an unlabeled container that happens to share the name.
- Docker's `--rm` is deliberately not used, so a container that fails to start can
  have its logs captured before cleanup.

Stopping a Kerkov-managed runtime also removes its generated key, saved URL, and
managed marker, so the next scan starts fresh. Credentials for an *external* ZAP
are preserved.

Targets on loopback are rewritten to `host.docker.internal` — port, path and
query preserved — so ZAP does not scan its own container. On native Linux, a
target service must listen on an interface the Docker bridge can reach.

The Docker daemon must already be running. A failed start says which of the three
things went wrong — the CLI is missing, the daemon is unreachable, or the user
lacks daemon access — before any configuration is generated or saved.

`EXECUTION_ENABLED=false` blocks the managed start; an already-running external
ZAP still works.

### Using an external ZAP

Point [`ZAP_API_URL`](../configuration.md#owasp-zap-zap-zap) at it and store its
key with `/auth set zap`. If that endpoint stops answering, Kerkov will only
replace it with a managed container when
[`ZAP_MANAGED=true`](../configuration.md#owasp-zap-zap-zap) and the URL is
loopback `http://`; otherwise the failure is reported rather than worked around,
so a scan never silently retargets from your ZAP to a fresh empty one.

## Returns

Pretty-printed JSON as output — sorted keys, two-space indent — with
`provider: "zap"` and the `action` in metadata. Start actions return a `scan_id`; status actions return progress;
`alerts` returns normalized findings.

Alert fields and the total serialized response are both bounded. When the
response hits the budget, `truncated` is true and `next_start` gives the offset
for the next request — page with it rather than raising `count`.

## Examples

Crawl, then watch it:

```json
{ "action": "spider_start", "target": "http://10.10.10.5:8080/app/" }
```
```json
{ "action": "spider_status", "scan_id": "0" }
```

Active-scan once the spider and its passive queue are done:

```json
{ "action": "active_scan_start", "target": "http://10.10.10.5:8080/app/" }
```
```json
{ "action": "active_scan_status", "scan_id": "1" }
```

Collect findings for one subtree:

```json
{ "action": "alerts", "target": "http://10.10.10.5:8080/app/", "count": 25 }
```

Abandon a scan:

```json
{ "action": "scan_stop", "scan_id": "1", "scan_kind": "active" }
```

## Configuration

See [OWASP ZAP](../configuration.md#owasp-zap-zap-zap) for the full table:
`ZAP_API_URL`, `ZAP_API_TIMEOUT_SECONDS`, `ZAP_API_VERIFY_TLS`,
`ZAP_API_ALLOW_REMOTE`, `ZAP_MANAGED`, `ZAP_STARTUP_TIMEOUT_SECONDS`,
`ZAP_DOCKER_IMAGE`, `ZAP_DOCKER_CONTAINER_NAME`.

The [`/zap`](../slash-commands.md#tools) command shows runtime status and stops a
managed container.

## Related

- [`http`](http.md) — for individual requests. ZAP is for systematic coverage.
- [`browser`](browser.md) — for a JavaScript-heavy application ZAP's spider
  cannot crawl.
- [`ledger`](ledger.md) — record confirmed alerts as findings; the ZAP alert list
  is not in context after compaction, the ledger is.
