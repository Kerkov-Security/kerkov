# `ansible`

Run reviewed Ansible playbooks against one or many hosts, and snapshot reusable
engagement automation profiles. This is an automation foundation: it does not
assess compliance or establish that an asset meets a security baseline.

Ansible is optional and requires no vendor subscription. Install `ansible-core`
on the controller; managed Linux hosts normally need SSH access and Python, not
an installation of Kerkov or Ansible. Use a Linux/macOS controller or WSL.

## Setup

From a checkout, retain the extras you normally use and add `automation`:

```bash
uv sync --locked --extra automation --extra dev --extra test --extra web
uv run kerkov setup ansible
```

For an installed package, use `pip install 'kerkov[automation]'`. An existing
`ansible-playbook` on PATH also works. `kerkov setup ansible` checks availability
and reports installation guidance; it does not install packages. Tool calls never
install software, fetch roles/collections, or update dependencies automatically.

Bundled examples live in `src/kerkov/config/ansible/` (also included in wheels):
`playbooks/connectivity.yml`, `playbooks/facts.yml`, and `profile.example.yaml`.
The example profile's resource ID, SSH user, and paths must match your engagement.

## Running a playbook

A static inventory may contain multiple hosts and groups:

```yaml
all:
  hosts:
    app_one:
      ansible_host: 192.0.2.10
      ansible_user: ubuntu
    app_two:
      ansible_host: 192.0.2.11
      ansible_user: ubuntu
  children:
    applications:
      hosts:
        app_one: {}
        app_two: {}
```

With a reviewed playbook and inventory in the active engagement workspace:

```json
{
  "action": "run",
  "playbook": "playbooks/connectivity.yml",
  "inventory": "hosts.yaml",
  "limit": "applications",
  "forks": 2,
  "check": true
}
```

Set `check=false` explicitly for normal execution. `check=true` is the default,
but playbooks can override check mode and execute tasks normally. It is a preview
request, not a read-only guarantee or a substitute for approval. `diff=true`
allows supported modules to print diffs in the private execution log.

Relative paths stay inside the active workspace, including through symlinks.
Absolute local paths are accepted for operator-reviewed playbooks, inventories,
and variable files. Inventory sources must have a YAML, JSON, or INI extension;
only Ansible's static YAML and INI inventory plugins are enabled. Dynamic
inventory plugins/scripts, arbitrary CLI flags, interactive credential prompts,
and automatic Galaxy dependency installation are outside this first version.

Use an SSH agent or credential-file references. SSH host-key verification stays
enabled, with batch authentication and connection persistence disabled. Configure
trusted known-host entries before running remote automation. Variables, including
any sensitive values, belong in reviewed local files supplied through
`vars_files`, not inline tool arguments. The model sees file paths, not their
contents, unless it explicitly reads those files. Password/Vault prompt flows are
not exposed by this tool.

## Engagement profiles

A profile records intended automation configuration using **existing ledger asset
or target IDs**. It does not create a second asset database or discover assets.
First record the relevant IP/domain resources through the inventory or `ledger`.

For example, after recording resources 21 and 22, create a template beside a
reviewed `playbooks/` directory:

```yaml
version: 1
name: linux_assessment
max_forks: 3
baseline: ubuntu2404_stig
exceptions: []
hosts:
  - resource_id: 21
    alias: app_one
    groups: [applications]
    user: ubuntu
  - resource_id: 22
    alias: app_two
    groups: [applications]
    user: ubuntu
playbooks:
  connectivity: playbooks/connectivity.yml
  facts: playbooks/facts.yml
```

`baseline` and `exceptions` are descriptive intent. This version does not apply
security controls, schedule maintenance windows, or interpret exceptions.
Playbook paths in a template resolve relative to that template, including when the
template is outside the workspace. Review those files before snapshotting.

Create, inspect, and use the snapshot:

```json
{"action": "snapshot", "template": "linux-assessment.yaml"}
```

```json
{"action": "profile", "profile": "linux_assessment"}
```

