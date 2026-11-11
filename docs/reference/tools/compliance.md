# Compliance assessment

Use `compliance_scan` to assess an authorized SSH host. InSpec profiles use
**Cinc Auditor in an ephemeral Docker container**. SCAP source datastreams use
[OpenSCAP on the target](#assess-with-openscap). Approve any remediation
separately through [`ansible`](ansible.md), then assess again. A successful
playbook recap confirms execution; the evidence records control outcomes.

For Cinc, the controller needs a running local Docker daemon and the Docker CLI. Linux,
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

Use `compliance_profile` to discover the default upstream catalog, validate a
public GitHub URL, and download/prepare a project before assessment. This
preparation uses the same approved tool loop, Docker ownership/cleanup and
cancellable process runner. It does not connect to or assess an SSH target.

## Web workflow

Open **Workflows → Compliance**, choose an inventory asset and baseline, and
enter or retrieve the existing SSH file references. **Assess asset** runs checks
without remediation. **Preview changes** assesses first, then requests Ansible
check mode when changes are needed. Review the saved plan and choose **Use this
preview to apply**, followed by **Apply reviewed changes**. Apply requires a
completed preview with matching asset, connection settings, profile/playbook/input
hashes and configured scanner image. Changed settings or files require another
preview. The workflow rechecks the complete scanner profile and reviewed plan
files before approval and again after approval, including the profile used for
verification. Normal approval rules and execution gating still apply to every tool.

Controller readiness checks cover the profile, credential-file references and
key permissions, Docker availability, and Ansible availability when needed.
These do not authenticate to the target or establish target OS compatibility or
sudo privileges; the approved scan and remediation check those prerequisites.
Kerkov does not install controller dependencies from this screen. Preview does
not install target Python, though other check-mode tasks can still mutate.
**Allow installing missing Python 3 during apply** is unchecked by default. When
enabled, apply may install Python from target repositories on Debian/Ubuntu/Alpine
using privilege escalation before running the selected playbook. Changing this
option requires a new matching preview.

The panel shows saved stages, readiness failures, a remediation plan, and
control-level before/after outcomes. Failed, errored, skipped, waived and empty
checks remain distinct. **Before evidence**, **After evidence** and **Remediation
log** open bounded local artifact previews. Full evidence paths remain in the
plan details. After is unassessed until a follow-up scan has produced evidence;
a successful playbook alone never verifies the baseline.

Select a control ID to open its evidence drawer. It shows the saved before/after
assertions, observed failure messages, skip reasons and errors, plus an explicit
remediation mapping for newly prepared built-in baseline plans. Custom profiles
and older plans without a saved mapping require manual review. Check descriptions
and messages come from Cinc evidence; Kerkov does not invent expected or observed
values. Search by control ID, title or profile, or filter by the latest assessed
outcome. **Unresolved** includes every outcome other than passed. These filters
apply to the loaded control list, which remains bounded to 200 controls.

**Download HTML report** and **Download JSON report** export the selected saved
run directly to your browser, without starting a model turn or executing tools.
Exports include every control from the original evidence, independently of the
table's filters and display limit, with asset/SSH identity, timestamps, baseline
and scanner versions, profile/evidence hashes, scanner image identity, remediation
execution and before/after outcomes. HTML reports are self-contained and printable;
JSON reports use `schema_version: 1`. Reports do not include SSH file contents,
profile source, input attributes or backtraces. Assertion descriptions and messages
can contain sensitive target configuration and are included in these local reviews
and downloads, but are not added to model summaries or conversation history.

Both views show at most 20 assertions per control per phase and 2,000 characters
per text field, with visible truncation notices. Full assertions remain in the
saved source JSON. Exports are limited to 16 MiB and fail explicitly above that
limit. Original evidence must remain under this engagement's compliance artifact
directory and match its saved SHA-256; unavailable, altered or ambiguous evidence
cannot produce an export. An interrupted run exports an incomplete final outcome;
remediation success never fills an unassessed verification column.

The server records runs atomically under
`automation/compliance-runs/` in the engagement workspace, independently of chat
compaction. The latest 30 records are restored when the engagement is opened.
An unfinished record from a previous connection is shown as interrupted with an
unknown outcome. **Reassess this asset** starts assessment only using the saved
profile snapshot and original connection/input settings, comparing with prior
control outcomes when available. It does not repeat remediation or rewrite the
previous result. Changed original profile/input/trust files require a new
assessment with reviewed settings. Runs that failed before preparing a profile
require a new assessment; **Reassess this asset** is disabled for those records.

The GUI submits a structured plan. Server-side stage checks reject unexpected
calls, changed SSH endpoints, remediation before assessment, preview calls that
apply changes, and repeated remediation within a run. Execution remains on the
shared conversation engine and approval strategy. The tool list remains stable
for provider caching; the plan gate rejects unrelated calls without ending the
workflow. Denied execution or changed reviewed files stop further changes.
Read-only inspection of the selected saved Ansible profile uses normal approvals.
Run-history write failures are reported in the panel and block further execution;
local scanner and remediation artifacts remain available for review. These checks
constrain the selected workflow; trusted Ruby profiles and Ansible playbooks remain executable
code, including delegation or controller-side actions they contain.

For custom remediation, select an existing immutable Ansible profile, declared
playbook and exact host alias matching the assessment endpoint. The workflow
generates a single-host inventory that preserves the selected alias and its group
memberships, with explicit SSH trust settings. The generated inventory is also
included in the reviewed plan hashes. Supply
additional playbook variables through the reviewed variables file. Custom
control-to-fix mapping and applicability still require review; the GUI does not
generate benchmark controls or infer framework certification.

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
utilities. GUI remediation enables the Ansible Python bootstrap: if Python 3 is
missing, an approved apply installs it on Debian/Ubuntu before gathering facts.
Preview reports a missing interpreter without installing it. Assessment itself
does not need target-side Python and never installs it. Only Python and its required packages are
installed by the bootstrap; host-key verification stays enabled. These are focused, versioned technical
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

### Catalog and URL downloads

Under **Community profiles**, choose **Ubuntu 22.04 STIG** (MITRE SAF) or
**Linux hardening** (DevSec), or paste a public GitHub repository URL and choose
**Validate URL**. Catalog entries contain source definitions, not third-party
checks copied into Kerkov's repository. Both choices use the same download and
preparation path as custom URLs.

URL validation checks HTTPS/GitHub syntax, public repository accessibility, a
usable revision and an ordinary root `inspec.yml` with supported metadata. It
does not execute profile code, download the archive, validate every control or
assess a target. Invalid links, unavailable/private repositories, missing profile
metadata and API rate limits produce an actionable error before preparation.
Only `https://github.com/owner/repository` and optional `/tree/branch-or-tag`
URLs are accepted. Credentials, query strings, fragments, subdirectory URLs and
slash-containing branch/tag URLs are unsupported in this version.

Validation displays the resolved project commit and each direct and transitive
dependency's GitHub URL and full Git commit. Imported dependencies must use a
public GitHub repository root in `git` and a full commit in `ref` that is
reachable from that repository's default branch.

After reviewing those sources, choose **Download & prepare**. Normal tool
approval and `EXECUTION_ENABLED` apply because trusted profile code may execute
during Cinc's offline check. Kerkov verifies that the reviewed commit is
reachable from the repository's default branch or the branch or tag in the URL.
It downloads that exact commit even if the branch has advanced, then rebuilds
the offline bundle and lockfile for Cinc to check.
**Assess asset** is the separate target assessment. Supply an inputs file when
the profile requires environment-specific inputs.

Ready copies live under `COMPLIANCE_PROJECTS_DIR`, outside the source repository.
Selecting a downloaded catalog project checks the local copy without contacting
GitHub. Choose **Check for updates** to compare the installed and upstream
revisions and dependency pins. When a new commit is available, choose
**Update to …** after reviewing it. Errors, timeouts and
cancellation during preparation retain the previous ready copy. Concurrent
preparation of the same source is rejected. After a successful refresh, old
cache generations are pruned. Saved assessment snapshots and reports remain
available. If another session refreshes a project you have selected, reselect
the project to pick up the new ready path.

The ready cache record stores the downloaded commit and source URL. The copy
also carries them in `.kerkov-source.json`. A scan or plan prepared directly
from the cache marks the source as cache-verified when the generation, source
identity and copied profile digest match the ready record. Other copies report
their sidecar metadata as self-reported.
Downloads reject unsafe redirects, traversal, archive links/special
files, duplicate files, and excessive compressed/expanded content. Preparation
requires a local Unix-socket Docker daemon on Linux/macOS/WSL. Only the staged
project is mounted for preparation; assessment SSH files are not mounted there.

The CLI/model-facing tool has these operations:

```json
{"action": "list"}
{"action": "validate", "url": "https://github.com/dev-sec/linux-baseline"}
{"action": "prepare", "url": "https://github.com/dev-sec/linux-baseline", "expected_commit": "<validated commit>"}
{"action": "prepare", "url": "https://github.com/dev-sec/linux-baseline", "expected_commit": "<validated commit>", "refresh": true}
```

Invoke them with `compliance_profile`. `prepare` returns a ready `profile` path
for `compliance_scan`. It requires an active engagement for private preparation
logs; `list` and `validate` do not execute processes. Preparation defaults to
600 seconds (`timeout_seconds`: 30–1,800). Saved interactive grants apply only
to read-only `list` and `validate` actions. A ready copy can be reused offline
without `expected_commit`. The cache is shared across engagements.
Upstream checks and downloads require direct access to GitHub. They ignore
ambient proxy and CA override variables and use GitHub's anonymous API quota.
Upstream licenses and notices remain with the downloaded project.

Profiles are trusted executable Ruby, including any local resources. Review and
select a profile matching the target OS and benchmark. Kerkov accepts an existing
**local InSpec project directory with `inspec.yml`**, including custom resources,
inputs and prepared offline profile dependencies. You can use downloaded
projects without rewriting their controls or registering a built-in baseline.
Direct URLs in `compliance_scan`, gem dependencies and automatic dependency
installation during assessments remain unsupported; use `compliance_profile`
for URL preparation first.
Profile metadata must have a simple `name`. The snapshot excludes `.git`, the
workspace artifact subtree, and the explicitly supplied SSH/input files. It refuses
profiles rooted inside that artifact subtree, unreadable directories, symlinks,
and special files and is bounded to 5,000 files / 64 MiB. Rejected
snapshots are removed before returning the error. Its SHA-256
covers the ordered relative file paths and contents. The entire copied profile
is mounted read-only for that assessment.

### Reuse an existing profile project

For projects already downloaded using other tooling, the local workflow remains
available:

1. Download or check out a reviewed upstream release into a local directory.
   Pin its full commit or retain a verified release archive; keep its license
   and notice files. The catalog/URL workflow above automates acquisition when
   using a supported public GitHub project.
2. If `inspec.yml` declares `depends`, prepare the dependencies using upstream
   `inspec vendor` or `cinc-auditor vendor`. Review the resolved versions before
   scanning. The project must contain a version-1 `inspec.lock` and unpacked
   `vendor/<cache-key>/` directories for every dependency, including transitive
   resources. Cache keys must be SHA-256 content hashes or full Git commit IDs.
   Gem resource packs and archive-only cache entries are unsupported.
3. Supply a reviewed inputs file when the project needs environment-specific
   values. Use the upstream input definitions; do not guess approved users,
   time sources or other organizational settings.
4. In the console, choose **Existing InSpec project**, enter its local directory
   and optional inputs file, then choose **Assess asset**. An Ansible profile
   is needed only when explicitly requesting preview/apply remediation.
   The CLI's `compliance_scan` uses the same project directory and inputs file.

For an installed Cinc Auditor, preparation uses:

```bash
cinc-auditor vendor /absolute/path/to/reviewed-project
```

Alternatively, prepare a downloaded public project with the scanner image:

```bash
docker run --rm --user "$(id -u):$(id -g)" \
  --read-only --tmpfs /tmp --env HOME=/tmp \
  --mount type=bind,src=/absolute/path/to/reviewed-project,dst=/profile \
  cincproject/auditor:7 vendor /profile
```

Preparation can access the upstream dependency sources and writes the lockfile
and vendor directory; it is separate from an assessment. For private sources,
prepare with your existing authenticated tooling rather than embedding secrets
in project metadata. Transfer the complete prepared directory for offline use.

Kerkov checks that the declared and locked dependency graphs agree and that
every referenced vendor entry is present inside the project before execution.
It rejects missing entries, gem dependencies, cycles, active cache locks,
nested vendor caches, and graphs exceeding 64 dependencies or 8 levels. Flatten
any dependency's own vendor cache into the root project cache during preparation.
The full bundle remains subject to the 5,000-file / 64 MiB snapshot limit.
Missing dependencies never trigger a scanner download. Source URLs and original
checkout paths in the lockfile are locators for preparation; execution uses the
copied vendor entries. Cinc receives a private writable temporary cache while
the evidence snapshot stays read-only.

The scan manifest and saved web plan record the original project name/version
and each dependency's name, profile name, version and cache key. These fields
exclude input defaults and source credentials. The bundle's `profile_sha256`
hash covers its actual files, including controls, libraries, inputs declared in
metadata, lockfiles and vendored resources; a vendor cache key is not a separate
Kerkov attestation of upstream authenticity. Reassessment uses the original
snapshot and rejects changed bundle contents. HTML/JSON exports retain the
project/dependency identity alongside the saved evidence and hashes.

Useful upstream projects include the
[MITRE SAF validation profiles](https://saf.mitre.org/framework/validate) for
technology-specific STIG assessments and the
[DevSec Linux baseline](https://github.com/dev-sec/linux-baseline) for general
hardening checks. Profile coverage, supported targets, manual checks and input
requirements come from those projects. Kerkov has no MITRE- or DevSec-specific
execution path. Use the [OpenSCAP workflow](#assess-with-openscap) for SCAP
datastreams. The InSpec project catalog accepts InSpec profiles.

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
For a local Unix-socket Docker daemon, loopback targets keep their controller
meaning: macOS routes `127.0.0.1` / `localhost` through `host.docker.internal`,
while Linux uses host networking. macOS uses only a generated SSH configuration,
with `HostKeyAlias` preserving the original approved host and port. Existing
trusted or hashed known-host entries therefore still apply; trust files are not
rewritten or extended. The manifest records both the selected target and its
transport route. Other macOS loopback addresses and remote Docker endpoints are
rejected with guidance instead of silently scanning the scanner itself.

Other addresses must be reachable directly from the scanner. An explicitly
selected `host.docker.internal` is a separate SSH identity and needs a matching
known-host entry. For non-default ports, use `[host]:port`.

A follow-up scan may use an earlier completed assessment's `profile` snapshot.
Its name and SHA-256 must still match that assessment's manifest; changed or
incomplete snapshots and other artifact directories are rejected.

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
| `profile` | Required | Reviewed local InSpec project with any dependencies already locked and vendored. |
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

`compliance_report` reads existing Cinc/InSpec JSON and OpenSCAP XCCDF or ARF XML.
Evidence import works with execution disabled and requires only a local file and
an active engagement workspace.

```json
{
  "report": "reports/audit.json",
  "expected_profile": "linux-baseline",
  "max_controls": 30
}
```

| Parameter | Default | Meaning |
|---|---|---|
| `report` | Required | Local report, up to 64 MiB. Relative paths stay workspace-confined; absolute paths are accepted. |
| `format` | `cinc` | Use `cinc` for Cinc/InSpec JSON or `openscap` for XCCDF/ARF XML. |
| `expected_profile` | — | Require the named Cinc profile or the full XCCDF profile ID. |
| `result_id` | — | Exact XCCDF TestResult ID, required when an OpenSCAP report contains multiple assessments. |
| `max_controls` | `30` | Returned controls or XCCDF rule results, 1–200. Saved normalization retains all entries. |

Successful imports preserve an exact source copy, its SHA-256, import time, and
normalized JSON under `artifacts/compliance/report-*/`. Cinc reports rejected by
the parser still return the evidence path and hash. Imported host labels
are not independently authenticated. Hashes identify bytes, not the producer.
The importer does not certify compliance, bind resources, approve exceptions,
or update the ledger. Its default approval scope is the exact report
call. `max_controls` and runtime deadlines do not enter saved rules.

### OpenSCAP evidence

Set `format` to `openscap` to import XCCDF 1.1/1.2 Benchmark or TestResult XML, or
XCCDF results embedded in ARF 1.1. The parser rejects DTDs, malformed reports,
missing completion times, empty results, duplicate TestResult IDs, and missing
or unsupported outcomes. It resolves neither XML entities nor network resources.

The summary distinguishes `pass`, `fail`, `error`, `unknown`, `notapplicable`,
`notchecked`, `notselected`, `informational`, and `fixed`. It includes the
selected result, profile, assessment time, reported targets, and a bounded list
of rules with errors and failures first. Repeated results for one rule are
retained as distinct instances. `evaluated_rule_result_count` counts pass and
fail results only. Reassess a `fixed` rule to verify its outcome.

## Assess with OpenSCAP

In **Workflows → Compliance**, select **SCAP datastream**. Enter the path to a
reviewed, self-contained SCAP source datastream on the controller, its SHA-256,
the full XCCDF profile ID, and the exact target distribution and release as
reported by Ansible. Select **Assess asset**. In chat, provide the same
information and the target. The model passes it to `compliance_scan` as
`profile` plus `openscap` settings. Use `oscap info <datastream>` on a system
with OpenSCAP to list profile IDs in that content.

OpenSCAP assessment needs Ansible on the controller and Python and `oscap` on
the target. The approved scan stages the reviewed XML over SSH in a private
directory, verifies the staged SHA-256, and scans that exact file. The
assessment preserves XCCDF and ARF XML, HTML and a per-host manifest.

### Provision a missing scanner

Ask in chat to install OpenSCAP on the selected target, or call `ansible` with
`playbook: "builtin:openscap_install"`, an explicit static inventory and host
limit, and `check: false`. This is a separate operation under the normal tool
approval policy. It uses privilege escalation and the target's existing
repositories. The playbook supports Debian, Ubuntu, Red Hat Enterprise Linux
and Fedora. It installs the release-appropriate scanner package when
`/usr/bin/oscap` is missing. Supply benchmark content separately. For offline
targets, provision OpenSCAP and its libraries using the target's approved
package repository or an offline package bundle.

### Assess multiple hosts with Ansible

For reviewed multi-host runs, use `builtin:openscap_assess` with a static
inventory, or declare `openscap_assess.yml` in an engagement Ansible profile.
Start with `openscap-vars.example.yaml`. Set the exact platform, full XCCDF
profile ID, absolute datastream path, and SHA-256. The path is on the target
by default. To upload a reviewed controller file, set
`openscap_controller_content: true`. Configure SSH host-key verification and
`become` in the reviewed inventory where applicable.

```json
{
  "playbook": "builtin:openscap_assess",
  "inventory": "hosts.yaml",
  "vars_files": ["openscap-vars.yaml"],
  "limit": "applications",
  "check": false,
  "timeout_seconds": 1800
}
```

The playbook checks the platform and hashes a private datastream copy before
invoking the scanner on that copy.
It requires `check=false` and runs without remediation or remote resource
fetching. It accepts OpenSCAP exit codes 0 and 2 as completed evaluations.
Missing reports, failed transfers, and other exit codes fail the Ansible run.
It saves the following under the returned `artifact_directory` for each host:

```text
openscap/<inventory-alias>/results.xml
openscap/<inventory-alias>/results-arf.xml
openscap/<inventory-alias>/report.html
openscap/<inventory-alias>/manifest.json
```

The manifest records the inventory alias and address, platform, content path
and digest, selected profile, scanner version, and exit code. Import
`results.xml` or `results-arf.xml` with `compliance_report` and
`format: "openscap"`. Check the Ansible recap for failed or unreachable hosts
before interpreting coverage. The playbook removes each host's temporary
directory on normal success or failure. An interrupted controller can leave
remote work or temporary files behind.

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
