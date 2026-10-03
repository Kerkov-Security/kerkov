# Skill format (`SKILL.md`)

A **skill** is a reusable playbook: a set of instructions Kerkov loads to carry out
a known procedure the same way every time. Each one is a `SKILL.md` file — YAML
frontmatter declaring it, followed by the instructions the model reads.

For using skills, see the [Skills guide](../guides/skills.md); for the tool that
loads them, [`skill_lookup`](tools/skill.md).

## Location

A skill lives at `skills/<name>/SKILL.md` in the project root. Each skill gets its
own directory, which is also where its helper scripts and data go — when the model
loads a skill, the load header names this directory so relative paths like
`scripts/foo.py` resolve against it.

## Minimal skill

```markdown
---
name: my-recon
description: Quick TCP enumeration of a single authorized host.
---

Given a target host, run a TCP service scan, identify listening services and
versions, and summarize the attack surface. Do not exploit anything.
```

`name` and `description` are the only required fields. Everything else is optional
metadata that improves search, gates eligibility, or controls who may invoke the
skill.

## Frontmatter

### Top-level fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | **required** | Unique skill name. What `/skill <name>` and `skill_lookup` load. |
| `description` | string | **required** | One line describing the skill. Shown in listings and searched. |
| `user-invocable` | boolean | `true` | Whether an operator can load it with `/skill`. |
| `disable-model-invocation` | boolean | `false` | When `true`, the model's `skill_lookup load` is refused — the skill is operator-only. |
| `metadata` | mapping | — | Optional structured metadata; see below. |

Note the hyphens: the frontmatter keys are `user-invocable` and
`disable-model-invocation`. Anything after the closing `---` is the skill body —
the instructions, read verbatim.

### `metadata.kerkov`

Security-domain metadata lives under `metadata.kerkov`. Every field is optional; all
are used for search facets, display, or eligibility.

```yaml
metadata:
  kerkov:
    category: lab/vulhub
    tactic: resource-development
    mitre: [T1587, T1190]
    difficulty: beginner
    platforms: [linux, macos, windows]
    tags: [vulhub, cve, docker, lab]
    products: [generic]
    vuln_classes: [generic]
    cves: [CVE-2022-26134]
    lab_sources: [vulhub]
    safe_verification: true
    requires:
      bins: [git, docker]
```

| Field | Type | Purpose |
|---|---|---|
| `category` | string | Grouping; a `skill_lookup search` facet. |
| `tactic` | string | Free-form tactic label. |
| `mitre` | list (or a bare string) | MITRE ATT&CK ids. A single string is accepted and wrapped into a list. |
| `difficulty` | string | Shown in listings; a search facet. |
| `platforms` | list | Where the skill applies. |
| `tags` | list | Keywords; a search facet. |
| `products`, `vuln_classes`, `cves`, `lab_sources` | list | Descriptive metadata. |
| `safe_verification` | boolean | Marks a skill whose verification step is non-destructive. |
| `requires` | mapping | Runtime requirements; see below. |

### `metadata.kerkov.requires`

Declares what a skill needs to run. It drives the eligibility check that `list` and
`load` perform.

| Field | Type | Effect |
|---|---|---|
| `bins` | list of strings | Executables that must be on `PATH`. A missing one marks the skill ineligible (`missing_bins`) — a warning on `load`, not a hard block, since the missing tool may not be on the path this run takes. |
| `elevated` | boolean | When `true`, the process must be running as root (POSIX) or Administrator (Windows), or `load` fails with `needs_elevation`. |
| `credentials` | list of strings | Environment/credential variables the skill needs. On `load`, a missing one is requested with masked input, or the load fails naming the `/auth set <VAR>` command that supplies it. |

Eligibility is checked at list and load time: `bins` are resolved with `which`,
elevation against the live process. See [`skill_lookup`](tools/skill.md#preconditions-on-load)
for exactly how each precondition surfaces.

## Parsing rules

- Frontmatter is the block between the opening `---` and the next `---`. A file
  with no valid frontmatter is skipped with a logged warning.
- A skill missing `name` or `description` is skipped.
- A file larger than 256 KB is skipped.
- Unknown top-level keys and unknown `metadata.kerkov` keys are ignored rather than
  rejected, so metadata can carry extra annotations without breaking the parser.

A malformed skill never stops the others from loading; it just does not appear.

## Example

The shipped `skills/` directory is the working reference — `vulhub-cve-lab`,
`htb-machine-solve`, `sqli-union`, and the rest each show a real skill's frontmatter
and body. Copy the closest one.

## Related

- [Skills guide](../guides/skills.md) — using and authoring skills, and the shipped
  set.
- [`skill_lookup`](tools/skill.md) — the tool that lists, searches, and loads them.
- [`/skill`](slash-commands.md#skills) — the operator command, which can load a
  skill even when model invocation is disabled.
