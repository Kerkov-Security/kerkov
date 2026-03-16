# Process Management System

The process management system (`src/core/process/`) provides PTY and subprocess adapters for spawning, monitoring, and interacting with shell processes. It enables the LLM to run long-lived interactive sessions (Python REPLs, `nmap` scans, `vim`, etc.) alongside simple one-shot commands.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Adapter Interface](#adapter-interface)
3. [Child Adapter](#child-adapter)
4. [PTY Adapter](#pty-adapter)
5. [Process Supervisor](#process-supervisor)
6. [Session Registry](#session-registry)
7. [Key Encoder](#key-encoder)
8. [ANSI Stripper](#ansi-stripper)
9. [Kill Tree](#kill-tree)
10. [Process Tool (Consumer)](#process-tool-consumer)
11. [End-to-End Walkthrough](#end-to-end-walkthrough)

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
  - kill_process_tree() → graceful SIGTERM → SIGKILL shutdown
```

The system has **three layers**:

| Layer | Module | Role |
|-------|--------|------|
| **Adapter** | `child_adapter.py`, `pty_adapter.py` | Low-level process I/O — spawn, read, write, kill |
| **Supervisor** | `supervisor.py` | Lifecycle orchestration — timeouts, fallback, DSR handling |
| **Registry** | `session_registry.py` | Session state, output buffering, draining, cleanup |

The `ProcessTool` (in `src/tools/process_tool.py`) is the LLM-facing consumer that ties all three together.

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
| `NO_OUTPUT_TIMEOUT` | Idle too long with no stdout/stderr |
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
    timed_out: bool                 # True if overall or idle timeout
    no_output_timed_out: bool       # True specifically for idle timeout
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
    def kill(self, sig) -> None             # send signal
    def dispose(self) -> None               # cleanup resources
```

#### `ManagedRun`

The handle the supervisor returns after spawning:

```python
@dataclass
class ManagedRun:
    run_id: str                                       # unique session ID
    pid: Optional[int]                                # OS process ID
    started_at_ms: float                              # epoch millis
    stdin: Optional[AdapterStdin]                     # write handle
    wait: Callable[[], Awaitable[RunExit]]            # await this for exit
    cancel: Callable[[Optional[TerminationReason]], None]  # kill the run
```

### Background: What is a PTY?

A **pseudo-terminal (PTY)** is a pair of virtual devices — a "master" and a "slave" — that emulate a hardware terminal. When a program is run inside a PTY, it believes it's connected to a real terminal:
- It receives `TERM` environment variable, terminal dimensions (rows/cols)
- Interactive programs enable line editing, color output, cursor movement
- stdout and stderr are **merged** into a single output stream (just like a real terminal)

Without a PTY, programs detect they're piped and may:
- Disable color output
- Buffer output in large blocks instead of line-by-line
- Skip interactive features (no readline, no progress bars)

The child adapter uses plain pipes (separate stdout/stderr), while the PTY adapter creates a real pseudo-terminal. The supervisor falls back to the child adapter if PTY spawn fails.

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
)
```

**Parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `command` | required | Shell command string passed to `bash -c` |
| `cwd` | `None` | Working directory |
| `env` | `None` | Environment variables dict |
| `stdin_mode` | `"pipe-closed"` | `"pipe-closed"` closes stdin after optional `input_data`; `"pipe-open"` leaves it open for writing |
| `input_data` | `None` | Data to write to stdin before potentially closing it |

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

### Kill Behavior

- `SIGKILL` → uses `kill_process_tree()` for full tree termination
- Other signals → `proc.send_signal(sig)` (direct to process only)

### Why `start_new_session=True`?

This creates a new **process group** so `kill_process_tree()` can send signals to the entire tree (parent + children) via `os.killpg()`. Without it, only the direct process would receive the signal, leaving children orphaned.

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
    cols=120,
    rows=30,
)
```

Since `PtyProcess.spawn()` is blocking, it runs in a thread executor so the event loop isn't blocked.

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
from src.core.process import get_process_supervisor

supervisor = get_process_supervisor()  # thread-safe singleton
```

### Spawning a Process

```python
run = await supervisor.spawn(
    command="nmap -sC -sV 10.0.0.1",
    mode="pty",                  # or "child"
    timeout_ms=300_000,          # 5-minute wall-clock limit
    no_output_timeout_ms=60_000, # kill if idle 60s
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
        adapter = await PtyAdapter.create(command, ...)
    except Exception:
        # PTY unavailable — fall back silently
        adapter = await ChildAdapter.create(command, ...)
```

The supervisor will silently fall back to the child adapter if PTY spawn fails (e.g., on systems without `/dev/ptmx` or when `ptyprocess` is not installed). This makes PTY a "best-effort" upgrade.

### Dual Timeout System

The supervisor manages two independent timers:

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

- **Overall timeout**: Hard limit on total runtime. Once reached, the process is killed regardless of activity.
- **No-output timeout**: Resets every time the process produces output. Kills the process if it goes silent for too long.

Both timers use `loop.call_later()` for non-blocking scheduling. The no-output timer is reset via `touch_output()`, which is thread-safe (uses `loop.call_soon_threadsafe()`) since PTY output arrives from a background thread.

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

Cancellation sets the forced reason and sends `SIGKILL` to the process.

---

## Session Registry

**File:** `session_registry.py`

Thread-safe registry that tracks running and finished process sessions with sophisticated output buffering.

### Singleton Access

```python
from src.core.process import get_session_registry

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

### `ProcessSession` Fields

```python
@dataclass
class ProcessSession:
    id: str                            # 8-char hex slug (e.g., "a1b2c3d4")
    command: str                       # the shell command
    started_at: float                  # time.time()
    cwd: Optional[str]                # working directory
    pid: Optional[int]                # OS process ID
    max_output_chars: int = 200_000   # aggregated buffer cap
    pending_max_output_chars: int = 30_000  # pending buffer cap

    # Output state
    pending_stdout: list[str]          # chunks since last drain
    pending_stderr: list[str]
    aggregated: str                    # full concatenated history
    tail: str                          # last 2000 chars
    truncated: bool                    # True if any buffer was capped

    # Lifecycle
    exited: bool                       # True after process exits
    exit_code: Optional[int]
    exit_signal: Optional[int]
    backgrounded: bool                 # True if handed off for polling

    stdin: Optional[object]            # AdapterStdin handle
```

### `FinishedSession` Fields

A snapshot created when `mark_exited()` is called on a backgrounded session:

```python
@dataclass
class FinishedSession:
    id: str
    command: str
    started_at: float
    ended_at: float
    status: str         # "completed" | "failed" | "killed"
    exit_code: Optional[int]
    exit_signal: Optional[int]
    aggregated: str     # full output history
    tail: str           # last 2000 chars
    truncated: bool
    total_output_chars: int
```

---

## Key Encoder

**File:** `key_encoder.py`

Converts human-readable key names into raw ANSI escape sequences for writing to PTY stdin. This is how the LLM sends special keys like Ctrl+C, arrow keys, and function keys.

### Encoding Requests

```python
from src.core.process import encode_key_sequence, KeyEncodingRequest

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
from src.core.process import encode_paste

payload = encode_paste("import sys\nprint(sys.version)")
# payload = "\x1b[200~import sys\nprint(sys.version)\x1b[201~"
#            ^^^^^^^^                                ^^^^^^^^
#            start bracket                           end bracket
```

Without bracketed paste, pasting `import sys\nprint(...)` into a Python REPL would execute `import sys` immediately at the newline, before the second line is received.

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
| Control chars | `\x00`-`\x1f` except `\n`, `\t`, `\r` | Bells, form feeds, etc. |

### Usage

```python
from src.core.process import strip_ansi

raw = "\x1b[32m✓ Scan complete\x1b[0m\r\n"
clean = strip_ansi(raw)
# clean = "✓ Scan complete\r\n"
```

The stripping is applied in two places:
1. **`ProcessTool._handle_poll()`** — when returning new output to the LLM
2. **`ProcessTool._handle_log()`** — when returning full output history

---

## Kill Tree

**File:** `kill_tree.py`

Terminates an entire process tree (parent + all children) with a graceful shutdown sequence.

### Two-Phase Shutdown

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
from src.core.process import kill_process_tree

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

**File:** `src/tools/process_tool.py`

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

### Example: Interactive Python Session

```
1. ExecTool spawns:  python3 -i  (mode=pty, backgrounded=True)
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
1. ExecTool spawns:  nmap -sC -sV -p- 10.0.0.1  (backgrounded=True)
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
ExecTool.run(command="python3 -i", mode="pty", background=True)
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
    touch_output()         →  reset no-output timer (thread-safe)
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
    │     stdout = "Python 3.13.2 ...\r\n>>> "
    │     stderr = ""
    │     (pending buffers cleared)
    │
    ├─▶ strip_ansi(stdout)  →  "Python 3.13.2 ...\n>>> "
    │
    └─▶ return ToolResult(output="Python 3.13.2 ...\n>>> \n\nProcess still running.")
```

### 4. LLM Sends Input

```
ProcessTool.run(action="paste", session_id="a1b2c3d4", text="print('hello')")
    │
    ├─▶ encode_paste("print('hello')", bracketed=True)
    │     → "\x1b[200~print('hello')\x1b[201~"
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
