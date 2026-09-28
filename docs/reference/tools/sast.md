# `sast_scan`

Static analysis of source code, using the open-source
[OpenGrep](https://github.com/opengrep/opengrep) engine. Finds vulnerabilities in
code you can read, as opposed to
[`vulnerability_scan`](sbom-vulnerability-scan.md), which finds them in
dependencies you pulled.

No paid service and no API key. A working `opengrep` on `PATH` is used if present;
otherwise `kerkov setup opengrep` installs one. `sast_scan` itself never downloads
or installs anything.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `target` | string | `.` | File or directory to scan. Absolute host-local paths are accepted; relative paths resolve from the engagement workspace and stay confined to it. |
| `config_paths` | array[string] | — | Rule files or directories. Same path rules as `target`. Registry names and URLs are **not** accepted. |
| `discover_configs` | boolean | `false` | Opt in to discovering standard OpenGrep/Semgrep YAML beside the target and at the workspace root. |
| `severities` | array of `INFO` \| `WARNING` \| `ERROR` | all | Severities to report. |
| `include` | array[string] | — | Gitignore-style include patterns. |
| `exclude` | array[string] | — | Gitignore-style exclude patterns. |
| `intrafile_taint` | boolean | `true` | OpenGrep's intrafile/interprocedural taint analysis. |
| `timeout_seconds` | integer | `180` | One shared deadline for the scan *and* the bounded report processing. |
| `max_findings` | integer | `50` | Findings returned to the model; the full report is always kept as an artifact. |

Unlike [`sbom_generate`](sbom-vulnerability-scan.md), `sast_scan` accepts absolute
paths — it reads source, it does not fetch images or update databases, so pointing
it at a checkout outside the workspace is reasonable. Relative paths are still
workspace-confined, including through symlinks.

## A deliberately narrow interface

The adapter exposes less than the raw OpenGrep CLI, on purpose:

- **Local rules only.** No registry names, no URLs, no arbitrary scanner flags, no
  builds, no autofix.
- **Config discovery is off by default.** A discovered YAML file *controls scan
  policy*, so `discover_configs=true` should be reserved for source trees you have
  reviewed.
- **No shell.** OpenGrep runs as an argument vector, in an environment with
  Kerkov-managed credentials removed.
- **Bounded before parsing.** Report size and result count are capped, and parsing
  happens off the event loop.

### What the model is shown

The summary is redacted as well as bounded. It carries severity-sorted findings
**without** source snippets, metavariable contents, or raw diagnostics, and rule
messages have interpolated metavariable values replaced with a redaction marker.

The reasoning: a SAST finding's message frequently quotes the matched code, and
the matched code in a security scan is frequently the secret. The unredacted
report is on disk for you to read; the conversation gets the finding, not the
payload.

## Approval

Prompts in `strict` and `normal`; runs automatically in `permissive` and `yolo`.
There is no allow rule — the scan is host-local and read-only, but a wide target
is CPU-expensive, and its process execution is worth seeing.

Approval offers **no broad rung**: a saved approval covers that call only, since
what a scan costs depends entirely on the target and the rules.
`timeout_seconds` never reaches a saved rule.

Gated by [`EXECUTION_ENABLED`](../configuration.md#tool-execution), shared with
`exec` and `container_lifecycle`.

!!! note "Not available to delegated agents"
    No built-in [agent profile](../agent-profiles.md) whitelists `sast_scan`. A
    whitelisted call bypasses interactive approval, and a wide OpenGrep scan is
    expensive enough that the operator should see it first. A user-defined
    `AGENT.md` can name it in `allowed_tools` and accept that tradeoff.

## Setup

```bash
kerkov setup opengrep
```

Downloads the latest stable official release, checks it against the SHA-256 digest
in GitHub's release metadata, and installs it under `~/.kerkov/tools/opengrep/`.
User-local, no `sudo`, invoked by absolute path — no shell restart or `PATH` edit.
An existing working installation is reused and not auto-updated.

The digest detects transfer corruption or mismatch. It does not independently
establish release provenance.

If no usable binary can be found, the scan fails *before* creating any artifact,
with instructions to run the setup command. The setup command is outside the
model's tool surface: installing software is an operator action.

## Returns

A redacted, bounded, severity-sorted summary as JSON output. The complete JSON report
and the console log are written to `artifacts/sast/` in the engagement workspace,
with their paths in `metadata.report_artifact` and `metadata.log_artifact`.

The JSON output carries the normalized `findings`, counts, scan details, and
`artifacts` with the `report` and `log` paths. Each finding includes `cwe`, `owasp`,
`confidence`, `likelihood`, `impact`, `technology`, and `vulnerability_class` where
the rule supplies them.

Metadata carries `status`, `engine`, `engine_version`, `finding_count`,
`findings_truncated`, `report_artifact`, `log_artifact`, and `runtime_source`.

`status` is `completed`, or `completed_with_errors` when the scan finished but
OpenGrep reported problems on some files.

!!! important "Findings are a successful scan"
    `success: false` means the scanner did not start, timed out, or produced a
    malformed report. Findings are `success: true`.

## Examples

Scan the workspace with an explicit ruleset:

```json
{ "target": ".", "config_paths": ["rules/python-security.yaml"] }
```

High-severity findings in one directory of a checkout outside the workspace:

```json
{
  "target": "/home/kali/code/target-app/src",
  "config_paths": ["/home/kali/rules/owasp-top-ten"],
  "severities": ["ERROR"]
}
```

Trusting a reviewed tree's own configuration:

```json
{ "target": "src/", "discover_configs": true, "exclude": ["tests/**"] }
```

## Related

- [`vulnerability_scan`](sbom-vulnerability-scan.md) — known CVEs in dependencies.
  The two answer different questions and both are usually worth running.
- [`read`](read-write.md) — read the unredacted report at `metadata.report_artifact`,
  or filter a large report with [`exec`](exec-process.md).
- [`recall`](recall.md) — retrieve the returned summary instead of re-scanning.
  The unredacted report file is not automatically indexed.
- [`ledger`](ledger.md) — record a confirmed finding so it survives compaction.
