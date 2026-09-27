# Approvals

Every tool call passes through an approval gate before it runs. What the gate
does with a call depends on two things: the **approval mode**, and the
**rules** that match the call. This is how you make safe, repetitive work
frictionless without relaxing your whole policy.

Approval gating is independent of narration — see
[Modes and autonomy](modes-and-autonomy.md) for the verbosity axis.

## The four modes

| Mode | Allow rules | Deny rules | Unmatched calls |
|---|---|---|---|
| **strict** | ignored | applied | prompt |
| **normal** _(default)_ | applied | applied | prompt |
| **permissive** | applied | applied | auto-approved |
| **yolo** | — | — | auto-approved (no prompts, rules skipped) |

Set the mode with [`/approval`](../reference/slash-commands.md#modes-and-autonomy)
or `--approval`; `/yolo` and `--yolo` are shorthand for toggling `yolo`:

```text
kerkov › /approval permissive
```

`yolo` requires `ALLOW_AUTONOMOUS=true`. In `strict`, even a call your rules
would allow still prompts — it's the mode for maximum oversight.

## How a call is evaluated

1. Rules are checked **in order; the first match wins.**
2. A `deny` match rejects the call without prompting.
3. An `allow` match auto-approves it (ignored in `strict`).
4. No match falls back to the mode default: **prompt** (strict/normal) or
   **auto-approve** (permissive).

Deny rules are listed first in the packaged defaults so dangerous patterns are
caught before any allow rule can match them.

## Writing rules

Rules live in YAML. Packaged defaults load automatically; your overrides go in
`~/.kerkov/approvals.yaml` (`USER_APPROVALS_PATH`). Each rule is a single
`allow:` or `deny:` mapping whose value is a quoted pattern:

```yaml
rules:
  # deny first — dangerous exec patterns
  - deny:  "exec({'command': 're:.*rm.*-[rRf].*'})"
  - deny:  "exec({'command': 're:(^|[^a-z])sudo($|[^a-z])'})"

  # auto-approve low-risk, read-only calls
  - allow: "read({})"
  - allow: "http({'method': 'GET'})"
  - allow: "exec({'command': 'nmap *'})"
```

### Pattern syntax

- `"tool_name({'field': 'pattern', ...})"` matches a call to that tool whose
  named fields match.
- Values are **glob patterns** by default (`nmap *`, `http://127.0.0.1/*`).
- Prefix a value with `re:` to match it as a **regular expression** instead.
- **Omit a field** to match any value for it — the match is partial.
- An **empty dict** `{}` matches every call to that tool.
- Quote every rule string: the dict syntax contains `': '`.

Avoid backslash escapes in regex values; prefer `[[:word:]]`-style classes or
glob patterns.

### Approval breadth

When you approve a call interactively, Kerkov can offer to save it as a rule.
Each tool decides how broadly one approval may generalize — from "this exact
call only" to "any call to this tool" to a pattern drawn from a natural boundary
in the arguments (a directory, a host, an HTTP method). Values that vary per
call or would leak secrets into the rules file (a request body, headers, a
timeout) are never written into a saved rule.

## What's approved by default

The packaged defaults deny obviously destructive `exec` patterns (`rm -rf`,
`sudo`, `mkfs`, `shred`) and auto-approve read-only calls — `read`, the
read-only `process` actions (`list`, `poll`, `log`), read-only `zap` actions,
`skill_lookup`, `recall`, `ledger`, and agent report collection.

Some tools are **deliberately left unmatched** so they prompt in `normal` mode:
`sbom_generate` and `vulnerability_scan` (they may pull images, update
databases, and consume real resources) and `sast_scan` (host-local and
read-only, but CPU-intensive).

!!! tip "Relax narrowly, not globally"
    Instead of switching the whole session to `permissive`, add an `allow` rule
    for the specific tool and pattern that's slowing you down. You keep prompts
    everywhere else and deny rules keep protecting you.

## Managing the override file

Keep only intentional overrides in `~/.kerkov/approvals.yaml`. The installer
seeds it empty; copied defaults left in it will shadow future default-rule
updates, so remove anything you didn't mean to pin.
