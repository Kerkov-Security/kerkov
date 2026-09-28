# `skill_lookup`

Finds and loads skills — the `SKILL.md` playbooks that carry out a known procedure
the same way every time. This is how the model reaches for a skill on its own,
without you typing [`/skill`](../slash-commands.md#skills).

For what skills are and how to write one, see the
[Skills guide](../../guides/skills.md).

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `action` | `list` \| `load` \| `search` | **required** | — |
| `skill_id` | string | — | Skill name. Required for `load`. |
| `query` | string | — | Keyword search, for `search`. |
| `tags` | array[string] | — | Filter by tags, for `search`. |
| `category` | string | — | Filter by category, for `search`. |

`search` requires at least one of `query`, `tags`, or `category`; a search with
none of them is rejected rather than silently returning everything.

## Actions

### `list`

Every available skill, one line each: name, description, declared difficulty, and
an eligibility note where the skill cannot currently run — `(missing: nmap)` or
`(needs elevated privileges)`. Metadata carries `count`.

### `search`

The same shape as `list`, narrowed by query, tags, or category.

### `load`

Returns the skill's full instructions, prefixed with a header naming the skill and
its directory:

```markdown
# Skill: vulhub-cve-lab
**Location:** `/home/kali/code/kerkov/skills/vulhub-cve-lab`
Resolve any relative paths in this skill (e.g. `scripts/foo.py`) against this
directory.
```

That header is load-bearing. Skills ship helper scripts alongside their
instructions, and without the location the model would resolve `scripts/foo.py`
against the engagement workspace, where it does not exist.

Metadata carries `skill_id` and `eligible`, plus a `credentials` map when the skill
declares required credentials.

## Preconditions on `load`

`load` can fail for four reasons that are worth distinguishing, because three of
them are fixable and say how:

| Condition | Result |
|---|---|
| The skill does not exist | Fails: `Skill '<id>' not found.` |
| The skill sets `disable_model_invocation` | Fails. The skill is operator-only; load it with `/skill`. |
| The skill needs elevated privileges and the process is not elevated | Fails, with both remedies named: restart under `sudo` (or an elevated terminal on Windows), or grant the underlying binaries the capabilities they need, e.g. `setcap` for `openvpn`. |
| The skill declares required credentials and one is missing | In an interactive session the credential is requested with masked input. If it cannot be obtained, the call fails naming the variable and the `/auth set <VAR>` command that supplies it. |

A missing *binary* is not a failure. The skill loads with a
`**Warning:** Missing required binaries:` line in the header, because the missing
tool may not be needed for the path this engagement takes — and if it is, the model
can install it or adapt.

## Approval

Auto-approved by the packaged rules:

```yaml
- allow: "skill_lookup({})"
```

Approval widens to **the whole tool**: every call reads packaged skill definitions
from disk. Nothing is executed by loading a skill — the instructions become
conversation text, and every tool call the model then makes is approved on its own
terms.

## Examples

```json
{ "action": "list" }
```

```json
{ "action": "search", "query": "sql injection" }
```

```json
{ "action": "search", "tags": ["web"], "category": "exploitation" }
```

```json
{ "action": "load", "skill_id": "vulhub-cve-lab" }
```

## Related

- [`/skill`](../slash-commands.md#skills) — the operator-facing half. `/skill <id>
  <args>` loads a skill with input, and works for skills that have model invocation
  disabled.
- [Skills guide](../../guides/skills.md) — the shipped skills and how to write one.
- [`/auth`](../slash-commands.md#auth) — storing the credentials a skill declares.
