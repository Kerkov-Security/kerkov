---
name: htb-machine-solve
description: "Solve a HackTheBox machine end-to-end: enumerate, gain foothold, capture user.txt, escalate, capture root.txt, and submit both flags"
user-invocable: true
disable-model-invocation: false
metadata:
  kerkov:
    category: lab/htb
    tactic: initial-access
    mitre: [T1190, T1078, T1068]
    difficulty: intermediate
    platforms: [linux, macos, windows]
    tags: [htb, hackthebox, machine, exploitation, privesc, flag]
    products: [hackthebox]
    lab_sources: [hackthebox]
    safe_verification: true
    requires:
      bins: [nmap, curl, python3]
      credentials: [HTB_API_TOKEN]
---

# HackTheBox Machine Solve

Solve the requested HTB Labs machine through confirmed user and root ownership.
Use `htb-machine-setup` when the target is not ready. These instructions cover
regular Labs machines; Starting Point tasks, Challenges, and Academy have
separate submission flows.

## Completion and user control

- A recovered password, successful login, shell, or root identity is progress.
  Continue to read and submit the actual flag files; none of those alone means
  the machine is solved.
- Capture and submit each accessible flag immediately. Do not defer user.txt
  until after privilege escalation or stop at credentials when they provide a
  concrete next step. If the foothold is already root, retrieve both flags.
- Report `solved` only when HTB confirms both user and root ownership for this
  machine. Keep captured, submitted, and confirmed ownership separate.
