# Process Management System

The process management system (`src/kerkov/core/process/`) provides PTY and subprocess adapters for spawning, monitoring, and interacting with shell processes. It enables the LLM to run long-lived interactive sessions (Python REPLs, `nmap` scans, `vim`, etc.) alongside simple one-shot commands.

The subsystem is **cross-platform**: it runs natively on POSIX (ptyprocess + signals) and Windows (ConPTY via pywinpty + taskkill/psutil). See [Platform Support](#platform-support) for the matrix and the host requirements.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Platform Support](#platform-support)
3. [Adapter Interface](#adapter-interface)
4. [Shell Wrapping](#shell-wrapping)
5. [Child Adapter](#child-adapter)
6. [PTY Adapter](#pty-adapter)
7. [Process Supervisor](#process-supervisor)
8. [Session Registry](#session-registry)
9. [Terminal Screen](#terminal-screen)
10. [Key Encoder](#key-encoder)
11. [ANSI Stripper](#ansi-stripper)
12. [Kill Tree](#kill-tree)
13. [Process Tool (Consumer)](#process-tool-consumer)
14. [End-to-End Walkthrough](#end-to-end-walkthrough)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    ProcessTool (LLM-facing)              │
│   actions: list, poll, log, write, paste, send_keys,    │
│            submit, kill                                  │
└────────────────┬────────────────────────────────────────┘
                 │ uses
    ┌────────────▼────────────┐
    │   ProcessSupervisor     │  ← spawns + manages lifecycle
    │   (singleton)           │
    └───┬─────────────┬───────┘
        │             │
        ▼             ▼
  ┌──────────┐  ┌──────────┐
  │PtyAdapter│  │ChildAdapt│   ← two interchangeable backends
  │(ptyproc) │  │(asyncio  │
  │          │  │ subproc) │
  └──────────┘  └──────────┘
        │             │
        ▼             ▼
  ┌──────────────────────────┐
  │  SessionRegistry         │  ← output buffering + state tracking
  │  (singleton)             │
  └──────────────────────────┘

Supporting utilities:
  - KeyEncoder      → ANSI key sequences for PTY input
  - strip_ansi()    → clean output before sending to LLM
  - TerminalScreen  → pyte screen emulation for interactive sessions
  - kill_process_tree() → graceful SIGTERM → SIGKILL shutdown
```

!!! info "Interactive sessions are terminal-emulated"
    `exec mode=session` over a PTY attaches a [`TerminalScreen`](#terminal-screen)
    to the session, so `poll` and `log` render an emulated display rather than the
    raw byte stream. One-shot and daemon runs keep the raw `strip_ansi` path. This
    is the single most surprising thing about the subsystem's output path; read
    that section before debugging anything that looks like mangled output.

The system has **three layers**:

| Layer | Module | Role |
|-------|--------|------|
| **Adapter** | `child_adapter.py`, `pty_adapter.py` (+ `posix_*`/`windows_*` backends) | Low-level process I/O — spawn, read, write, kill |
| **Supervisor** | `supervisor.py` | Lifecycle orchestration — timeouts, fallback, DSR handling |
| **Registry** | `session_registry.py` | Session state, output buffering, draining, cleanup |

The `ProcessTool` (in `src/kerkov/tools/process_tool.py`) is the LLM-facing consumer that ties all three together.

---

## Platform Support

The subsystem runs on both POSIX and Windows. Platform-specific concerns are isolated behind thin shims so the supervisor, registry, and tools stay platform-agnostic.

| Concern | POSIX | Windows |
|---------|-------|---------|
| PTY backend | `posix_pty_adapter.py` (`ptyprocess`) | `windows_pty_adapter.py` (`pywinpty` / ConPTY) |
| PTY shim | `pty_adapter.py` selects the backend by `sys.platform` and re-exports it as `PtyAdapter` / `PtyAdapterStdin` | same |
| Child backend | `child_adapter.py` with `start_new_session=True` | `child_adapter.py` with `creationflags=CREATE_NEW_PROCESS_GROUP` |
| Shell wrapping | `shell_wrap()` → `bash -c` | `shell_wrap()` → `cmd /c` (default) or PowerShell via `WINDOWS_DEFAULT_SHELL` |
| Tree kill | `posix_kill.py` — `SIGTERM` → grace → `SIGKILL` on the process group | `windows_kill.py` — graceful `terminate()`/`taskkill /T` → grace → `taskkill /F /T` (psutil sweep) |
| Termination intent | `KillMode.GRACEFUL` → `SIGTERM`; `KillMode.FORCE` → tree kill | `KillMode.GRACEFUL` → `CTRL_BREAK_EVENT`/`terminate`; `KillMode.FORCE` → tree kill |

The public adapter surface no longer leaks signal numbers: `ProcessAdapter.kill()` takes a platform-neutral `KillMode` (`GRACEFUL` / `FORCE`), and each backend maps it to the right primitive. Nothing above the adapters needs to `import signal`.

### Windows host requirements

- **Windows 10 1809+** (ConPTY). The `windows_pty_adapter` requires `pywinpty>=2.0`.
- **Windows Terminal or another VT-enabled host** for full xterm fidelity. ConPTY honors `ESC[?2004h/l` (bracketed paste) and the DSR cursor-position reply, so the supervisor's `strip_dsr_requests` / `build_cursor_position_response` and the `key_encoder` xterm modifier sequences (`ESC[1;5A` for Ctrl+Up, etc.) work unchanged. Classic `conhost.exe` is more limited — key encoding still emits the same bytes, but some sequences may not be interpreted.
- **`psutil`** is used for process-tree enumeration during a forced kill; if it is unavailable the backend falls back to `taskkill /F /T` alone.

`pywinpty` and `psutil` are gated by environment markers in `pyproject.toml` and install only on `win32`.

!!! warning "`ptyprocess` is not gated, though it is meant to be"
    `pyproject.toml` declares `ptyprocess>=0.7.0` **twice** — once unmarked and
    once with `sys_platform != "win32"` — so it installs on Windows too, where
    it is unusable. Nothing breaks, because `pty_adapter.py` selects the backend
    by `sys.platform` and never imports it there; the cost is an unnecessary
    Windows dependency. The fix is deleting the unmarked declaration, and it is
    a packaging defect rather than a documentation one.

---

## Adapter Interface

**File:** `adapter_interface.py`

Defines the abstract contracts that both adapters implement, plus shared data types.

### Key Types

#### `TerminationReason` (Enum)

Describes *why* a process ended:

| Value | Meaning |
|-------|---------|
| `MANUAL_CANCEL` | User or LLM explicitly cancelled |
| `OVERALL_TIMEOUT` | Hit the wall-clock timeout |
| `NO_OUTPUT_TIMEOUT` | Produced no output for the configured idle window |
| `SPAWN_ERROR` | Failed to start at all |
| `SIGNAL` | Killed by an OS signal (e.g., SIGSEGV) |
| `EXIT` | Normal exit (code 0 or nonzero) |

#### `WaitResult`

Returned by `adapter.wait()`. Exactly one of these fields is set:

```python
@dataclass
class WaitResult:
    code: Optional[int] = None     # exit code (0 = success)
    signal: Optional[int] = None   # signal number if killed
```

For example, a process that exits normally with code 0 returns `WaitResult(code=0, signal=None)`. One killed by SIGKILL returns `WaitResult(code=None, signal=9)`.

#### `RunExit`

A richer exit report returned by `ManagedRun.wait()`:

```python
@dataclass
class RunExit:
    reason: TerminationReason       # why it ended
    exit_code: Optional[int]        # OS exit code
    exit_signal: Optional[int]      # OS signal number
    duration_ms: float              # wall-clock runtime
    stdout: str                     # captured stdout
    stderr: str                     # captured stderr
    timed_out: bool                 # True if any timeout fired (overall or idle)
    no_output_timed_out: bool       # True specifically for idle/no-output timeout
```

#### `AdapterStdin` (ABC)

Abstract interface for writing to a process's stdin:

```python
class AdapterStdin(ABC):
    def write(self, data: str) -> None: ...
    def close(self) -> None: ...
```

Both adapters implement this — `ChildAdapterStdin` wraps an `asyncio.StreamWriter`, while `PtyAdapterStdin` wraps `PtyProcess.write()`.

#### `ProcessAdapter` (ABC)

The main adapter contract:

```python
class ProcessAdapter(ABC):
    pid: Optional[int]               # process ID
    stdin: Optional[AdapterStdin]    # stdin handle

    def on_stdout(self, callback) -> None   # register stdout listener
    def on_stderr(self, callback) -> None   # register stderr listener
    async def wait(self) -> WaitResult      # block until exit
    def kill(self, mode: KillMode) -> None  # GRACEFUL or FORCE (platform-neutral)
    def dispose(self) -> None               # cleanup resources
```

`KillMode` (`GRACEFUL` / `FORCE`) keeps OS signal numbers off the public surface. `FORCE` (the default, used by the supervisor's cancel path) tears down the whole process tree; `GRACEFUL` asks the process to stop. Each backend maps these to the right primitive per platform.

#### `ManagedRun`

The handle the supervisor returns after spawning:

```python
@dataclass
class ManagedRun:
    run_id: str                                       # unique session ID
    pid: Optional[int]                                # OS process ID
    started_at_ms: float                              # epoch millis
    stdio_mode: str                                   # "pty" or "pipe", as resolved
    stdin: Optional[AdapterStdin]                     # write handle
    wait: Callable[[], Awaitable[RunExit]]            # await this for exit
    cancel: Callable[[Optional[TerminationReason]], None]  # kill the run
```

`stdio_mode` records what the spawn *resolved to*, not what the caller asked
for. A `mode="pty"` request that fell back to the child adapter comes back as
`"pipe"`, which is how callers learn that terminal features are not available on
this run without having to infer it from behaviour.

??? note "Background: what is a PTY, and why are there two adapters?"

    A **pseudo-terminal (PTY)** is a pair of virtual devices — a "master" and a
    "slave" — that emulate a hardware terminal. A program run inside one believes
    it is connected to a real terminal:

    - It receives a `TERM` environment variable and terminal dimensions (rows/cols).
    - Interactive programs enable line editing, colour output and cursor movement.
    - stdout and stderr are **merged** into a single stream, just as on a real terminal.

    Without a PTY, programs detect the pipe and may disable colour, buffer output
    in large blocks instead of line by line, and skip interactive features — no
    readline, no progress bars.

    That difference is the whole reason for two adapters. The child adapter uses
    plain pipes and keeps stdout and stderr apart, which is what you want for a
    one-shot command whose streams you intend to read separately. The PTY adapter
    allocates a real pseudo-terminal, which is what an interactive session needs.
    The supervisor prefers the PTY and falls back to the child adapter if the PTY
    spawn fails.

---

## Shell Wrapping

**File:** `shell_wrap.py`

Both adapters take a single command *string* and have to hand it to a shell.
That wrapping is the one place where the two platforms genuinely cannot share a
code path, so it lives in its own module and every other module stays
platform-agnostic.

`shell_wrap(command, shell=None)` returns an argv list:

| Platform | Result |
|---|---|
| POSIX | `["bash", "-c", command]` |
| Windows | `["cmd", "/c", command]`, or the PowerShell/pwsh equivalent |

The shell is selectable on Windows through `WINDOWS_DEFAULT_SHELL` (`cmd`,
`powershell`, `pwsh`; `cmd` is the default) or an explicit `shell=` argument.
`resolve_shell()` falls back to `cmd` for any unrecognised name rather than
failing the spawn.

### Why Windows needs a second function

An argv list is the wrong shape for Windows. Both `subprocess.list2cmdline` and
pywinpty backslash-escape embedded quotes, and `cmd` then chokes on the result —
so a command containing quotes that runs fine on POSIX would fail on Windows for
reasons that have nothing to do with the command.

`windows_pty_spec(command, shell=None)` therefore returns a
`(appname, cmdline)` pair for ConPTY's low-level `PTY.spawn`, where `cmdline` is
the **verbatim** argument tail with no escaping applied:

- For `cmd`, that is `COMSPEC` plus `' /d /s /c "<command>"'`.
- For PowerShell and pwsh, the command is encoded UTF-16LE and passed through
  `-EncodedCommand`, so quotes and spaces survive the Windows command-line
  boundary intact.

The child adapter reaches the same outcome on Windows by using
`create_subprocess_shell` rather than `create_subprocess_exec`. Both paths
preserve quoting the way a user typing at a prompt would expect.

---

## Child Adapter

**File:** `child_adapter.py`

A fallback adapter that uses `asyncio.create_subprocess_exec` with piped stdio. Good for non-interactive commands where you don't need terminal emulation.

### How It Works

```
          ┌──────────────────┐
          │  asyncio Process  │
 stdin ──▶│  bash -c "cmd"   │──▶ stdout (pipe)
          │                  │──▶ stderr (pipe)
          └──────────────────┘
            start_new_session=True (detached process group)
```

### Factory: `ChildAdapter.create()`

```python
adapter = await ChildAdapter.create(
    command="nmap -sV 10.0.0.1",
    cwd="/tmp",
    env={"PATH": "/usr/bin"},
    stdin_mode="pipe-closed",   # or "pipe-open"
    input_data="some input\n",  # optional initial stdin data
    shell=None,                 # override the platform default shell
)
```

**Parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `command` | required | Shell command string, wrapped by [`shell_wrap()`](#shell-wrapping) for the platform |
| `cwd` | `None` | Working directory |
| `env` | `None` | Environment variables dict |
| `stdin_mode` | `"pipe-closed"` | `"pipe-closed"` closes stdin after optional `input_data`; `"pipe-open"` leaves it open for writing |
| `input_data` | `None` | Data to write to stdin before potentially closing it |
| `shell` | `None` | Shell override (`bash` on POSIX; `cmd` / `powershell` / `pwsh` on Windows) |

### Output Reading

Stdout and stderr are read in separate `asyncio.Task`s that call your callback with decoded UTF-8 chunks (4096-byte reads):

```python
adapter.on_stdout(lambda chunk: print("OUT:", chunk))
adapter.on_stderr(lambda chunk: print("ERR:", chunk))
```

### Exit Handling

```python
result = await adapter.wait()
# result.code = 0 (normal exit)
# result.code = None, result.signal = 9 (killed by SIGKILL)
```

Negative return codes from `asyncio.subprocess` indicate signal kills — the adapter translates `returncode = -9` to `WaitResult(code=None, signal=9)`.

`wait()` does not return the moment the process exits: it then gives the reader
tasks up to `READER_DRAIN_TIMEOUT_S` (0.25 s) to finish draining the pipes.
Process exit and pipe EOF are not the same event, and without that bounded wait
a short command's output routinely came back empty — the process was gone before
the readers had been scheduled. The wait is bounded rather than unconditional
because a backgrounded grandchild can hold the write ends open indefinitely, in
which case the readers would never see EOF at all. The PTY adapter does the same
thing with its reader thread, joining it for the same 0.25 s.

### Kill Behavior

- `KillMode.FORCE` → uses `kill_process_tree()` for full tree termination
- `KillMode.GRACEFUL` → `proc.send_signal(...)` (SIGTERM on POSIX, CTRL_BREAK_EVENT on Windows; direct to process only)

### Why detach the child?

The child is spawned detached so `kill_process_tree()` can reach the whole tree (parent + children), not just the direct process. On POSIX this is `start_new_session=True` (a new process group reachable via `os.killpg()`); on Windows it is `creationflags=CREATE_NEW_PROCESS_GROUP` (so `CTRL_BREAK_EVENT` and `taskkill /T` reach the tree). `shell_wrap()` supplies the platform's argv (`bash -c` vs `cmd /c`).

---

## PTY Adapter

**File:** `pty_adapter.py`

The primary adapter. Uses the `ptyprocess` library to spawn commands in a real pseudo-terminal.

### How It Works

```
          ┌──────────────────────┐
          │  PtyProcess          │
 stdin ──▶│  bash -c "cmd"      │──▶ unified output (PTY fd)
          │  (pseudo-terminal)   │    (stdout + stderr merged)
          └──────────────────────┘
            dimensions: 120x30
```

### Factory: `PtyAdapter.create()`

```python
adapter = await PtyAdapter.create(
    command="python3 -i",
    cwd="/home/user",
    env=None,        # inherit the parent environment when omitted
    cols=120,
    rows=30,
    shell=None,      # override the platform default shell
)
```

`command` is wrapped by [`shell_wrap()`](#shell-wrapping) exactly as it is for
the child adapter. Since `PtyProcess.spawn()` is blocking, it runs in a thread
executor so the event loop isn't blocked.

### Key Differences from Child Adapter

| Aspect | ChildAdapter | PtyAdapter |
|--------|-------------|------------|
| Output streams | Separate stdout/stderr | Merged single stream |
| `on_stderr()` | Active (reads stderr pipe) | No-op |
| Reader | `asyncio.Task` | Daemon `threading.Thread` |
| Terminal features | None (piped) | Full (colors, readline, etc.) |
| Stdin close | `writer.close()` | Sends `\x04` (Ctrl-D / EOF) |
| Dependency | stdlib only | `ptyprocess` library |

### The Reader Thread

PTY reads are **blocking** (`PtyProcess.read()` doesn't support asyncio), so a daemon thread handles reading:

```python
def _reader():
    while not self._reader_stop.is_set():
        data = self._pty.read(4096)  # blocks until data available
        if not data:
            break
        callback(text)
```

The `_reader_stop` threading event allows clean shutdown via `dispose()`.

### Exit Watcher

A separate daemon thread waits for the PTY process to exit and translates the result:

```python
def _watcher():
    self._pty.wait()                    # blocks until exit
    sig = self._pty.signalstatus        # None or signal number
    code = self._pty.exitstatus         # None or exit code
    self._settle_wait(WaitResult(...))  # thread-safe notify
```

The `_settle_wait()` method is thread-safe — it uses a `threading.Lock` to set the result and `loop.call_soon_threadsafe()` to notify the asyncio event.

### Force-Kill Fallback

Some PTY hosts fail to emit an exit event after `SIGKILL`. The adapter handles this with a timer:

```python
def _schedule_force_kill_fallback(self):
    # If PTY doesn't report exit within 4 seconds after SIGKILL,
    # settle the wait manually
    self._force_kill_timer = threading.Timer(
        4.0,  # FORCE_KILL_WAIT_FALLBACK_S
        lambda: self._settle_wait(WaitResult(code=None, signal=SIGKILL)),
    )
```

---

## Process Supervisor

**File:** `supervisor.py`

The central orchestrator. Picks the adapter, manages timeouts, handles DSR requests, and returns `ManagedRun` handles.

### Singleton Access

```python
from kerkov.core.process import get_process_supervisor

supervisor = get_process_supervisor()  # thread-safe singleton
```

### Spawning a Process

```python
run = await supervisor.spawn(
    command="nmap -sC -sV 10.0.0.1",
    mode="pty",                  # or "child"
    timeout_ms=300_000,          # 5-minute wall-clock limit
    no_output_timeout_ms=60_000, # kill if idle for 60s; pass None to disable
    on_stdout=lambda chunk: print(chunk),
    on_stderr=lambda chunk: print(chunk),
)

# run.run_id   → "a1b2c3d4"
# run.pid      → 12345
# run.stdin    → AdapterStdin (write to process)
# run.wait()   → coroutine returning RunExit
# run.cancel() → kill the process
```

### Adapter Selection with Fallback

```python
if mode == "pty":
    try:
        adapter = await _create_adapter_cancellation_safe(
            PtyAdapter.create(command, cwd=cwd, env=env)
        )
    except Exception as e:
        logger.warning("PTY spawn failed (%s); retrying without PTY for '%s'", e, command)
        using_pty = False

if adapter is None:
    adapter = await _create_adapter_cancellation_safe(
        ChildAdapter.create(command, cwd=cwd, env=env, stdin_mode="pipe-open")
    )
```

The supervisor falls back to the child adapter if PTY spawn fails — in a
container with no `/dev/ptmx`, say, or when the host is out of pseudo-terminals.
This makes PTY a best-effort upgrade. A *missing* `ptyprocess` is not one of
these cases: `pty_adapter.py` imports its backend at module import time, so an
absent library is an `ImportError` at startup rather than a fallback at spawn. The fallback is **logged at warning level**,
not silent: a run that quietly lost its terminal features would otherwise be
diagnosed by its symptoms. The resolved choice is also reported to the caller as
`ManagedRun.stdio_mode`.

### Cancellation-Safe Spawn

Both `create()` calls go through `_create_adapter_cancellation_safe()`, and the
reason is a race that leaks live processes:

```python
async def _create_adapter_cancellation_safe(creation):
    creation_task = asyncio.ensure_future(creation)
    try:
        return await asyncio.shield(creation_task)
    except asyncio.CancelledError:
        creation_task.add_done_callback(_cleanup_adapter_after_cancelled_spawn)
        raise
```

Spawning is not instantaneous — `PtyProcess.spawn()` runs in a thread executor,
and the child adapter awaits the asyncio subprocess creation. If the caller is
cancelled inside that window, cancelling the creation coroutine would abandon a
process that is *already starting*: the OS creates it anyway, and nothing is left
holding a handle to kill it.

So the creation is **shielded** and allowed to finish, and the cancellation is
re-raised immediately so the caller is not delayed by it. A done-callback then
takes the adapter that eventually materialises and issues a `KillMode.FORCE`
kill followed by `dispose()`. This is why a cancelled `exec` does not leave a
live process behind.

### Timeouts

The supervisor manages two independent timers, each optional:

```
Time ──────────────────────────────────────────────▶

Overall timeout (wall clock):
├──────────────────────────────── timeout_ms ──────┤
                                                   KILL

No-output timeout (idle):
├── output ──── idle ────── output ── idle ─────┤
                 ↑ reset                ↑ reset
                 └── no_output_timeout_ms ──────┤
                                                 KILL (if exceeded)
```

- **Overall timeout** (`timeout_ms`): hard wall-clock limit. Once reached,
  the process is killed regardless of activity.
- **No-output timeout** (`no_output_timeout_ms`): resets every time the
  process produces output. Kills the process if it goes silent for too
  long. Useful as a fail-fast guard for stuck network calls and hung
  subprocesses in `one_shot` mode.

Both timers use `loop.call_later()` for non-blocking scheduling. The
idle timer is reset via `touch_output()`, which is thread-safe (uses
`loop.call_soon_threadsafe()`) since PTY output arrives from a
background thread.

Pass `None` (or omit) for either limit to disable it. Long-lived runs
that go quiet by design — daemons like openvpn after the TLS handshake,
interactive sessions waiting for the user — leave both off. `ExecTool`
encodes this policy: `mode=one_shot` enables both (defaults: 120s overall,
60s idle); `mode=session` and `mode=daemon` disable both.

### DSR (Device Status Report) Handling

Some terminal programs send DSR requests (`\x1b[6n` or `\x1b[?6n`) to query cursor position. Without a response, they hang.

The supervisor intercepts these in the stdout callback:

```python
def on_supervisor_stdout(chunk: str):
    if using_pty:
        cleaned, dsr_count = strip_dsr_requests(chunk)
        # Auto-respond with cursor position report
        if dsr_count > 0:
            for _ in range(dsr_count):
                managed_run.stdin.write("\x1b[1;1R")  # row=1, col=1
        on_stdout(cleaned)  # forward without DSR sequences
```

This prevents the LLM from seeing raw DSR escape sequences and prevents programs from hanging.

### The `wait_for_exit()` Coroutine

Inside `spawn()`, the supervisor builds a `wait_for_exit` closure that:

1. Awaits `adapter.wait()` for the process to exit
2. Clears all timers
3. Calls `adapter.dispose()` to clean up resources
4. Removes the run from the active dictionary
5. Determines the `TerminationReason` (forced reason > signal > normal exit)
6. Returns a `RunExit` with full output and metadata

### Cancellation

```python
# Via ManagedRun handle
run.cancel()  # defaults to MANUAL_CANCEL
run.cancel(TerminationReason.OVERALL_TIMEOUT)

# Via supervisor by ID
supervisor.cancel("a1b2c3d4")
```

Cancellation sets the forced reason and issues a `KillMode.FORCE` kill (full process-tree teardown).

The supervisor keeps its active runs in a dict keyed by `run_id`, and exposes
two more methods over it:

| Method | Purpose |
|---|---|
| `cancel_all(reason=MANUAL_CANCEL)` | Cancel every run this supervisor owns. |
| `get_active(run_id)` | The `ManagedRun` for an id, or `None` if it has settled. |

`cancel_all()` is the one every teardown path calls — a web socket closing, a
session ending, the CLI quitting. Each `WebKerkovSession` owns its own
`ProcessSupervisor`, so "every run this supervisor owns" is exactly that
socket's processes and no one else's. `cancel()` on an unknown `run_id` logs a
warning and returns rather than raising, since a run that has already exited has
already removed itself from the dict.

---

## Session Registry

**File:** `session_registry.py`

Thread-safe registry that tracks running and finished process sessions with sophisticated output buffering.

### Singleton Access

```python
from kerkov.core.process import get_session_registry

registry = get_session_registry()  # thread-safe singleton
```

### Session Lifecycle

```
                 add_session()
   Created ──────────────────▶ Running
                                 │
                     mark_backgrounded()
                                 │
                                 ▼
                            Backgrounded ──────────────▶ Finished
                           (pollable by LLM)       mark_exited()
                                                        │
                                                        ▼
                                                  _prune_finished()
                                                  (after 30 min TTL)
```

### Output Buffering

Each session maintains two buffer systems:

#### 1. Pending Buffers (for polling)

New output chunks are appended to `pending_stdout` / `pending_stderr`. When the LLM polls, these are **drained** (returned and cleared). This gives the LLM only the *new* output since the last poll.

```python
# Append output (called from PTY reader thread)
registry.append_output(session, "stdout", "scan results...\n")

# Drain (called when LLM polls)
stdout, stderr = registry.drain_session(session)
# stdout = "scan results...\n"
# Next drain returns "" until more output arrives
```

#### 2. Aggregated Buffer (for full history)

All output is concatenated into `session.aggregated`. This provides full history for the `log` action.

#### Buffer Caps

| Buffer | Default Cap | Purpose |
|--------|------------|---------|
| `pending_stdout/stderr` | 30,000 chars | Prevent unbounded growth between polls |
| `aggregated` | 200,000 chars | Full history cap |
| `tail` | 2,000 chars | Last N chars for quick display |

When a buffer exceeds its cap, the **oldest** chunks are dropped (keeping the most recent output). The `truncated` flag is set to `True` so the LLM knows some output was lost.

The effective pending cap is `min(pending_max_output_chars, max_output_chars)`,
not `pending_max_output_chars` alone. Both are per-session and settable, and a
caller that lowered the aggregate cap below the pending one would otherwise buffer
more between polls than the session is allowed to keep in total.

```python
# Buffer capping behavior:
# Before: [chunk1, chunk2, chunk3, chunk4]  → 35,000 chars
# After:  [chunk3, chunk4]                  → 28,000 chars (under 30,000 cap)
# session.truncated = True
```

### Sweeper (Automatic Cleanup)

A daemon `threading.Timer` runs periodically to:
1. **Reap stale running sessions** — sessions that have exited but weren't properly marked
2. **Prune old finished sessions** — delete entries older than the TTL (default: 30 minutes)

The sweeper interval is `max(30s, ttl/6)`, so with the default 30-minute TTL it runs every 5 minutes.

It is not a permanent timer. Each pass reschedules itself only if the registry
still holds a running or finished session, so a registry that empties out stops
waking the process up. The next `add_session()` starts it again.

### `ProcessSession` Fields

```python
@dataclass
class ProcessSession:
    id: str                            # 8-char hex slug (e.g., "a1b2c3d4")
    command: str                       # the shell command
    started_at: float                  # time.time()
    cwd: Optional[str]                 # working directory
    pid: Optional[int]                 # OS process ID
    max_output_chars: int = 200_000    # aggregated buffer cap
    pending_max_output_chars: int = 30_000  # pending buffer cap

    # Output state
    pending_stdout: list[str]          # chunks since last drain
    pending_stderr: list[str]
    pending_stdout_chars: int          # running length, so capping is O(1)
    pending_stderr_chars: int
    total_output_chars: int            # everything ever produced, pre-capping
    aggregated: str                    # full concatenated history
    tail: str                          # last 2000 chars
    truncated: bool                    # True if any buffer was capped

    # Lifecycle
    exited: bool                       # True after process exits
    exit_code: Optional[int]
    exit_signal: Optional[int]
    backgrounded: bool                 # True if handed off for polling

    stdin: Optional[AdapterStdin]      # write handle
    screen: Optional[TerminalScreen]   # set for interactive PTY sessions
```

`total_output_chars` counts what the process produced, not what survived
capping, so it is what tells you whether a session's history is complete.

`screen` is the field that carries [terminal emulation](#terminal-screen). When
it is set, `poll` and `log` render from the emulated display and the raw
buffers are effectively a second copy. `append_output()` feeds it *outside* the
registry lock, since the emulator has a lock of its own.

### `FinishedSession` Fields

A snapshot created when `mark_exited()` is called on a backgrounded session:

```python
@dataclass
class FinishedSession:
    id: str
    command: str
    started_at: float
    ended_at: float
    cwd: Optional[str]  # carried over from the running session
    status: str         # "completed" | "failed" | "killed"
    exit_code: Optional[int]
    exit_signal: Optional[int]
    aggregated: str     # full output history
    tail: str           # last 2000 chars
    truncated: bool
    total_output_chars: int
```

The snapshot deliberately drops `stdin`, `screen` and the pending buffers: the
process is gone, so a write handle would be a handle to nothing, and the pending
buffers exist only to answer the next `poll`. What it keeps is everything that
can still be asked about a session that has already ended — `cwd` included,
though nothing currently reads it back.

---

## Terminal Screen

**File:** `screen_renderer.py`

Interactive programs do not append to a stream — they *redraw*. A REPL repaints
its prompt line after every keystroke; `vim` and `ssh` repaint the whole window.
Captured as a flat byte stream and merely ANSI-stripped, those repaints
concatenate instead of overwriting, and the result is unreadable:

```
>>> i>>> im>>> imp>>> impo>>> impor>>> import >>> import s...
```

`TerminalScreen` fixes this by being an actual terminal. It feeds the raw stream
to [pyte](https://github.com/selectel/pyte), applies the cursor movements to a
virtual 120×30 screen with 5000 lines of scrollback, and surfaces only the
resulting *display* text.

| Method | Returns | Used by |
|---|---|---|
| `feed(chunk)` | — | `SessionRegistry.append_output()`, on every chunk |
| `render_full()` | Scrollback + current screen | `log`, and the finished-session snapshot |
| `render_new()` | Everything since the previous call | `poll` |

### Who gets one

`ExecTool` attaches a screen for exactly one case: `mode=session` that resolved
to a PTY. One-shot and daemon runs keep the raw `strip_ansi` path, because they
are not redrawing anything and a scrollback-bounded emulator would only lose
output. The screen is created **before** the spawn, so the reader thread's
earliest output — the shell banner, the first prompt — is captured rather than
arriving before there is anywhere to put it.

This matters most on Windows. Python's PyREPL repaints the whole prompt per
keystroke and never enables bracketed paste under ConPTY, so without emulation
an interactive Python session is unusable.

### How `render_new()` stays incremental

`poll` must return only what is new, and the emulator has no notion of "since
last time". `render_new()` reconstructs it from an invariant: the live screen
sits immediately after the scrollback history, so `new_history + screen` begins
at the same absolute line as the screen the previous poll returned. Dropping the
longest common prefix of the two therefore yields exactly the new content — and
it is correct both when the terminal scrolled and when it redrew in place, which
a simple line count would not be.

### One consequence worth knowing

When a session exits, `mark_exited()` replaces `session.aggregated` with
`render_full()` before snapshotting it into the `FinishedSession`. The history
kept for an emulated session is therefore the rendered transcript, not the raw
byte stream — which is the readable thing to keep, but it does mean the raw
stream is not recoverable after exit.

---

## Key Encoder

**File:** `key_encoder.py`

Converts human-readable key names into raw ANSI escape sequences for writing to PTY stdin. This is how the LLM sends special keys like Ctrl+C, arrow keys, and function keys.

### Encoding Requests

```python
from kerkov.core.process import encode_key_sequence, KeyEncodingRequest

# Named keys
result = encode_key_sequence(KeyEncodingRequest(keys=["C-c"]))
# result.data = "\x03"  (Ctrl+C = ETX byte)

# Multiple keys
result = encode_key_sequence(KeyEncodingRequest(keys=["Up", "Up", "Enter"]))
# result.data = "\x1b[A\x1b[A\r"

# Hex bytes
result = encode_key_sequence(KeyEncodingRequest(hex_bytes=["0x04"]))
# result.data = "\x04"  (Ctrl+D / EOF)

# Literal text
result = encode_key_sequence(KeyEncodingRequest(literal="hello"))
# result.data = "hello"
```

### Modifier Syntax

Modifiers use a prefix notation: `C-` (Ctrl), `M-` (Alt/Meta), `S-` (Shift):

| Token | Sequence | Description |
|-------|----------|-------------|
| `C-c` | `\x03` | Ctrl+C (interrupt) |
| `C-d` | `\x04` | Ctrl+D (EOF) |
| `C-z` | `\x1a` | Ctrl+Z (suspend) |
| `C-a` | `\x01` | Ctrl+A (beginning of line) |
| `M-x` | `\x1bx` | Alt+X |
| `S-Tab` | `\x1b[Z` | Shift+Tab (back-tab) |
| `C-M-a` | `\x1b\x01` | Ctrl+Alt+A |
| `^C` | `\x03` | Caret shorthand for Ctrl+C |

### How Ctrl Keys Work

Ctrl+key encoding maps characters to their control-code equivalents. The formula is: `ASCII code of uppercase letter AND 0x1F`. For example:
- `C` = ASCII 67 → `67 & 31` = `3` → `\x03` (ETX, interrupt signal)
- `D` = ASCII 68 → `68 & 31` = `4` → `\x04` (EOT, end-of-transmission)
- `[` = ASCII 91 → `91 & 31` = `27` → `\x1b` (ESC)

This is why Ctrl+C sends the interrupt signal — the terminal translates `\x03` to SIGINT.

### xterm Modifier Encoding

For keys like arrows that support modifiers, xterm encoding inserts a modifier number:

```
ESC[A          → Up
ESC[1;2A       → Shift+Up     (modifier 2)
ESC[1;3A       → Alt+Up       (modifier 3)
ESC[1;5A       → Ctrl+Up      (modifier 5)
```

The modifier value is calculated as: `1 + (shift ? 1 : 0) + (alt ? 2 : 0) + (ctrl ? 4 : 0)`

### Named Key Map (Reference)

Common keys and their ANSI sequences:

| Key | Sequence | Notes |
|-----|----------|-------|
| `Enter` / `Return` | `\r` | Carriage return |
| `Tab` | `\t` | Horizontal tab |
| `Escape` / `Esc` | `\x1b` | ESC byte |
| `Space` | ` ` | Space character |
| `Backspace` / `BSpace` | `\x7f` | DEL byte |
| `Up` | `\x1b[A` | CSI A |
| `Down` | `\x1b[B` | CSI B |
| `Right` | `\x1b[C` | CSI C |
| `Left` | `\x1b[D` | CSI D |
| `Home` | `\x1b[1~` | CSI 1~ |
| `End` | `\x1b[4~` | CSI 4~ |
| `PageUp` | `\x1b[5~` | CSI 5~ |
| `PageDown` | `\x1b[6~` | CSI 6~ |
| `Insert` | `\x1b[2~` | CSI 2~ |
| `Delete` | `\x1b[3~` | CSI 3~ |
| `F1`-`F4` | `\x1bOP`-`\x1bOS` | SS3 sequences |
| `F5`-`F12` | `\x1b[15~`-`\x1b[24~` | CSI sequences |
| `BTab` | `\x1b[Z` | Back-tab (shift+tab) |

### Bracketed Paste Mode

When pasting text into a PTY, bracketed paste mode wraps the text in escape sequences so the terminal knows it's pasted (not typed). This prevents programs from executing pasted text character by character:

```python
from kerkov.core.process import encode_paste

payload = encode_paste("import sys\nprint(sys.version)")
# payload = "\x1b[200~import sys\nprint(sys.version)\x1b[201~"
#            ^^^^^^^^                                ^^^^^^^^
#            start bracket                           end bracket
```

Without bracketed paste, pasting `import sys\nprint(...)` into a Python REPL would execute `import sys` immediately at the newline, before the second line is received.

### Bracketing is negotiated, not assumed

Wrapping text in `ESC[200~`/`ESC[201~` for a program that has *not* enabled
bracketed paste does not degrade gracefully — the program receives the brackets
as literal input. So the mode is tracked rather than assumed: the PTY reader
watches the output stream for `ESC[?2004h` and `ESC[?2004l` and keeps
`PtyAdapterStdin.bracketed_paste_enabled` in sync with whatever the program last
asked for.

`ProcessTool`'s `paste` action resolves the flag in that order: an explicit
`bracketed` parameter wins; otherwise the tracked state, but only when the
session's stdin is a `PtyAdapterStdin`; otherwise off. A child-adapter session
is never bracketed, which is correct — there is no terminal to negotiate with.
Two consequences follow:

- A PyREPL running under ConPTY never enables bracketed paste, so pastes to it
  are sent unbracketed and the [terminal emulator](#terminal-screen) is what
  makes the result readable.
- A prompt that reads a password with echo off is a common case where an
  explicit `bracketed=false` is the fix for a paste that "did nothing".

---

## ANSI Stripper

**File:** `ansi_strip.py`

Strips terminal escape sequences from process output before sending it to the LLM. The LLM doesn't understand ANSI codes — it would see `\x1b[31mERROR\x1b[0m` instead of just `ERROR`.

### What It Removes

| Pattern | Example | Purpose |
|---------|---------|---------|
| CSI sequences | `\x1b[31m`, `\x1b[2J`, `\x1b[10;20H` | Colors, cursor movement, screen clearing |
| OSC sequences | `\x1b]0;title\x07` | Window title, hyperlinks |
| SS2/SS3 | `\x1bOA` | Alternate character sets |
| Single-char escapes | `\x1b=`, `\x1b>` | Keypad modes |
| CR overwrites | `\r` (not followed by `\n`) | Progress bar rewrites |
| Control chars | `\x00`-`\x1f` except `\n`, `\t`, `\r`, plus `\x7f` | Bells, form feeds, stray DEL |

### Usage

```python
from kerkov.core.process import strip_ansi

raw = "\x1b[32m✓ Scan complete\x1b[0m\r\n"
clean = strip_ansi(raw)
# clean = "✓ Scan complete\r\n"
```

Stripping is applied on the **`poll` path only**, and only when the session is
not terminal-emulated:

| Call | Session | What the LLM gets |
|---|---|---|
| `poll` | Emulated (`session.screen` set) | `screen.render_new()` — no stripping; the emulator already resolved the escapes |
| `poll` | Not emulated | `strip_ansi(stdout)` and `strip_ansi(stderr)`, drained from the pending buffers |
| `poll` | Already finished | `strip_ansi(finished.tail)` |
| `log` | Emulated | `screen.render_full()` |
| `log` | Not emulated | `session.aggregated` **as captured** |

!!! note "`log` does not strip"
    The last row is not a typo. `_handle_log()` never calls `strip_ansi`, so a
    non-emulated session's history reaches the LLM with its escape sequences
    intact. In practice the sessions whose output is full of escapes are the
    interactive ones, and those are emulated — but a one-shot or daemon run of a
    colourising command is backgrounded, unemulated, and its `log` output is raw.

---

## Kill Tree

**Files:** `kill_tree.py` (dispatcher) → `posix_kill.py` / `windows_kill.py`

Terminates an entire process tree (parent + all children) with a graceful shutdown sequence. `kill_tree.py` owns grace-period normalization and dispatches to the platform backend by `sys.platform`. The Windows backend (`windows_kill.py`) substitutes `taskkill /F /T` (plus a psutil child sweep) for the POSIX `killpg(SIGTERM)` → grace → `killpg(SIGKILL)` flow described below.

### Two-Phase Shutdown (POSIX)

```
Phase 1: SIGTERM (graceful)
  │
  ├─▶ os.killpg(pid, SIGTERM)     ← process group signal
  │   (falls back to os.kill(pid, SIGTERM) if group signal fails)
  │
  ▼
  Wait grace period (default 3 seconds)
  │
  ▼
Phase 2: SIGKILL (forced)
  │
  ├─▶ os.killpg(pid, SIGKILL)     ← force-kill the group
  │   (falls back to os.kill(pid, SIGKILL))
  └─▶ done
```

### Usage

```python
from kerkov.core.process import kill_process_tree

# Default: 3-second grace period
kill_process_tree(pid=12345)

# Custom grace period
kill_process_tree(pid=12345, grace_ms=5000)

# Immediate kill (no grace)
kill_process_tree(pid=12345, grace_ms=0)
```

### Why Process Groups?

When you run `bash -c "nmap -sV target"`, bash spawns nmap as a child. Killing only the bash PID leaves nmap running as an orphan. Process group kill (`os.killpg`) sends the signal to the entire group — bash and all its children.

The grace period is clamped to `[0, 60000]` ms and the SIGKILL phase runs on a daemon timer thread so the caller isn't blocked.

---

## Process Tool (Consumer)

**File:** `src/kerkov/tools/process_tool.py`

The LLM-facing tool that exposes session management via structured actions. This is how the LLM interacts with backgrounded processes.

### Actions

| Action | Description | Requires |
|--------|-------------|----------|
| `list` | Show all running + recently finished sessions | Nothing |
| `poll` | Get new output since last poll, optionally wait | `session_id` |
| `log` | Read full output history with pagination | `session_id` |
| `paste` | Send text to PTY session (preferred for typing) | `session_id`, `text` |
| `submit` | Press Enter (send `\r`) | `session_id` |
| `write` | Send raw data to stdin (for non-PTY sessions) | `session_id`, `data` |
| `send_keys` | Send special/control keys only | `session_id`, `keys` |
| `kill` | Terminate a backgrounded session | `session_id` |

### Waiting and Paging

`poll` takes an optional `timeout` in milliseconds and waits for it in 250 ms
slices, re-checking `session.exited` on each one, so a process that finishes
early returns early. The value is clamped to `[0, MAX_POLL_WAIT_MS]`
(120,000 ms): a model asking for an hour-long poll would otherwise pin a turn
against a session that may never produce anything.

`log` defaults to the **last `DEFAULT_LOG_TAIL_LINES` (200) lines** and appends a
note saying so when it truncated. Passing `offset` and/or `limit` switches to
explicit paging from the start of the history instead.

### Example: Interactive Python Session

```
1. ExecTool spawns:  python3 -i  (ExecMode.SESSION → pty, backgrounded)
   → session_id = "a1b2c3d4"

2. ProcessTool(action="paste", session_id="a1b2c3d4", text="import sys")
   → "Pasted 10 chars to session a1b2c3d4."

3. ProcessTool(action="submit", session_id="a1b2c3d4")
   → "Submitted session a1b2c3d4 (sent CR)."

4. ProcessTool(action="poll", session_id="a1b2c3d4", timeout=2000)
   → ">>> import sys\n\nProcess still running."

5. ProcessTool(action="paste", session_id="a1b2c3d4", text="print(sys.version)")
   ProcessTool(action="submit", session_id="a1b2c3d4")

6. ProcessTool(action="poll", session_id="a1b2c3d4", timeout=2000)
   → ">>> print(sys.version)\n3.13.2 ...\n\nProcess still running."

7. ProcessTool(action="send_keys", session_id="a1b2c3d4", keys=["C-d"])
   → EOF sent, Python exits

8. ProcessTool(action="poll", session_id="a1b2c3d4")
   → "Process exited with code 0."
```

### Example: Running a Long Scan

```
1. ExecTool spawns:  nmap -sC -sV -p- 10.0.0.1  (ExecMode.DAEMON → backgrounded)
   → session_id = "b2c3d4e5"

2. ProcessTool(action="poll", session_id="b2c3d4e5", timeout=5000)
   → "Starting Nmap 7.94...\n\nProcess still running."

3. ProcessTool(action="poll", session_id="b2c3d4e5", timeout=10000)
   → "Discovered open port 22/tcp...\n\nProcess still running."

4. ... (LLM polls periodically) ...

5. ProcessTool(action="log", session_id="b2c3d4e5")
   → Full output history (last 200 lines by default)

6. ProcessTool(action="log", session_id="b2c3d4e5", offset=0, limit=50)
   → First 50 lines of output
```

### Example: Interrupting a Hung Process

```
1. ProcessTool(action="send_keys", session_id="x1y2z3w4", keys=["C-c"])
   → "Sent 1 bytes to session x1y2z3w4."

2. ProcessTool(action="poll", session_id="x1y2z3w4", timeout=2000)
   → "^C\nKeyboardInterrupt\n\nProcess still running."

   (If still hung:)
3. ProcessTool(action="kill", session_id="x1y2z3w4")
   → "Termination requested for session x1y2z3w4."
```

---

## End-to-End Walkthrough

Here's the complete flow when the LLM runs `python3 -i` as a backgrounded PTY session:

### 1. Spawn

```
ExecTool.run(command="python3 -i", mode=ExecMode.SESSION)
    │
    ├─▶ registry.create_session_id()  → "a1b2c3d4"
    ├─▶ registry.add_session(ProcessSession(...))
    │
    ├─▶ supervisor.spawn(command="python3 -i", mode="pty", ...)
    │       │
    │       ├─▶ PtyAdapter.create("python3 -i", cols=120, rows=30)
    │       │       │
    │       │       ├─▶ PtyProcess.spawn(["bash", "-c", "python3 -i"])
    │       │       │     (runs in thread executor — blocking call)
    │       │       │
    │       │       ├─▶ PtyAdapter._start_exit_watcher()
    │       │       │     (daemon thread: waits for PTY exit)
    │       │       │
    │       │       └─▶ return PtyAdapter
    │       │
    │       ├─▶ adapter.on_stdout(on_supervisor_stdout)
    │       │     (starts daemon reader thread)
    │       │
    │       ├─▶ Set up overall + no-output timers
    │       │
    │       └─▶ return ManagedRun(run_id="a1b2c3d4", stdin=..., wait=..., cancel=...)
    │
    ├─▶ registry.mark_backgrounded(session)
    └─▶ return "Session a1b2c3d4 started (pid=12345)"
```

### 2. Output Flow

```
PTY reader thread:
    PtyProcess.read(4096)  →  raw bytes
    decode UTF-8           →  "Python 3.13.2 ...\r\n>>> "
    │
    ▼
on_supervisor_stdout():
    strip_dsr_requests()   →  (cleaned text, 0 DSR requests)
    on_stdout(cleaned)     →  (external callback, if any)
    stdout_buf.append(cleaned)
    │
    ▼
registry.append_output(session, "stdout", cleaned):
    pending_stdout.append(chunk)
    aggregated += chunk
    tail = last 2000 chars
    (cap buffers if over limit)
```

### 3. LLM Polls

```
ProcessTool.run(action="poll", session_id="a1b2c3d4", timeout=2000)
    │
    ├─▶ registry.get_session("a1b2c3d4")  →  session (running)
    │
    ├─▶ Wait up to 2000ms (asyncio.sleep in 250ms intervals)
    │     checking session.exited each iteration
    │
    ├─▶ registry.drain_session(session)
    │     (pending buffers cleared — but this session is emulated,
    │      so the drained text is not what gets returned)
    │
    ├─▶ session.screen.render_new()  →  "Python 3.13.2 ...\n>>> "
    │
    └─▶ return ToolResult(output="Python 3.13.2 ...\n>>> \n\nProcess still running.")
```

`python3 -i` was spawned as `mode=session` over a PTY, so it has a
[`TerminalScreen`](#terminal-screen) and `poll` renders from it. A one-shot or
daemon session takes the other branch and gets `strip_ansi(stdout)` instead.

### 4. LLM Sends Input

```
ProcessTool.run(action="paste", session_id="a1b2c3d4", text="print('hello')")
    │
    ├─▶ resolve bracketing:
    │     params.bracketed if the caller set it, else
    │     session.stdin.bracketed_paste_enabled  ← PTY stdin only, tracked
    │                                              live from the output stream
    │
    ├─▶ encode_paste("print('hello')", bracketed)
    │     → "\x1b[200~print('hello')\x1b[201~"   (bracketed)
    │     → "print('hello')"                     (not bracketed)
    │
    └─▶ session.stdin.write(payload)
          PtyProcess.write(payload.encode("utf-8"))

ProcessTool.run(action="submit", session_id="a1b2c3d4")
    │
    └─▶ session.stdin.write("\r")
          PtyProcess.write(b"\r")
```

### 5. Process Exits

```
Exit watcher thread:
    self._pty.wait()         → blocks until Python exits
    exitstatus = 0
    signalstatus = None
    │
    ├─▶ _settle_wait(WaitResult(code=0, signal=None))
    │       _wait_result = result
    │       loop.call_soon_threadsafe(_wait_event.set)
    │
    ▼
supervisor.wait_for_exit() unblocks:
    adapter.wait()           → WaitResult(code=0)
    clear_timers()
    adapter.dispose()        → reader_stop.set(), cancel tasks
    self._active.pop("a1b2c3d4")
    │
    └─▶ return RunExit(reason=EXIT, exit_code=0, stdout="...", duration_ms=45000)

LLM polls again → session marked finished → "Process exited with code 0."

After 30 minutes: sweeper prunes the finished session from memory.
```
