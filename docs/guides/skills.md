# Skills

A **skill** is a reusable, parameterized playbook — a set of instructions
Kerkov can load to carry out a known procedure the same way every time.
Kerkov ships several, and you can add your own.

## Using skills

List, search, and load skills with
[`/skill`](../reference/slash-commands.md#skills):

```text
kerkov › /skill                       # list available skills
kerkov › /skill search sqli           # search by keyword
kerkov › /skill localhost-scan        # load one
kerkov › /skill vulhub-cve-lab CVE-2021-44228   # load with input
```

Loading a skill feeds its instructions into the conversation; anything after the
skill name is passed as its input. The model then follows the playbook, still
subject to your current mode and approval policy.

The model can also pull skills in on its own through the `skill_lookup` tool
when a task matches one — you don't have to invoke `/skill` by hand.

## Shipped skills

| Skill | What it does |
|---|---|
| `localhost-scan` | Enumerate services, ports, and attack surface on the current machine. |
| `vulhub-cve-lab` | Stand up a Vulhub lab from a CVE, verify the service is reachable, and prepare target details. |
| `vulhub-exploit-validator` | Prove a vulnerability safely on a booted Vulhub target, then run the matching test path and collect evidence. |
| `sqli-union` | Union-based SQL injection exploitation. |
| `r2-analyze` | Reverse-engineer a binary with radare2 — disassembly, decompilation, strings, xrefs. |
| `htb-machine-setup` | Set up a HackTheBox machine: connect VPN, spawn the box, verify reachability, report target details. |
| `htb-machine-solve` | Solve an HTB machine end to end: enumerate, foothold, capture `user.txt`, escalate, capture `root.txt`. |

## Writing your own

Skills are `SKILL.md` files with YAML frontmatter (a `name` and `description`)
followed by the instructions. Project skills live in the `skills/` directory;
the installer also seeds a copy under `~/.kerkov/` so your own skills survive
upgrades.

A minimal skill:

```markdown
---
name: my-recon
description: Quick TCP enumeration of a single authorized host
---

Given a target host, run a TCP service scan, identify listening services and
versions, and summarize the attack surface. Do not exploit anything.
```

Save it as `skills/my-recon/SKILL.md` and it appears in `/skill`.

!!! note "Skill format reference"
    [Skill format](../reference/skill-format.md) documents the full `SKILL.md`
    schema — every frontmatter key, the `metadata.kerkov` facets, and the
    `requires` preconditions. The shipped skills under `skills/` are working
    examples to copy from.
