# Tool Approval System

The tool approval system gates every LLM tool call before it executes. It decides whether to auto-approve, auto-deny, or ask the user — based on a rules engine and the active approval mode.

## Table of Contents

1. [Approval Modes](#approval-modes)
2. [Evaluation Flow](#evaluation-flow)
3. [Rules Engine](#rules-engine)
4. [Rule Files](#rule-files)
5. [Interactive Prompt](#interactive-prompt)
6. [Autonomy Presets](#autonomy-presets)
7. [Source Reference](#source-reference)

---

## Approval Modes

The active mode controls how the rules engine result is acted on. It is set via `APPROVAL_MODE` in `.env`, via the `/autonomy` command, or via the `--autonomy` CLI flag.

| Mode | Value | Behaviour |
|------|-------|-----------|
| **Strict** | `strict` | Deny rules apply and immediately reject. Allow rules are ignored — every other call prompts the user. |
| **Normal** | `normal` | Full rules evaluation. Allow rules auto-approve, deny rules auto-reject, unmatched calls prompt. |
| **Permissive** | `permissive` | Deny rules apply. Everything else (allow matches and unmatched calls) is auto-approved. |
| **YOLO** | `yolo` | All calls auto-approved. Deny rules are also ignored. Requires `ALLOW_AUTONOMOUS=true`. |

`normal` is the default.

---

## Evaluation Flow

For every tool call the engine runs through the following decision tree:

```
Non-interactive session?
  └─ YES → auto-approve

Active mode is YOLO?
  └─ YES → auto-approve

Evaluate rules (workspace → session → user → project; first match wins):
  └─ deny match → reject (all modes except YOLO)
  └─ allow match:
      └─ STRICT  → ignore allow, prompt user
      └─ other   → auto-approve

No rule matched:
  └─ PERMISSIVE → auto-approve
  └─ NORMAL     → prompt user
  └─ STRICT     → prompt user
```

---

## Rules Engine

Rules are stored in YAML files and loaded at startup. Each rule is a single `allow` or `deny` entry that matches against a tool name and an optional set of parameter patterns.

### Rule Syntax

```yaml
rules:
  - allow: "tool_name({'param': 'glob-pattern'})"
  - deny:  "tool_name({'param': 're:regex-pattern'})"
```

- **Tool name** — exact match against the tool's registered name (e.g. `exec`, `read`, `write`).
- **Parameters** — a Python dict literal. Only the listed keys need to match; extra parameters on the real call are ignored (partial match). An empty dict `{}` matches any call to that tool.
- **Glob patterns** — the default. Supports `*`, `?`, and `[seq]` via `fnmatch`.
- **Regex patterns** — prefix the value with `re:`. The pattern is a `re.search` (not a full match), so `re:.*sudo.*` matches anywhere in the value.

### Examples

```yaml
rules:
  # Block any exec call containing rm -r / rm -f
  - deny:  "exec({'command': 're:.*rm.*-[rRf].*'})"

  # Block sudo in exec calls
  - deny:  "exec({'command': 're:.*(^|[^a-z])sudo( |$).*'})"

  # Auto-approve all read calls
  - allow: "read({})"

  # Auto-approve GET requests only
  - allow: "http({'method': 'GET'})"

  # Auto-approve ping commands in any mode
  - allow: "exec({'command': 'ping *'})"
```

Rules are evaluated top-to-bottom; the first match wins. Put deny rules before broad allow rules to avoid accidental bypasses.

---

## Rule Files

Three YAML files contribute rules, loaded in priority order. Earlier files take precedence — the first matching rule across all tiers wins.

```
<workspace>/approvals.yaml        ← 1. workspace rules (highest priority)
~/.kerkov/approvals.yaml          ← 2. user rules
config/approvals.yaml             ← 3. project rules (repo defaults)
```

### Workspace rules — `<workspace>/approvals.yaml`

Written by the program when you choose **[3] Approve and save as session allow rule** at the interactive prompt. Rules accumulate here over the lifetime of an engagement and are reloaded automatically when you `--continue` or `/resume` that engagement.

Workspace rules are the highest-priority tier, so engagement-specific approvals override both your personal defaults and repo defaults. They are also the active session rules for the current run — there is no separate in-memory session tier when a workspace is active.

Workspace rule files live under `~/.kerkov/workspaces/<engagement-id>/approvals.yaml`.

### User rules — `~/.kerkov/approvals.yaml`

Personal overrides. **Never written by the program** — only you edit this file. Use it to permanently allow tools you trust or deny patterns your workflow never needs.

The path can be changed via `USER_APPROVALS_PATH` in `.env`.

### Project rules — `config/approvals.yaml`

Shipped with the repository. Sets safe defaults for the whole team: deny destructive exec patterns, auto-approve low-risk read-only tools. Edit this to change what counts as "normal safe behaviour" for everyone.

---

## Interactive Prompt

When a call reaches the user (no rule matched, or STRICT mode), the program shows:

```
╭─ Tool Approval Required ─────────────────────────────╮
│ Call: exec({'command': 'nmap -sV 10.0.0.1', 'mode': 'one_shot'})
│
│ [1] Approve
│ [2] Deny
│ [3] Approve and save as workspace allow rule   ← when a workspace is active
│     Approve and save as session allow rule     ← otherwise
╰──────────────────────────────────────────────────────╯
Choice:
```

| Choice | Effect |
|--------|--------|
| **1** | Approve this call once. |
| **2** | Deny. Optionally provide a reason; the reason is relayed to the LLM so it can adjust. |
| **3** | Approve now and save an allow rule to `<workspace>/approvals.yaml` (if a workspace is active) or hold it in memory for this session only (if no workspace is active). On resume the rule reloads from the workspace file. Parameter values are saved as exact literals — glob special characters (`*`, `?`, `[`, `]`) in values are escaped automatically. |

---

## Autonomy Presets

The `/autonomy` command (or `AUTONOMY_LEVEL` in `.env`) sets both verbosity and approval mode together as a preset. The approval mode can be overridden independently afterward with `APPROVAL_MODE`.

| Autonomy level | Approval mode |
|----------------|---------------|
| 1 — Guided | Strict |
| 2 — Standard | Normal |
| 3 — Aggressive | Permissive |
| 4 — Autonomous | YOLO (requires `ALLOW_AUTONOMOUS=true`) |

---

## Source Reference

| File | Purpose |
|------|---------|
| `src/cli/approval_rules.py` | `ApprovalRulesEngine`, `ApprovalRule`, rule parsing, glob/regex matching, `append_rule_to_file`, `from_files` factory |
| `src/cli/tool_approval_helper.py` | `ToolApprovalCliHelper` — bridges the engine to the CLI: runs evaluation, shows the prompt, persists workspace rules via `set_workspace()` |
| `src/cli/approval_mode_manager.py` | `ApprovalMode` enum and `ApprovalModeManager` |
| `src/cli/autonomy_manager.py` | Autonomy presets that couple approval mode to verbosity |
| `config/approvals.yaml` | Project default rules |
| `~/.kerkov/approvals.yaml` | User rules (not in repo) |
| `~/.kerkov/workspaces/<id>/approvals.yaml` | Per-engagement rules written at runtime |
