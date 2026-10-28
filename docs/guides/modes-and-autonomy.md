# Modes and Autonomy

Two independent controls shape how Kerkov behaves: the **mode** (what posture it
takes toward the target) and **autonomy** (how much it narrates and how often it
asks before acting). They are orthogonal — you can run passive recon fully
hands-off, or aggressive offensive work with confirmation on every step.

## Modes

A mode sets the assistant's methodology and boundaries through its system
prompt. Four are available:

| Mode | Best for | Posture |
|---|---|---|
| **recon** | Discovery and attack-surface mapping | Passive-first enumeration and analysis; no exploitation. |
| **audit** _(default)_ | Professional penetration tests | Methodical testing with controlled validation, evidence, and remediation guidance. |
| **offensive** | Red-team exercises | Objective-driven exploitation, chaining, and deeper attack-path analysis. |
| **ctf** | Competitions and training labs | Fast hypothesis testing and flag extraction; the flag is the only goal. |

Start in a mode with `--mode` (recon, audit, or offensive), or switch any time
with [`/mode`](../reference/slash-commands.md#modes-and-autonomy):

```bash
kerkov --mode recon
```

```text
kerkov › /mode offensive
```

**CTF is runtime-only.** There is no `--mode ctf`; select it in a session with
`/mode ctf`.

Mode is independent of your provider, model, and autonomy — switching mode keeps
all three.

## Autonomy: one preset over two axes

Autonomy is a convenience preset that sets **narration verbosity** and
**approval gating** together. The four levels run from most supervised to fully
hands-off:

| Level | Name | Verbosity | Approval | Behavior |
|---|---|---|---|---|
| 1 | guided | guided | strict | Detailed explanations; every tool call asks first. |
| 2 | standard _(default)_ | standard | normal | Brief explanations; unmatched calls prompt, deny rules enforced. |
| 3 | aggressive | aggressive | permissive | Minimal explanation; unmatched calls auto-approve, deny rules enforced. |
| 4 | autonomous | silent | yolo | No narration, no prompts. Requires `ALLOW_AUTONOMOUS=true`. |

Set the starting level with `AUTONOMY_LEVEL`, or change it live:

```text
kerkov › /autonomy 3
kerkov › /autonomy aggressive
```

There is **no `--autonomy` command-line flag** — set `AUTONOMY_LEVEL` in the
environment, or use `/autonomy` at runtime. Level 4 refuses to engage unless
`ALLOW_AUTONOMOUS=true`.

## Overriding one axis

The preset is a starting point, not a lock. Either axis can be pinned on its
own, which detaches it from the preset:

- **Verbosity** — how much the assistant narrates, and nothing else:

  | Level | What you see |
  |---|---|
  | `guided` | Per-tool intent before each call; interpretation after. |
  | `standard` | Batch-level intent before a sequence; a summary after. |
  | `aggressive` | Phase-level explanations only; no per-call narration. |
  | `silent` | No narration; tools just execute. |

  ```text
  kerkov › /verbosity silent
  ```

  Or at launch: `--verbosity silent`.

- **Approval gating** — when a call needs your say-so; covered in full under
  [Approvals](approvals.md). Set with `/approval` or `--approval`.

Pinning one axis leaves the other where the preset put it, so you can run, say,
detailed narration (`/verbosity guided`) with a relaxed approval policy
(`/approval permissive`) — a combination no single autonomy level produces.

## Choosing a combination

- **Learning a target, or a sensitive engagement:** audit + autonomy 1–2. You
  see the reasoning and approve each step.
- **Repetitive, well-scoped work:** keep verbosity low, relax approvals with
  rules rather than a blanket level — see [Approvals](approvals.md).
- **A disposable lab or CTF:** ctf + autonomy 3, or level 4 in a throwaway
  environment.

!!! warning "Level 4 and YOLO bypass every prompt"
    Autonomy 4 (and `--yolo` / `/yolo`) run every tool call without asking.
    Deny rules still apply under permissive, but YOLO ignores rules entirely.
    Use them only in isolated environments with a tightly scoped target.
