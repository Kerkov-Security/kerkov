---
name: example-ctf-challenge
description: Example CTF challenge — copy this file to harnesses/runs/<slug>/harness.md and fill it in
mode: task_list
target_class: ctf

scope:
  in_scope:
    - "http://localhost:8080/*"
  out_of_scope:
    - any host outside in_scope
    - DoS / resource exhaustion patterns

budget:
  max_iterations: 50
  max_wallclock_seconds: 14400  # 4h

allowed_tools: [shell, read, write, fetch, crypto]

forbidden_actions:
  - brute-force auth at >10 attempts/minute
  - writes outside ./artifacts/ or the run-dir memory files

rate_limits:
  fetch_per_minute: 30
  shell_commands_per_minute: 60

stuck_after: 8

tasks:
  - id: enumerate
    description: enumerate the challenge surface (endpoints, file types, ports, etc.)
    done: "file_exists:./artifacts/recon.md AND line_count:./artifacts/recon.md:>=1"
    passes: false
  - id: identify_vuln
    description: identify the vulnerability class to exploit
    done: "file_exists:./findings/vuln_class.md"
    passes: false
  - id: capture_flag
    description: extract the challenge flag
    done: "line_match:./artifacts/proof.txt:flag\\{.*\\}"
    passes: false
---

# CTF challenge harness

Replace this prose with challenge-specific context: target URL or binary, any
provided hints, allowed tools, prior knowledge.

The driver evaluates the `done` predicates above after every iteration. The run
terminates successfully when **all** of them evaluate true.

## Notes for the agent
- Append one line to `progress.txt` per iteration with what you learned.
- Drop PoCs/scripts/evidence under `artifacts/`.
- If you hit a dead end, append it to `dead_ends.md` so future iterations don't
  waste effort retrying.
- End every iteration with a single `harness_state` tool call.
