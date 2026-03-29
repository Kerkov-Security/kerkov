# Async Input Manager — Implementation Plan

## Goal

Replace the current synchronous, blocking input model with an async input system that:
1. Lets users press **Escape** to interrupt LLM calls or tool execution
2. Lets users **type ahead** while the bot is thinking
3. Keeps the input line **always available** (not just at the explicit prompt)

## New Dependency

**`prompt_toolkit`** — async-native terminal input library with key binding support, input buffering, and cross-platform compatibility (Windows + Unix).

```
pip install prompt_toolkit
```

Add to `pyproject.toml` / `setup.cfg` under `install_requires`.

---

## Components

### 1. `InputManager` — `src/cli/input_manager.py` (NEW)

The central component. Owns all stdin interaction. Nothing else in the codebase should read from stdin directly after this is in place.

**Responsibilities:**
- Maintain a `prompt_toolkit.PromptSession` for line input (history, editing, completion)
- Expose `async get_line(prompt: str) -> str` — async replacement for `console.input()`
- Expose an `asyncio.Event` called `interrupt_requested` that fires on Escape keypress
- Expose `reset_interrupt()` to clear the event after it's been handled
- Expose `get_buffered_input() -> Optional[str]` — returns any text the user typed during a thinking phase (type-ahead buffer)
- Register **key bindings**:
  - `Escape` → sets `interrupt_requested`, prints a subtle "[interrupted]" indicator
  - Standard editing keys (handled by prompt_toolkit by default)
- Use `prompt_toolkit.patch_stdout()` so Rich console output doesn't corrupt the input line

**Key design decisions:**
- The `PromptSession` runs with `patch_stdout=True` so any `console.print()` from Rich is safe while the prompt is active
- The interrupt event is an `asyncio.Event` so `get_response()` can `await` it concurrently with LLM calls
- Type-ahead: when the user types while no prompt is active, keystrokes go into a buffer. When `get_line()` is next called, the buffer pre-populates the input field

**Constructor params:**
- `console: Console` — the Rich console instance (for coordinating output)

**Skeleton:**
```python
class InputManager:
    def __init__(self, console: Console):
        self.console = console
        self.interrupt_requested = asyncio.Event()
        self._session = PromptSession(...)
        self._key_bindings = self._build_key_bindings()

    async def get_line(self, prompt: str) -> str:
        """Async line input. Replaces console.input()."""
        ...

    def reset_interrupt(self) -> None:
        """Clear the interrupt flag after handling."""
        ...

    def _build_key_bindings(self) -> KeyBindings:
        """Register Escape and any custom bindings."""
        ...
```

---

### 2. `CancellableTask` utility — `src/cli/cancellable_task.py` (NEW)

A small wrapper that runs an awaitable as an `asyncio.Task` and cancels it when the InputManager's interrupt event fires.

**Responsibilities:**
- Accept an awaitable + the `InputManager.interrupt_requested` event
- Run the awaitable and a watcher concurrently via `asyncio.wait` with `FIRST_COMPLETED`
- If the interrupt event fires first, cancel the task and raise a custom `InterruptedByUser` exception
- If the awaitable completes first, return its result normally
- Handle `asyncio.CancelledError` cleanup inside the cancelled task

**Interface:**
```python
class InterruptedByUser(Exception):
    """Raised when the user presses Escape during an operation."""
    pass

async def run_cancellable(
    coro: Coroutine,
    interrupt_event: asyncio.Event,
) -> Any:
    """Run a coroutine, cancelling it if interrupt_event is set."""
    ...
```

**Usage in `get_response()`:**
```python
try:
    response = await run_cancellable(
        provider.complete(...),
        self.input_manager.interrupt_requested,
    )
except InterruptedByUser:
    console.print("[dim]Interrupted by user[/dim]")
    self.input_manager.reset_interrupt()
    return
```

---

### 3. Refactor `KerkovCLI` — `src/cli/kerkov_cli.py` (MODIFY)

**Changes:**

#### 3a. Add `InputManager` as a member
- Instantiate in `__init__` alongside existing members
- Pass the shared `console` instance to it

#### 3b. Replace all `console.input()` calls with `self.input_manager.get_line()`
- Main loop (line 103): `user_input = await self.input_manager.get_line(prompt)`
- CTF context prompting (lines 342-392): all `console.input()` → `await self.input_manager.get_line()`
- This makes the main loop truly async — the event loop is free during input

#### 3c. Wrap LLM calls in `run_cancellable()`
- The `await provider.complete(...)` call (line 184) becomes:
  ```python
  response = await run_cancellable(
      self.provder_manager.provider.complete(...),
      self.input_manager.interrupt_requested,
  )
  ```

#### 3d. Wrap tool execution in `run_cancellable()`
- The `await tool.execute(...)` call (line 220) becomes:
  ```python
  tool_result = await run_cancellable(
      tool.execute(tool_call.parameters),
      self.input_manager.interrupt_requested,
  )
  ```

