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
upgrades. Unchanged shipped skills are refreshed on upgrade; edited skills are
preserved. See the [installation guide](../get-started/install.md) for the update
and legacy-file handling rules.

For Python companion scripts that import Kerkov or read its credential store,
use `<kerkov_python>` in the skill's command examples. Both `/skill` and
`skill_lookup` replace it with the shell-quoted Python command for the running
Kerkov installation. Keep that rendered command instead of substituting
`python3`: an isolated tool environment may not be the Python on the shell's
`PATH`. Resolve script paths against the skill directory printed when it loads.

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

## Installing external skills

Kerkov loads any skill that follows the `SKILL.md` layout, including those
published through [skills.sh](https://skills.sh/) and installed by its
`skills` CLI. Nothing Kerkov-specific is required: a skill with only `name`,
`description`, and a body works.

The CLI's `universal` target installs into a project's `.agents/skills/` and
the global `~/.config/agents/skills/`:

```bash
npx skills find pdf                                    # browse the directory
npx skills add vercel-labs/skills --agent universal     # install
```

Kerkov picks up `.agents/skills/` automatically when it runs from a source
checkout and that directory exists. Any other root — the global one, or a
project checkout you are not running Kerkov from — must be named explicitly:

```bash
SKILLS_EXTRA_DIRS=~/.config/agents/skills,/srv/myrepo/.agents/skills
```

Set it in your shell or in the `.env` of your Kerkov checkout. Kerkov deliberately
ignores it when it comes from a `.env` it merely found in the directory you
launched from, so starting an engagement inside a target's checkout can never add
that checkout's `SKILL.md` files to the catalog. Relative entries resolve against
the Kerkov checkout rather than the working directory.

No Node.js is needed for manual installation: copy the skill's directory,
`SKILL.md` and its `scripts/`, `references/`, and `assets/` together, into any
configured root. A skill directory may also be a symlink to one stored
elsewhere; companion paths resolve against the real directory.

Roots are scanned in this order, and the **first** one to define a name wins:

1. the resolved skills directory — a checkout's `skills/`, else
   `~/.kerkov/skills`, else the bundled copy;
2. `<checkout>/.agents/skills/`, when running from a source checkout;
3. each path in `SKILLS_EXTRA_DIRS`, in the order listed.

So an installed skill can never silently replace a shipped playbook. `/skill`
names the source of any skill that came from outside the main directory and
lists every shadowed file; `skill_lookup` annotates them the same way. A
missing or malformed root is logged and skipped — the other skills still load.

New and removed skills are picked up at **startup**; restart Kerkov after
installing one.

!!! warning "Review what you install"
    A skill is instructions for the model, and its `scripts/` are code you are
    inviting it to run. Read both before installing. Loading a skill never
    executes its scripts or installs its dependencies, and imported
    instructions earn no extra privileges: every tool call still goes through
    your mode and approval policy, and an `allowed-tools` field in the
    frontmatter is ignored rather than honored. A skill written for another
    agent may also name tools or hooks Kerkov does not have.

!!! note "Skill format reference"
    [Skill format](../reference/skill-format.md) documents the full `SKILL.md`
    schema — every frontmatter key, the `metadata.kerkov` facets, and the
    `requires` preconditions. The shipped skills under `skills/` are working
    examples to copy from.
