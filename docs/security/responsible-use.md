# Responsible use

Kerkov executes real commands and performs active security testing. It is built
for authorized penetration testing, security research, and CTFs — and it does
**not** grant permission to test anything. Authorization is yours to obtain and
yours to stay inside.

!!! danger "Authorized use only"
    Use Kerkov only on systems you own or have explicit **written** permission to
    test, and keep every action inside the agreed scope. You are responsible for
    the authorization, the scope, protecting collected data, and following
    applicable laws and engagement rules.

## Scope and written authorization

Before the first command, have written authorization that names the in-scope
hosts, domains, and URLs; the permitted techniques; and the window. Kerkov helps
you *hold* a scope — it does not define one. Record the scope you are authorized
for in the engagement's [assets and targets](../guides/engagements.md): an asset
is something you protect, a target is a subject of an offensive assessment. These
are bookkeeping that keeps the boundary in front of you and the model; they do
**not** themselves enforce it. Enforcement is approvals.

## Approvals are your control surface

Every tool call passes through the approval layer before it runs, on an axis
independent of how much the model narrates. This is the control that keeps an
agentic system inside scope, so it is worth setting deliberately rather than
leaving at the default. The four modes:

| Mode | Behavior |
|---|---|
| **Strict** | Deny rules apply; every other call asks first |
| **Normal** (default) | Allow and deny rules apply; unmatched calls ask first |
| **Permissive** | Deny rules apply; every other call is approved |
| **YOLO** | Bypasses all prompts and rules |

Rules live in `config/approvals.yaml` and your per-user
`~/.kerkov/approvals.yaml`, evaluated in order, first match wins. The shipped
allow list is deliberately short — read-only reads and status queries — and the
shipped deny list blocks the obvious foot-guns (`rm -rf`, `sudo`, `mkfs`,
`shred`). Use rules to make safe, repetitive work frictionless without globally
relaxing the policy. The [approvals guide](../guides/approvals.md) has the syntax
and precedence.

## The intrusive boundary

Some capabilities act on the target rather than merely reading about it. Keep
these behind an approval you actually read:

- **`exec`** runs commands against real systems. The deny rules catch a few
  destructive patterns, but they are a backstop, not a scope — most of what
  `exec` can do is only as safe as the command. The shipped rules have no
  `exec` allow rule, so in NORMAL mode an unmatched command prompts. Nothing
  else holds it back: a saved allow rule auto-approves the commands it matches,
  PERMISSIVE approves every command no deny rule catches, and a
  [delegated agent](../guides/agents.md#reviewing-an-agents-tool-calls) whose
  profile includes `exec` runs commands without a prompt. Deny rules are not
  evaluated for those agent calls.
- **`zap` active scans and spidering** send real, potentially disruptive traffic
  to a target. Only ZAP's read-only status and alert queries are auto-approved;
  a spider or active scan prompts in NORMAL mode. See the
  [`zap` reference](../reference/tools/zap.md).
- **`vulnerability_scan`, `sbom_generate`, and `sast_scan`** are left unmatched
  on purpose: they can pull images, update a vulnerability database, or consume
  heavy CPU, so NORMAL mode prompts before each.

- **`ansible`** can change controllers and remote hosts through trusted playbooks.
  Check mode and inventory selection are not permission boundaries. It remains
  unmatched in default approval rules.
- **`compliance_scan`** executes trusted Ruby profiles in Docker with remote SSH
  command access and optional sudo. It remains unmatched in default rules and
  excluded from built-in agent whitelists.

### Modes raise the posture, not the permission

[Offensive and CTF modes](../guides/modes-and-autonomy.md) change how the model
*approaches* a target — objective-driven exploitation, faster hypothesis testing
— but they do not widen approvals. A mode is methodology; approvals are the
gate. The two are set independently.

### YOLO and non-interactive runs remove the gate

`YOLO` approval (and `--yolo`) bypasses all prompts **and** all rules, and
requires `ALLOW_AUTONOMOUS=true`. Non-interactive runs (`--message … --no-welcome`)
auto-approve tool calls because there is no operator to ask. Both remove the
control described above. Use them only in an isolated environment, against a
tightly scoped target, with a prompt you have reviewed — never as a convenience
on a live engagement.

## Related

- [Data handling](data-handling.md) — where Kerkov stores what it collects, and
  what leaves the machine.
- [Approvals guide](../guides/approvals.md) — rule syntax and precedence.
- [Modes and autonomy](../guides/modes-and-autonomy.md) — posture versus gating.
