# Modes and Autonomy

Two independent controls shape how Kerkov behaves: the **mode** (what posture it
takes toward the target) and **autonomy** (how much it narrates and how often it
asks before acting). They are orthogonal — you can run defensive Blue work fully
hands-off, or aggressive Red work with confirmation on every step.

## Modes

A mode is a **security-team role** that sets the assistant's methodology and
boundaries through its system prompt. Three are available:

| Mode | Best for | Posture |
|---|---|---|
| **red** | Red-team exercises | Objective-driven exploitation, attack chaining, deeper attack-path analysis, and evasion; no remediation unless asked. |
| **purple** _(default)_ | Professional penetration tests | Find, confirm with controlled exploitation, and report with remediation guidance and evidence. |
| **blue** | Hardening and defense | Harden, assess compliance, manage vulnerabilities (SBOM/Grype/SAST, prioritized by KEV/EPSS), review detections, and fix-then-verify; no exploitation of targets. |

Start in a mode with `--mode` (red, purple, or blue), or switch any time with
[`/mode`](../reference/slash-commands.md#modes-and-autonomy):

```bash
kerkov --mode red
```

```text
kerkov › /mode blue
```

The pre-release mode names (`recon`, `audit`, `offensive`, `ctf`) are no longer
accepted: `--mode` and `/mode` take only `red`, `purple`, and `blue`, and a saved
engagement that recorded an old name fails to resume rather than being silently
remapped to a different posture. Reconnaissance and CTF are now
[workflows](web-console.md) that set their own mode and overlay rather than modes
of their own.

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
| 4 | autonomous | silent | yolo | No narration, no prompts. |

Set the starting level with `AUTONOMY_LEVEL`, or change it live:

```text
kerkov › /autonomy 3
kerkov › /autonomy aggressive
```

There is **no `--autonomy` command-line flag** — set `AUTONOMY_LEVEL` in the
environment, or use `/autonomy` at runtime. All four levels are available
without an additional opt-in setting.

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

- **Learning a target, or a sensitive engagement:** purple + autonomy 1–2. You
  see the reasoning and approve each step.
- **Hardening and remediation work:** blue + autonomy 1–2, so you review and
  approve each change before it is applied.
- **Repetitive, well-scoped work:** keep verbosity low, relax approvals with
  rules rather than a blanket level — see [Approvals](approvals.md).
- **A disposable lab or CTF:** red + autonomy 3, or level 4 in a throwaway
  environment.

!!! warning "Level 4 and YOLO bypass every prompt"
    Autonomy 4 (and `--yolo` / `/yolo`) run every tool call without asking.
    Deny rules still apply under permissive, but YOLO ignores rules entirely.
    Use them only in isolated environments with a tightly scoped target.
