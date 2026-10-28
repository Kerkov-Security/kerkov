# Compliance assessment

Use `compliance_scan` to assess an authorized SSH host with **Cinc Auditor in an
ephemeral Docker container**, then use [`ansible`](ansible.md) for separately
approved remediation and assess again. Kerkov interprets evidence from existing
engines; it does not invent compliance verdicts from a successful playbook recap.

The controller needs a running local Docker daemon and the Docker CLI. Linux,
macOS (Docker Desktop or an equivalent local runtime), and WSL controllers are
supported. The SSH target needs normal system utilities and sufficient
permissions. Cinc, Ruby, OpenSCAP, Python, and Docker are not required on the
target by this assessment transport; individual controls can have additional
requirements. Ansible remediation retains its own target requirements.

The Docker integration uses the same preflight diagnostics, missing-image pull
policy, and ownership labels as Kerkov's ZAP/SearXNG runtimes. Long assessment
execution uses the shared command runner for private logs, process ownership,
timeouts, and cancellation. There is no published API port or long-running Cinc
service.

The stock Kerkov/ZAP Compose stack does not grant Kerkov access to a Docker
daemon, so it does not run this scanner out of the box. Use Kerkov on your Mac
as the controller. A custom containerized controller needs Docker CLI/daemon
access and matching absolute paths visible to the daemon for bind mounts; this
feature does not add a Docker socket mount to the default stack.

## Built-in baselines

The web console's first **Compliance** tab offers two built-in baselines with
matching Ansible playbooks. Each lives under the installed package's
`config/compliance/<baseline>/` directory: `inspec.yml` and `controls/baseline.rb`
define the checks, and `remediate.yml` defines their fixes. The catalog in
`core/compliance/baselines.py` binds each baseline to its control IDs and packaged
paths. There are no downloads or automatic baseline generation.

- **Linux account protection** (`linux-accounts`) supports Debian and Ubuntu.
  It checks four existing regular files: passwd/group must be `root:root` with
  mode `0644`, and shadow/gshadow must be `root:shadow` with mode `0640`. Its
  playbook checks OS compatibility, refuses symlinks or missing/non-regular files,
  and applies these ownership/mode settings. It does not change account entries.
- **SSH essentials** (`ssh-essentials`) supports Debian/Ubuntu with an existing,
  running systemd `ssh` or `sshd` service. It checks `sshd -T` for
  `PermitEmptyPasswords no`, `IgnoreRhosts yes`, `X11Forwarding no`,
  positive `MaxAuthTries` values up to 4, and positive `LoginGraceTime` values up
  to 60 seconds. Version 1.1 reads effective limits before remediation and retains
  stricter positive values. A zero (unlimited) login grace is replaced with 60.
  Its playbook places a managed global
  block before the existing configuration, validates a candidate with `sshd -t`,
  saves a backup, and reloads the service only when changed. Root/password login
  and unrelated settings remain as configured.