#### 3e. Handle `InterruptedByUser`
- Catch at two levels:
  1. **Per-tool**: If interrupted during a tool call, record a "tool interrupted" message in history so the LLM knows what happened, then break out of the tool loop
  2. **Per-LLM-call**: If interrupted during `provider.complete()`, print "[interrupted]" and return from `get_response()` — the user is back at the prompt

#### 3f. Remove the `console.status()` spinner coordination concern
- `prompt_toolkit.patch_stdout()` handles Rich output safety
- The spinner can continue to work as-is since it's just Rich output

---

### 4. Refactor `ToolApprovalCliHelper` — `src/cli/tool_approval_helper.py` (MODIFY)

**Changes:**
- Accept `InputManager` in constructor (or individual methods)
- Replace `console.input()` calls (lines 80, 89) with `await input_manager.get_line()`
- Make `get_tool_usage_approval()` and `_prompt_approval_box()` **async**
- Update the call site in `kerkov_cli.py` to `await`

**Why async matters here:** The approval prompt currently blocks the event loop. Making it async means the interrupt listener is still active — the user could potentially press Escape to skip approval (auto-deny), though this is a future enhancement.

---

### 5. Provider Cancellation Cleanup — `src/providers/anthrophic_provider.py` (MODIFY)

**Changes:**
- Wrap the streaming context manager in a try/except for `asyncio.CancelledError`
- On cancellation, ensure the stream is properly closed (the `async with` should handle this, but add explicit logging)
- Other providers (Gemini, Ollama) need the same treatment when they gain streaming support

**Why:** When `run_cancellable()` cancels the task, `asyncio.CancelledError` propagates into the `async with self.client.messages.stream(...)` block. The context manager's `__aexit__` should close the connection, but we want to verify this and log it.

---

### 6. Update `BaseLLMProvider` — `src/providers/base_provider.py` (MODIFY, minor)

**Optional enhancement:**
- Add a `cancel()` method to the base class (default no-op)
- Providers that hold stateful connections can override to close them explicitly
- `run_cancellable()` can call this before/after task cancellation for extra safety

This is optional for the initial implementation — `asyncio.CancelledError` + context manager cleanup should suffice.

---

## Migration Order

Execute in this order to keep the codebase functional at each step:

1. **Add `prompt_toolkit` dependency**
2. **Create `InputManager`** — build and unit test in isolation
3. **Create `CancellableTask`** — build and unit test in isolation
4. **Refactor `ToolApprovalCliHelper`** — make async, accept InputManager
5. **Refactor `KerkovCLI`** — wire InputManager in, replace `console.input()`, wrap calls in `run_cancellable()`
6. **Provider cleanup** — add CancelledError handling to Anthropic provider
7. **Integration test** — manual testing of Escape interrupt, type-ahead, and normal flow

---

## Files Affected Summary

| File | Action | Description |
|------|--------|-------------|
| `pyproject.toml` (or equivalent) | MODIFY | Add `prompt_toolkit` dependency |
| `src/cli/input_manager.py` | **NEW** | Core input management with async input, interrupt events, type-ahead |
| `src/cli/cancellable_task.py` | **NEW** | `run_cancellable()` utility + `InterruptedByUser` exception |
| `src/cli/kerkov_cli.py` | MODIFY | Wire InputManager, replace `console.input()`, wrap async calls |
| `src/cli/tool_approval_helper.py` | MODIFY | Make async, use InputManager for input |
| `src/providers/anthrophic_provider.py` | MODIFY | CancelledError handling in stream context |
| `src/providers/base_provider.py` | MODIFY (optional) | Add `cancel()` method to base class |

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Rich + prompt_toolkit terminal conflicts | Garbled output | Use `prompt_toolkit.patch_stdout()` context; test early on Windows |
| Escape key conflicts with terminal escape sequences | False interrupts | Use prompt_toolkit's key binding system which handles escape sequence disambiguation (waits briefly to distinguish Escape from Alt+key) |
| CancelledError not propagating cleanly through providers | Dangling connections | Add explicit try/except in provider `complete()` methods; test cancellation path |
| Type-ahead buffer confusing users | Unexpected input submission | Show buffered text visually in the prompt when it becomes active; allow editing before submit |
| Windows terminal compatibility | Different terminal behavior | prompt_toolkit handles Windows natively (uses Win32 APIs); test on Windows Terminal and legacy cmd |
| Breaking existing message handlers that use `console.input()` | CTF prompting, codex-login broken | Audit all `console.input()` call sites (there are ~12); migrate all in one pass |

---

## Out of Scope (Future Enhancements)

- Tab completion for `/commands` (easy to add once InputManager exists)
- Input history persistence across sessions (prompt_toolkit `FileHistory`)
- Vi/Emacs keybinding modes
- Multi-line input editing
- Progress bars instead of spinners during tool execution
- Streaming LLM output (token-by-token display) — a natural next step once the async input infrastructure is in place