```json
{
  "action": "run",
  "profile": "linux_assessment",
  "playbook": "facts",
  "limit": "applications",
  "forks": 10
}
```

The last call uses at most three workers, as capped by the profile. Profile host
bindings also support `port`, `connection` (`ssh` or `local`), and an absolute
`private_key_file` reference. Local connections require a loopback resource.
Profiles support up to 200 host bindings and 30 named playbooks. One resource
may have separate bindings for distinct SSH ports, such as hosts behind NAT.

Snapshots live at `automation/profiles/<name>.json` in the root engagement
workspace. They persist on resume, are shared by the master and its agents, and
are isolated from other engagement workspaces. Snapshot creation is atomic and
refuses to overwrite an existing name. Template edits do not change an existing
snapshot. Create a new named snapshot when configuration changes.

At execution, the tool verifies that referenced resources still exist with the
same resolved host bindings and that the playbook entrypoint's SHA-256 matches.
It does **not** freeze or hash every imported role, collection, included file, or
external dependency. Keep those reviewed and versioned alongside the playbook.
Profile runs require the active ledger (`KB_ENABLED`); explicit static inventory
runs work without it. The profile is separate from an agent's role/tool profile.

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `action` | `run` | `run`, `snapshot`, or `profile`. |
| `playbook` | — | Local file, or declared playbook name with `profile`. |
| `inventory` | — | Explicit static inventory; mutually exclusive with `profile`. |
| `profile` | — | Engagement snapshot name for inspection or execution. |
| `template` | — | Profile template file for `snapshot`. |
| `limit` | `all` | Ansible host/group selection expression. |
| `tags`, `skip_tags` | `[]` | Task tag names to include/exclude. |
| `vars_files` | `[]` | Up to ten local extra-variable file references. |
| `check` | `true` | Request check mode; tasks can override it. |
| `diff` | `false` | Permit supported diffs in the private log. |
| `forks` | `5` | Maximum controller worker concurrency, 1–20, further capped by a profile. |
| `timeout_seconds` | `600` | Local controller runtime deadline, 1–3600 seconds. |
| `max_hosts` | `30` | Host recaps returned, 1–200; complete totals are retained. |

## Results, artifacts, and cancellation

Each run gets a private directory under `artifacts/ansible/` with its own
`ansible.cfg`, execution log, and `summary.json`. Profile runs also write a
resolved static inventory there. Paths are returned as metadata. Kerkov removes
managed provider credentials and inherited `ANSIBLE_*` overrides from the child
environment and selects its own callback/configuration.

The summary returns total host counts, task counters, bounded per-host recaps,
and failed/unreachable task names. Failed or unreachable hosts come first.
`no_log` task names are masked in the returned failure list. Module payloads,
facts, arbitrary debug output, and configuration diffs stay out of the model's
summary. Inspect the full log explicitly when you need those details; raw logs
can contain sensitive diagnostics even when the playbook uses `no_log`.

A nonzero exit, failed/unreachable hosts, timeout, invalid recap, or no matched
hosts is a failed tool call. Missing summaries are never interpreted as success.
Full counts remain available even when host details are truncated. A successful
playbook is an execution result, not evidence of compliance or a ledger update.

Cancellation and timeout terminate the local controller process tree, including
late-spawned children. They do not roll back completed changes or guarantee that
every remote process has stopped. Reassess affected hosts after interruption.

## Approval and trust

All actions respect `EXECUTION_ENABLED`. The tool is registered through the shared
ToolSuite for CLI and web sessions and is absent from built-in agent whitelists.
Default approval rules leave it unmatched; NORMAL and STRICT modes prompt.

The interactive approval scope offers no saved grant, because paths refer to
mutable executable input. Operators can still author explicit approval rules with
the usual consequences. Profiles do not grant permission to run playbooks.

Treat playbooks, inventories, variable files, plugins, roles, and collections as
trusted executable inputs. Host selection and profile bindings are not a sandbox:
playbooks can use controller-side actions, lookups, custom code, and delegation
outside the selected inventory. Keep approval separate from intended state.
