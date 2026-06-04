# Tool Approval System

Tool approval is governed by a pattern-based rules engine combined with an approval mode. Together they decide whether each tool call runs automatically, prompts the user, or is denied outright.

## Rule syntax

Rules live in YAML files under a `rules:` list. Each entry is either an `allow` or `deny` key whose value is a call-string:

```yaml
rules:
  - deny:  exec({'command': 're:.*rm .*'})
  - allow: exec({'command': 'ping *'})
  - allow: read({})
```

Call-string format: `tool_name({'param': 'pattern', ...})`. Empty braces `{}` match any call to that tool. Patterns are glob by default; prefix with `re:` for a regular expression. Only the listed fields need to match — extra parameters on the actual call are ignored.

## Rule priority

Rules are evaluated in this order (first match wins):

1. **Session rules** — calls the user approved interactively this run ("Approve and save as session allow rule")
2. **User rules** — loaded from `~/.kerkov/approvals.yaml`
3. **Project rules** — loaded from `config/approvals.yaml` in the repo
4. **Workspace rules** — loaded from `<workspace>/approvals.yaml`; lowest priority, so workspace allow rules cannot override project or user deny rules

Missing files are silently skipped.

## Approval modes

When no rule matches, the **approval mode** decides what happens:

| Mode | Set by autonomy preset | Behaviour on no-match |
|---|---|---|
| `STRICT` | GUIDED (level 1) | Always prompt, even when an allow rule matches |
| `NORMAL` | STANDARD (level 2) | Prompt the user |
| `PERMISSIVE` | AGGRESSIVE (level 3) | Auto-approve |
| `YOLO` | AUTONOMOUS (level 4) | Auto-approve; deny rules are also bypassed |

Deny rules block execution in every mode except `YOLO`.

## Evaluation flow

```
tool call arrives
│
├─ YOLO mode? → approve
│
├─ deny rule matched? → deny (in all other modes)
│
├─ STRICT mode? → prompt
│
├─ allow rule matched? → approve
│
├─ PERMISSIVE mode? → approve
│
└─ prompt user (NORMAL / no match)
```

## Rule files

### Project rules — `config/approvals.yaml`

Checked into the repo. Defines the default deny list for destructive commands. Always resolved relative to the package, not the working directory, so the rules load regardless of where kerkov is launched from.

### User rules — `~/.kerkov/approvals.yaml`

Personal overrides. Evaluated before project rules, so a user allow rule can whitelist something the project denies.

### Workspace rules — `<workspace>/approvals.yaml`

Written when the user picks "Approve and save as session allow rule" inside a workspace. Evaluated after project rules, ensuring project deny rules cannot be bypassed by a workspace allow rule.

## Interactive approval prompt

When a tool call reaches the prompt step, users see:

```
╭─ Tool Approval Required ──────────────────╮
│ Call: exec({'command': 'nmap -sV 10.0.0.1'}) │
│                                              │
│ [1] Approve                                  │
│ [2] Deny                                     │
│ [3] Approve and save as session allow rule   │
╰──────────────────────────────────────────────╯
```

- **[1] Approve** — run once
- **[2] Deny** — reject with an optional reason sent back to the model
- **[3] Approve and save** — add a session allow rule; if a workspace is active, also persist it to the workspace `approvals.yaml`

## Adding a new deny rule

To block a dangerous command pattern project-wide, add to `config/approvals.yaml`:

```yaml
rules:
  - deny: exec({'command': 're:.*dd if=.*of=/dev/.*'})
```

Deny rules match partially — the pattern only needs to cover the fields listed. Extra parameters on the actual call are not checked against unlisted fields.