SSH coverage is global: `Match` blocks can override global values for specific
connections and need separate review. OpenSSH uses the first global value for
each keyword. See the [OpenSSH configuration reference](https://man.openbsd.org/sshd_config).
Candidate validation uses Ansible's existing
[`blockinfile` validation](https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/blockinfile_module.html).

Built-in GUI remediation selects `remediate.yml` from the initial scan's profile
snapshot. Reassessment must use the same profile hash and connection settings;
playbook success alone is not a verified baseline result.

Both baselines require elevated target access and normal existing target
utilities. Ansible also needs its usual target-side Python. They do not install
packages or disable host-key verification. These are focused, versioned technical
baselines, not complete CIS, STIG or PCI benchmarks. The existing Linux example
below remains an integration sample.

Locate all built-in paths from the installed package:

```bash
uv run python -c 'from kerkov.core.compliance.baselines import builtin_baselines; print(builtin_baselines())'
```

## Prepare a profile and SSH access

The default image is `cincproject/auditor:7`. Set `CINC_DOCKER_IMAGE` in your
operator configuration to select a specific official release or digest. Kerkov
pulls it only when missing, resolves its immutable local image ID, and executes
that ID. It records the configured reference and resolved ID in the run manifest.
It does not update an already-cached tag on each scan.

Profiles are trusted executable Ruby, including any local resources. Review and
pin a profile matching the target OS and benchmark. This version accepts a
**self-contained local directory with `inspec.yml`**; profile/gem dependencies,
remote profile URLs, and automatic dependency installation are unsupported.
Package dependent profiles into a reviewed self-contained profile before use.
Profile metadata must have a simple `name`. The snapshot excludes `.git`, the
workspace artifact subtree, and the explicitly supplied SSH/input files. It refuses
profiles rooted inside that artifact subtree, unreadable directories, symlinks,
and special files and is bounded to 5,000 files / 64 MiB. Rejected
snapshots are removed before returning the error. Its SHA-256
covers the ordered relative file paths and contents. The entire copied profile
is mounted read-only for that assessment.

A small starter profile ships at:

```text
src/kerkov/config/compliance/linux-example/
```

Locate the installed example with:

```bash
uv run python -c 'from pathlib import Path; import kerkov.config; print(Path(kerkov.config.__file__).parent / "compliance" / "linux-example")'
```

Copy it into the workspace and review it. It checks `/etc/passwd` ownership and
whether `sshd -T` reports direct root login disabled. Use sufficient privileges
for `sshd -T`. This is an integration example, **not a CIS/STIG benchmark**.

Provide a dedicated SSH private-key file and an independently trusted
`known_hosts` file. On POSIX the key must belong to the current user and have
owner-only permissions, such as 0600 or 0400. Only those individual files are mounted, read-only; Kerkov does
not mount your whole home directory, forward an SSH agent, or expose the Docker
socket to the scanner. Encrypted keys requiring interactive passphrases and
password authentication are not exposed by this first version.

Host-key verification is explicitly enabled. Unknown or mismatched keys fail;
Kerkov never adds trust-on-first-use entries. Host SSH configuration is not
inherited, so proxies/jump hosts and custom SSH aliases must not be assumed.
The container needs direct network access to the specified host and port.
`host.docker.internal` reaches the Docker host; `localhost` means the scanner
container, not your Mac. Known-host entries must match the supplied address and
port (for a non-default port, use `[host]:port`).

## Run an assessment

Arguments to `compliance_scan`:

```json
{
  "host": "192.0.2.10",
  "user": "audit",
  "profile": "profiles/linux-baseline",
  "private_key_file": "/home/operator/.ssh/audit_key",
  "known_hosts_file": "/home/operator/.ssh/known_hosts",
  "sudo": true,
  "timeout_seconds": 900,
  "max_controls": 30
}
```

Replace the example address and controller-local credential paths. Paths relative
to the active engagement workspace cannot escape it, including through symlinks;
absolute local paths are accepted. Docker mount paths cannot contain commas or
control characters. `sudo=true` requests target-side elevation and requires
noninteractive sudo authorization; no sudo password is sent in tool arguments.
Assessment profiles can issue commands on the target and execute code in the
scanner. Assessment is not an enforced read-only permission boundary.

| Parameter | Default | Meaning |
|---|---|---|
| `host`, `user` | Required | SSH hostname/IP reachable from the container and login user. No credential-bearing URI. |
| `port` | `22` | SSH port, 1–65535. |
| `profile` | Required | Reviewed, self-contained local profile directory. |
| `private_key_file` | Required | Owner-only local SSH private-key reference. |
| `known_hosts_file` | Required | Trusted local known-hosts reference. |
| `inputs_file` | — | Optional reviewed local YAML input file, mounted read-only. Input values are not tool arguments. |
| `sudo` | `false` | Request target-side sudo for assessment commands. |
| `timeout_seconds` | `600` | Preflight, optional image pull, and assessment deadline, 1–3600 seconds. Bounded container cleanup follows. |
| `max_controls` | `30` | Returned controls, 1–200, with errors/failures first. Full normalized results remain on disk. |

`EXECUTION_ENABLED=false` prevents Docker access and run artifact creation. The
tool is registered for both CLI and web sessions through ToolSuite and is absent
from built-in agent whitelists. Default approval rules leave it unmatched, so
NORMAL and STRICT modes prompt. Mutable executable profiles are not offered as
saved interactive grants. No target software or configuration is installed or
changed by built-in preparation tasks; reviewed profiles themselves remain
trusted executable input.

## Results, artifacts, and cleanup

Each assessment gets `artifacts/compliance/scan-*/` in the shared root engagement
workspace. It contains the profile snapshot, generated connection settings,
`scanner.log`, exact `output/source.json`, complete `normalized.json`, and
`manifest.json`. The container writes evidence into a dedicated `output/` directory;
normalization reuses that source without making a second full copy.
Credential file contents are not copied into these artifacts. Raw scanner JSON
can contain input values and sensitive configuration details; directories are
private on POSIX, and artifacts remain plaintext on local disk.

The manifest records the explicit SSH target, sudo request, profile snapshot hash,
image reference/ID, container name, execution outcome, and evidence references.
Image and cleanup diagnostics remain in the result and manifest when execution
fails or is cancelled. Timeouts name the phase that exhausted the deadline.
It does not infer or update ledger asset bindings. Record confirmed findings
through [`ledger`](ledger.md) with appropriate evidence links.

Cinc exit codes 0, 100 (test failures), and 101 (skips) are accepted only when a
valid report is present. Other exit codes, missing/malformed evidence, failed
profiles, and cleanup errors fail the tool call. A completed assessment with
failed controls is a successful execution with findings, not a clean bill of
health. Summaries separate passed, failed, errored, skipped, empty and waived
controls. Mixed pass/skip controls remain skipped and exceptions remain errors.
A control is waived only when the report explicitly marks it skipped due to a
waiver; expired waivers and waivers that allow evaluation preserve test outcomes.
Failures and errors take precedence even when waiver data is present. Unknown
status strings are errors. Full per-control test counts remain available. Raw
messages, code, inputs, backtraces, and resource payloads are excluded from model
summaries.

Fresh assessments carry `evidence_status: "scanned"` in their results, normalized
reports, and manifests. Importing existing JSON uses `evidence_status: "imported"`.

The runtime uses a fresh ownership-labeled container, drops capabilities, uses a
read-only root filesystem, and mounts the profile/configuration/SSH files
read-only. It publishes no ports, uses no privileged mode, and mounts no Docker
socket. Cleanup verifies this run's ownership labels before removal; an unrelated
container is never removed on a name collision. Timeout/cancellation terminate
the local Docker client and remove the owned scanner container, including a
container whose creation finishes after cancellation. If Docker is unavailable
during cleanup, inspect the saved container name and ownership labels. Stopping
the scanner cannot roll back changes made by trusted profile code or guarantee
that every remote command has terminated.

## Import existing evidence

`compliance_report` reads existing Cinc/InSpec JSON without starting a scanner.
This works with execution disabled and without Docker, Ansible, or the knowledge
base.

```json
{
  "report": "reports/audit.json",
  "expected_profile": "linux-baseline",
  "max_controls": 30
}
```

| Parameter | Default | Meaning |
|---|---|---|
| `report` | Required | Local Cinc/InSpec JSON, up to 64 MiB. Relative paths stay workspace-confined; absolute paths are accepted. |
| `expected_profile` | — | Require the named Cinc profile in the report. |
| `max_controls` | `30` | Returned controls, 1–200. Saved normalization retains all entries. |

An import preserves an exact source copy, its SHA-256, and import time under
`artifacts/compliance/report-*/` before parsing. Rejected reports still return
the evidence path and hash; successful imports also save normalized JSON. Imported host labels
are not independently authenticated. Hashes identify bytes, not the producer.
The importer does not certify compliance, bind resources, approve exceptions,
or update the ledger. Its default approval scope is the exact report
call. `max_controls` and runtime deadlines do not enter saved rules.

## Remediate and reassess

Cinc profiles express checks in Ruby; Ansible playbooks express changes in YAML.
There is no automatic conversion of findings into reviewed remediation. Map failed
control IDs to appropriate trusted playbooks, inspect the proposed host/configuration
changes, obtain normal Ansible approval, and reassess against the same pinned
profile after execution. A successful remediation recap alone does not close a
finding. Exception approval, automatic drift comparison, scheduling, and
organization-wide certification are outside this first integration.

See the [Cinc container documentation](https://cinc.sh/docs/containers/) for the
upstream runtime and [Ansible automation](ansible.md) for remediation execution.