- **Leave the machine and VPN running by default**, on success, partial results,
  submission errors, and a request to stop solving. Teardown is a separate user
  choice; follow [Optional cleanup](#optional-cleanup). Stopping work does not
  mean terminating the box.

## Inputs and preparation

Reuse the setup handoff and any existing sessions:

- `target_ip`, `machine_id`, and `os` (`Linux` or `Windows`).
- `vpn_session_id`, if known; preserve it for the final handoff.
- The user's cleanup preference, if already stated. Otherwise ask at the end.

Recover missing details with `active`, `find`, and `status` before rerunning
setup. Match the returned machine ID to the requested target. If setup is
needed, it must preserve any running instance unless the user authorized its
termination. Missing `os` or a failed ping is not a reason to respawn a box.

The bundled `scripts/htb_service.py` handles API calls. Resolve its **absolute
path** using the `skill_dir` printed in the skill load header; commands run from
the engagement workspace, not necessarily the skill directory. Replace
`<skill_dir>` and other placeholders before execution. Use `python3` and
`mode=one_shot` for the helper. The command and no-output timeouts below allow
its 60-second HTTP timeout to finish.

The helper reads the protected credential store. Configure `HTB_API_TOKEN` with
`/auth set HTB_API_TOKEN` and default persistence, not `--session`. Never put the
API token value in a command. An authentication problem blocks API submission,
not the validity of an already captured flag.

Before recon, check existing ownership:

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' status <machine_id>" mode=one_shot timeout=75 no_output_timeout=75
```

Output includes `machine_id`, `os`, `user_owned`, and `root_owned`. Ownership is
`true`, `false`, or `null` (unknown). Skip a flag only on explicit `true` for the
correct machine. Do not infer root ownership from user ownership or vice versa.
If both are true, report already solved and apply the user's cleanup choice;
otherwise continue with the missing flags. Retain each initial ownership value
so already-owned flags are not reported as newly submitted.

## Solving workflow

Drive the solve from evidence without asking for approval at each phase. Use
installed tools and `skill_lookup` for relevant techniques. Before unfamiliar
commands, read the relevant section of [Command reference](references/commands.md)
and the installed tool's help. The reference includes dated upstream sources;
avoid copying commands from stale writeups without checking their syntax.

Stay within the requested target IP and discovered hostnames mapped to that IP.
Do not follow redirects or pivot onto unrelated systems. Ground exploits in
observed versions and preconditions; prefer deterministic authenticated paths
over brute force and unstable kernel exploits.

### 1. Recon and enumeration

Discover open ports, service versions, routes, shares, exposed files, and
configuration hints. Rank promising paths using the results. If ICMP is filtered,
check the VPN log and a targeted TCP probe (`nmap -Pn`); ping failure alone does
not prove the target is down. Save useful evidence in the engagement workspace.

### 2. Turn credentials into access

For a recovered credential, identify its service and try the evidence-supported
login on this machine. Confirm the resulting identity and privileges. A web
login may expose a file-read path without giving a shell; use it to collect a
flag when possible. Do not submit passwords, NTLM hashes, or API tokens as flags.

Use managed sessions for interactive shells. For SSH, explicitly request a
remote PTY as well as Kerkov's local session:

```
exec command="ssh -tt <user>@<target_ip>" mode=session
```

Supply the recovered password at the prompt with `process action=paste
session_id=<id> text=<password> bracketed=false`, then `process action=submit`.
Check the host-key prompt against the intended lab target. Drive subsequent
commands through `paste` + `submit`, and read with `poll`/`log`. Reuse the shell
for `sudo -l`, `su`, and other interactive commands. A one-shot file read is fine
when it is sufficient; do not repeatedly reconnect for an interactive workflow.

### 3. Capture and submit user.txt

Read the flag from the current target instance:

- Linux: usually `/home/<user>/user.txt`.
- Windows: usually `C:\Users\<user>\Desktop\user.txt`.

Record the exact path and flag in the engagement evidence. Strip only surrounding
whitespace (including CR/LF); a regular machine flag is exactly 32 lowercase hex
characters. Do not extract an arbitrary 32-character substring from mixed output
or assume a similarly shaped credential is a flag. Re-read incomplete output.

Immediately run the submission procedure below, before continuing to privesc.
If submission is blocked, retain the flag and error, leave its submission state
unconfirmed, and continue useful authorized target work when possible.

### 4. Privilege escalation and root.txt

Use the foothold's observed permissions, local credentials, services, tasks,
and vulnerabilities to obtain root or Administrator/SYSTEM access. If already
privileged, skip escalation and read the flags directly.

- Linux: usually `/root/root.txt`.
- Windows: usually `C:\Users\Administrator\Desktop\root.txt`.

Submit root.txt immediately using the same procedure. Check for any missing
user flag before finishing. Do not pivot or install persistence after root.

## Submission and verification

Run this from the local engagement workspace, not inside the target shell:

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' submit <machine_id> <flag>" mode=one_shot timeout=75 no_output_timeout=75
exec command="python3 '<skill_dir>/scripts/htb_service.py' status <machine_id>" mode=one_shot timeout=75 no_output_timeout=75
```

The helper uses `POST /api/v5/machine/own`; HTB identifies user versus root from
the flag. Profile/status remains on v4. See the command reference for endpoint
sources and their limits.

`--difficulty` accepts **10, 20, ..., 100**, not 1-10. Preserve an explicit user
rating. Otherwise omit it initially; community clients differ on whether it is
required. If HTB explicitly rejects the missing rating, choose one based on the
solve so far, disclose it, and retry once with `--difficulty <rating>`. Do not
change the flag for a difficulty validation error or keep changing ratings to
work around other failures.

Interpret submission results, then verify the corresponding ownership field:

- **0**: the response explicitly indicates acceptance. Save HTB's message and
  confirm the matching `user_owned` or `root_owned` field with `status`.
- **8**: wrong flag or malformed input. Re-read the same flag path, confirm the
  machine ID and current instance, and retry only with a corrected value.
- **9**: HTB reports already owned/submitted. Verify the matching ownership field
  before treating it as complete; do not claim a new submission.
- **2/4/5**: authentication, rate limit, or blocked response. Surface the actual
  error (including `Retry-After`), retain the flag, and pause API requests. Do
  not bypass access controls or automatically retry a rate limit.
- **7 or an interrupted/timed-out call**: acceptance is unknown or the API failed.
  Inspect the error and query ownership before considering a retry. HTTP 200,
  empty JSON, or a successful process launch alone does not prove acceptance.

On an unconfirmed status result, make at most one additional read-only status
check if the API is available. If still false/unknown, report partial progress
with the saved flag, response, and next action. Do not blindly repost, fall back
to the retired v4 submission route, or tear down the machine to fix submission.
After root submission, confirm **both** ownership fields for the requested ID.

## Recovery and handoff

- When several distinct attempts yield no new evidence, summarize the attempts
  and remaining leads and ask whether to continue. A failed login with one
  credential is not a reason to stop when another concrete path remains.
- On a VPN drop or machine reboot, inspect the existing VPN session and use
  `wait-ip --timeout 180` once (`timeout=200 no_output_timeout=200`). Check that
  its returned `machine_id` still matches before using the IP. Restore the
  connection through setup if necessary, without implicit termination/reset.
- Preserve discovered credentials, captured flags, paths, and managed session
  IDs for resume. Report credentials as access findings, never as flag completion.

Return a result before asking about cleanup:

- `status`: `solved` only for both confirmed owns; `partial` for useful progress
  with outstanding flags/submissions; `failed` when no useful progress was made.
- `target_ip`, `machine_id`, and `phases_completed`.
- `user_flag_captured`, `root_flag_captured`: booleans for this run.
- `user_flag_submitted`, `root_flag_submitted`: booleans for new submissions
  verified by ownership this run, including a timed-out POST followed by a
  confirmed transition from unowned to owned. Already-owned flags remain false.
- `user_owned`, `root_owned`: confirmed booleans or `null` when unknown.
- `submission_results`: per-flag path, API message/error, and any outstanding action.
- `notable_findings`, saved evidence paths, and relevant session IDs.
- `cleanup`: user preference, machine/VPN state (`left_running`, `stopped`, or
  `unknown`), and any cleanup error. Do not claim a stop without verification.

## Optional cleanup

If the user has not already chosen, ask once **after** presenting the result:
“Keep the box and VPN running, terminate only the box, or terminate the box and
disconnect this solve's VPN?” Keep them running while awaiting the answer. No
answer, “stop solving,” a stall, or submission failure is not teardown consent.

Honor a choice already given without asking again. “Clean up after solving”
applies after both owns are confirmed, not after a partial/failed run. If the user
explicitly requests termination with flags outstanding, save the evidence and
report those outstanding submissions, then honor that request.

For authorized machine termination, use the requested machine ID and verify with
`active` that it is no longer active:

```
exec command="python3 '<skill_dir>/scripts/htb_service.py' terminate <machine_id>" mode=one_shot timeout=75 no_output_timeout=75
exec command="python3 '<skill_dir>/scripts/htb_service.py' active" mode=one_shot timeout=75 no_output_timeout=75
```

Disconnect the VPN only if the user chose it and the recorded session belongs to
this solve. A reused/shared tunnel needs an explicit choice covering it; an
unknown session ID is not a reason to kill all VPN processes.

```
process action=kill session_id=<vpn_session_id>
```

Check the process result, report the final resource state, and preserve all solve
evidence. Failure to terminate the box does not imply permission to reset it.
