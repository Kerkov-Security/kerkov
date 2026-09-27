# Tools

Tools are what turn a conversation into an engagement: the model asks for one by
name, Kerkov checks the call against your [approval
rules](../../guides/approvals.md), runs it locally, and hands the result back.
Nothing runs that you have not permitted, directly or by rule.

This section documents every tool the model can call. Each page follows one
shape: what the tool is for, its parameters, how it is approved, what it returns,
worked examples, and the configuration and tools related to it.

## The suite

`ToolSuite.get_tools()` (`src/kerkov/tools/tool_suite.py`) always builds twelve
tools. Two more are conditional, and the CLI adds five on top once the session
exists.

| Tool | Page | Registered |
|---|---|---|
| `read` | [Read and write](read-write.md) | Always |
| `write` | [Read and write](read-write.md) | Always |
| `exec` | [Exec and process](exec-process.md) | Always |
| `process` | [Exec and process](exec-process.md) | Always |
| `http` | [HTTP](http.md) | Always |
| `read_url` | [Web search and page reads](web-search.md) | Always |
| `cve_lookup` | [CVE lookup](cve-lookup.md) | Always |
| `container_lifecycle` | [Container lifecycle](container.md) | Always |
| `sbom_generate` | [SBOM and vulnerability scan](sbom-vulnerability-scan.md) | Always |
| `vulnerability_scan` | [SBOM and vulnerability scan](sbom-vulnerability-scan.md) | Always |
| `sast_scan` | [SAST](sast.md) | Always |
| `zap` | [ZAP](zap.md) | Always |
| `browser` | [Browser](browser.md) | Unless the caller passes `include_browser=False` — the [web console](../../guides/web-console.md) does |
| `web_search` | [Web search and page reads](web-search.md) | When the selected search provider can run: Tavily with a key (saved, in the environment, or promptable), or SearXNG with `SEARCH_BASE_URL` |
| `skill_lookup` | [Skills](skill.md) | CLI level, always |
| `recall` | [Recall](recall.md) | CLI level, when `KB_ENABLED=true` |
| `ledger` | [Ledger](ledger.md) | CLI level, when `KB_ENABLED=true` |
| `delegate` | [Delegate and agent_wait](delegate.md) | CLI level, always |
| `agent_wait` | [Delegate and agent_wait](delegate.md) | CLI level, always |

Five tools are registered by the front end rather than the suite because they
need something that does not exist when tools are built: `recall` and `ledger`
need a store keyed to the live engagement id, and `delegate` and `agent_wait`
need the session's agent supervisor. They take late-bound getters instead of the
objects themselves.

Delegated agents receive a subset of this same snapshot, filtered by their
profile's whitelist — see [agent profiles](../agent-profiles.md). They also get
one tool the master never has: `report`, how an agent finishes.

`sast_scan` is deliberately absent from every built-in profile's whitelist,
because a whitelisted call bypasses interactive approval and a wide OpenGrep
scan is expensive enough to be worth seeing first.

## What every tool returns

Every tool returns a `ToolResult`: a success flag, an output string, an optional
error, and optional structured metadata. `success=false` means the tool call
failed — not that it found something bad. A vulnerability scan that reports
forty findings succeeded; a scan whose scanner would not start failed.

Metadata is where the machine-readable detail lives (status codes, session ids,
artifact paths, normalized findings). The output string is what the model reads
as prose.

## Capability gates

Two settings disable whole tools rather than prompting for them. The tool is
still registered — the model can still call it — but the call returns a refusal
with `policy` metadata explaining why.

| Setting | Default | Effect when `false` |
|---|---|---|
| [`EXECUTION_ENABLED`](../configuration.md#tool-execution) | `true` | `exec`, `container_lifecycle`, `sbom_generate`, `vulnerability_scan` and `sast_scan` refuse to run, and `zap` can no longer start its managed container |
| [`WRITE_TOOL_ENABLED`](../configuration.md#tool-execution) | `true` | `write` refuses to run |

`process` is not gated: it drives sessions `exec` already started, and turning
execution off mid-session would leave them unkillable.

## The workspace

Each engagement gets a workspace directory under
[`WORKSPACE_DIR`](../configuration.md#storage-and-logging)
(`~/.kerkov/workspaces` by default), named for the engagement id. It holds bulky
artifacts — scan reports, screenshots, scanner logs — and supplies the default
working directory for commands. Path resolution and confinement depend on the tool:

| Tool | Path behavior |
|---|---|
| [`read`, `write`](read-write.md), [`sast_scan`](sast.md) | Relative paths resolve inside the active workspace and cannot escape it, including through `..` or symlinks. Absolute paths are accepted outside it. |
| [`sbom_generate`, `vulnerability_scan`](sbom-vulnerability-scan.md) | All local targets, including absolute paths, must stay inside the workspace. Image references are not local paths. |
| [`container_lifecycle`](container.md) | Relative `compose_dir` paths resolve from the CLI's launch directory. Compose projects are not confined to the workspace. |
| [`exec`](exec-process.md) | Uses the workspace when `workdir` is omitted. An explicit `workdir` is passed to the process launcher; commands and their file access are not workspace-confined. |
| [`browser`](browser.md) screenshots | Relative paths resolve under `<workspace>/browser/screenshots`; every screenshot path must stay inside that root. |

The whole agent tree shares one workspace, keyed on the root engagement, so a
master can read what its agents wrote. Concurrent agents can therefore overwrite
each other's files by name; namespacing output is a prompt convention, not an
enforced boundary.

## Output size and artifacts

Tool output is normalized before it enters the conversation so one large scan
cannot consume the context window. Output at or below
[`TOOL_OUTPUT_MAX_CHARS`](../configuration.md#context-and-compaction)
(6000) passes through untouched. Larger output collapses repeated lines, then
falls back to a head-and-tail preview.

When the knowledge base is on, the full original output is stored first and the
conversation keeps an `artifact#N` handle to it, so the detail is one
[`recall`](recall.md) away. Several tools go further and write their complete
report to the workspace themselves, returning only a bounded summary — see
[SBOM and vulnerability scan](sbom-vulnerability-scan.md), [SAST](sast.md), and
[container lifecycle](container.md).

For those tools, `recall` retrieves the returned summary. Files named in the result
are not automatically indexed: use [`read`](read-write.md), or filter a large file
with [`exec`](exec-process.md), to inspect the complete report or log.

## Approval breadth

Every tool declares how far a single approval may be generalized when you save
it as a rule. This is the tool's own answer, not a shared default: an origin, a
directory, an action, the whole tool, or nothing at all.

| Breadth | Tools |
|---|---|
| Whole tool | `cve_lookup`, `web_search`, `skill_lookup`, `recall`, `ledger`, `agent_wait` |
| The action | `process`, `zap`, `browser`, `container_lifecycle` |
| The origin | `http`, `read_url` |
| The containing directory | `read`, `write` |
| This call only | `exec`, `sast_scan`, `sbom_generate`, `vulnerability_scan`, `delegate` |

`exec` offers no broad rung on purpose: `sudo *` and `bash *` are blanket grants
wearing a program name, so widening `exec` should be a deliberate edit to
`approvals.yaml`, never a keystroke at a prompt.

Values that vary per call, or that would leak into the rules file, never reach a
saved rule at all — timeouts, `write`'s `content`, `http`'s headers and request
bodies. See [Approvals](../../guides/approvals.md) for the prompt, the modes,
and the rules file.
