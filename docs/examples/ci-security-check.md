# CI security check

Kerkov runs non-interactively, which makes it a natural fit for a CI security
step: one `--message` call, tool approvals auto-accepted, and a reasoned
pass/fail verdict at the end. This example drives two **defensive** scanners —
[`vulnerability_scan`](../reference/tools/sbom-vulnerability-scan.md)
(Grype) against a container image and [`sast_scan`](../reference/tools/sast.md)
(OpenGrep) against source — and applies a build policy to the results. Kerkov
produces the verdict; a small wrapper turns it into the build-failing exit
status CI acts on (see [Failing the build](#failing-the-build)).

!!! note "This is defensive tooling"
    Nothing here attacks a live target. Both tools read artifacts — an image's
    package catalogue and source files — and report known issues. See
    [Non-interactive use](../guides/non-interactive.md) for the CI shape in
    general.

## Prerequisites

- Kerkov [installed](../get-started/install.md) with a working provider.
- **Grype** on your `PATH` (installed separately — Kerkov never downloads it).
- **OpenGrep** installed via `kerkov setup opengrep`.
- A target image pulled locally and a source tree to scan. This run used the
  public `wordpress:php8.2-apache` image and a small deliberately-vulnerable
  Python sample. Recreate the demo tree below to reproduce the run exactly, or
  point the command at your own image, source directory, and rule file.

    `ci-demo/app/payment.py` — the code under test (line numbers match the
    captured findings below):

    ```python
    import os
    import subprocess


    def run_report(user_input):
        # Build a report for the given user input.
        os.system("generate_report " + user_input)        # command injection


    def calc(expr):
        return eval(expr)                                  # arbitrary code exec


    def fetch(host):
        subprocess.call("ping -c1 " + host, shell=True)    # shell=True
    ```

    `sast_scan` takes **local** rule files (remote rulesets are unsupported), so
    the run used a three-rule OpenGrep ruleset matching those patterns.
    `ci-demo/rules/python-ci.yml`:

    ```yaml
    rules:
      - id: os-system-command-injection
        languages: [python]
        severity: ERROR
        message: "Command executed via os.system(); concatenated input is OS command injection."
        pattern: os.system(...)
      - id: eval-dynamic-input
        languages: [python]
        severity: WARNING
        message: "eval() on dynamic input allows arbitrary code execution."
        pattern: eval(...)
      - id: subprocess-shell-true
        languages: [python]
        severity: ERROR
        message: "subprocess call with shell=True invites command injection."
        pattern-either:
          - pattern: subprocess.call(..., shell=True, ...)
          - pattern: subprocess.run(..., shell=True, ...)
          - pattern: subprocess.Popen(..., shell=True, ...)
    ```

## The command

```bash
uv run kerkov --mode audit --no-welcome --verbosity standard \
  --message "Act as a CI security gate for a Python service. Run two scans and \
then decide pass/fail. (1) Run vulnerability_scan on the container image \
wordpress:php8.2-apache and report the vulnerability counts by severity. (2) Run \
sast_scan on the source directory /home/kali/code/ci-demo/app using the rule \
file /home/kali/code/ci-demo/rules/python-ci.yml. Then give a combined summary \
and state whether the build should FAIL under this policy: fail on any Critical \
or High image vulnerability, or any ERROR-severity SAST finding."
```

The paths above are this run's absolute paths; substitute your own image, source
directory, and rule file. `sast_scan` validates that each rule path exists and
rejects a missing file, so point it at a real ruleset — the `python-ci.yml`
above, or your own.

## What it did

Output is real, captured from the run above.

```text
✓ Connected to gpt-5.6-sol
Workspace: /home/kali/.kerkov/workspaces/2026-10-07-8d47c1bc

I'll run the image vulnerability scan and Python SAST scan in parallel, then
apply the stated gate exactly: fail for any Critical/High image vulnerability or
any ERROR SAST finding.

→ Executing 2 tool(s): vulnerability_scan, sast_scan

vulnerability_scan  target=wordpress:php8.2-apache  source_type=image
  {
    "engine": "grype",
    "status": "completed_unsummarized",
    "reason": "scanner report is too large to summarize safely (18309307 bytes;
               limit 16777216 bytes); the complete report remains at
               .../vulnerability-scans/grype-call_<id>.json",
    "artifacts": { "report": ".../grype-call_<id>.json", "log": ".../grype-call_<id>.log" }
  }

sast_scan  target=/home/kali/code/ci-demo/app  configs=[python-ci.yml]
  {
    "engine": "opengrep", "engine_version": "1.27.1",
    "finding_count": 3,
    "counts_by_severity": { "ERROR": 2, "WARNING": 1 },
    "findings": [
      { "severity": "ERROR",   "path": "payment.py", "start": {"line": 7},
        "message": "Command executed via os.system(); concatenated input is OS command injection." },
      { "severity": "ERROR",   "path": "payment.py", "start": {"line": 15},
        "message": "subprocess call with shell=True invites command injection." },
      { "severity": "WARNING", "path": "payment.py", "start": {"line": 11},
        "message": "eval() on dynamic input allows arbitrary code execution." }
    ]
  }

The image report was too large for the scanner's inline summary, so I'll extract
severity counts from the saved Grype JSON without rerunning the scan.

→ Executing 1 tool(s): exec
exec  jq ... grype-call_<id>.json
  ✗ Error: bash: line 1: jq: command not found

→ Executing 1 tool(s): write
write → scripts/ci-gate/count_grype_severities.py   (a 20-line severity counter)

→ Executing 1 tool(s): exec
exec  python3 scripts/ci-gate/count_grype_severities.py .../grype-call_<id>.json
  {
    "total": 1277,
    "counts_by_severity": {
      "Critical": 34, "High": 224, "Medium": 200,
      "Low": 49, "Negligible": 764, "Unknown": 6
    }
  }

→ Executing 2 tool(s): ledger, ledger
  Recorded to findings ledger as entry #1.   (image: 34 Critical, 224 High — violates policy)
  Recorded to findings ledger as entry #2.   (SAST: 2 ERROR command-injection findings)

# CI Security Gate: FAIL

## 1. Container vulnerability scan
Image: wordpress:php8.2-apache  Scanner: Grype  Total matches: 1,277
| Severity | Count |
| Critical | 34  |
| High     | 224 |
| Medium   | 200 |
| Low      | 49  |
| Negligible | 764 |
| Unknown  | 6   |
Image gate: FAIL — policy fails on any Critical or High.

## 2. Python SAST scan
Source: /home/kali/code/ci-demo/app  Scanner: OpenGrep 1.27.1  Findings: 3 (2 ERROR, 1 WARNING)
- ERROR  — OS command injection via os.system() at payment.py:7
- ERROR  — command injection from subprocess shell=True at payment.py:15
- WARNING — arbitrary code execution via dynamic eval() at payment.py:11
SAST gate: FAIL — policy fails on any ERROR finding.

## Combined decision
Image contains Critical/High vulnerabilities: Yes
SAST contains ERROR findings: Yes

# BUILD SHOULD FAIL
```

## Failing the build

`kerkov --message` prints the verdict but **exits `0`** whenever the run itself
succeeds — the gate decision lives in the output and the
[findings ledger](../reference/tools/ledger.md), not the process exit status. The
printed `BUILD SHOULD FAIL` above does **not**, on its own, fail the pipeline.

To turn the verdict into a build-failing exit status, capture the output and act
on a stable sentinel. Ask the model to end its reply with one machine-readable
line, then check for it:

```bash
#!/usr/bin/env bash
set -euo pipefail

output=$(uv run kerkov --mode audit --no-welcome --verbosity standard \
  --message "...the scan prompt above... End your reply with exactly one line: \
GATE: PASS or GATE: FAIL.")

printf '%s\n' "$output"

if printf '%s\n' "$output" | grep -qx 'GATE: FAIL'; then
    echo "Security gate failed." >&2
    exit 1
fi
```

The scans still run inside Kerkov; the wrapper only translates the decision the
model already made into the pass/fail status CI consumes. Keep the sentinel
exact and on its own line so the `grep` is unambiguous.

## What to take away

- **One call, two scanners, a verdict.** A single non-interactive invocation ran
  both scans and reconciled their output against an explicit policy to produce a
  clear FAIL — the raw material a pipeline step turns into a pass/fail exit
  status (see [Failing the build](#failing-the-build)).
- **Large reports stay on disk, not in the model's context.** Grype's full report
  was 18 MB — over the summary limit — so `vulnerability_scan` returned
  `completed_unsummarized` with the artifact path instead of flooding the
  conversation. The model read the saved JSON to get the counts it needed.
- **It adapts around a missing tool.** With `jq` absent, the model wrote a
  20-line Python counter into the workspace and ran that — the kind of recovery
  the [exec and process tools](../reference/tools/exec-process.md) make routine.
- **`sast_scan` uses local rules only.** It runs an existing OpenGrep binary over
  host-local targets and **local** rule files; it never fetches remote rulesets
  or installs software during a scan.

## Related pages

- [SBOM and vulnerability scan](../reference/tools/sbom-vulnerability-scan.md)
- [SAST](../reference/tools/sast.md)
- [Non-interactive use](../guides/non-interactive.md)
- [Exec and process tools](../reference/tools/exec-process.md)
