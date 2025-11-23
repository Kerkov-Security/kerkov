from abc import ABC, abstractmethod
import asyncio
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass
from datetime import datetime
from collections import deque

@dataclass
class ShellEvent:
    """
    Represents a shell interaction event displayed in terminal view.

    Attributes:
        timestamp: When the event occurred
        event_type: Type of event ('command', 'stdout', 'stderr')
        content: The actual content (command text or output)
        source: Who initiated this ('user', 'assistant', 'system')
    """
    timestamp: datetime
    event_type: str  # 'command', 'stdout', 'stderr'
    content: str
    source: str  # "user", "assistant", "system"

    def __str__(self) -> str:
        """String representation for easy display."""
        time_str = self.timestamp.strftime('%H:%M:%S')

        prefix = {
            'command': f"[{self.source.upper()} CMD]",
            'stdout': f"[{self.source.upper()} OUT]",
            'stderr': f"[{self.source.upper()} ERR]"
        }.get(self.event_type, f"[{self.source.upper()} ???]")

        return f"{time_str} {prefix} {self.content}"

class Shell(ABC):
    """
    Abstract base class for shell implementations (bash, zsh, PowerShell, etc.).

    All shells run in headless mode with captured stdout/stderr.
    Use ShellManager's view_mode to control how output is displayed in terminal views.
    """

    def __init__(
        self,
        output_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.process: Optional[asyncio.subprocess.Process] = None
        self.running = False
        self.output_callback = output_callback

        self._event_buffer: deque[ShellEvent] = deque(maxlen=10000)

        self._unread_events: list[ShellEvent] = []

        # LLM read tracking - index into _event_buffer for what LLM has seen
        self._llm_last_seen_index: int = 0

        # Lock for thread-safe buffer access
        self._buffer_lock = asyncio.Lock()

        # Listener tasks for stdout/stderr
        self._stdout_listener_task: Optional[asyncio.Task] = None
        self._stderr_listener_task: Optional[asyncio.Task] = None

    def _add_event(self, event_type: str, content: str, source: str = 'system'):
        """
        Add an event to the buffer (thread-safe through deque).

        Args:
            event_type: Type of event ('command', 'stdout', 'stderr')
            content: The event content
            source: Source of the event ('user', 'assistant', 'system')
        """
        event = ShellEvent(
            timestamp=datetime.now(),
            event_type=event_type,
            content=content,
            source=source
        )
        self._event_buffer.append(event)
        self._unread_events.append(event)

    async def get_output(self) -> list[ShellEvent]:
        """
        Retrieve new events since last call.

        Returns:
            List of ShellEvent objects, or empty list if no new events
        """
        async with self._buffer_lock:
            if not self._unread_events:
                return []

            events = self._unread_events.copy()
            self._unread_events.clear()
            return events

    async def get_history(self, limit: Optional[int] = None) -> list[ShellEvent]:
        """
        Retrieve historical events from the buffer in chronological order.

        Args:
            limit: Maximum number of most recent events to return. None returns all.
                   Events are returned in chronological order (oldest to newest).

        Returns:
            List of ShellEvent objects in chronological order

        Example:
            Get last 10 events in chronological order\n
            events = await shell.get_history(limit=10)\n
            events[0] is oldest of the 10, events[-1] is newest
        """
        async with self._buffer_lock:
            if limit is None:
                return list(self._event_buffer)
            else:
                return list(self._event_buffer)[-limit:]

    async def get_history_as_text(self, limit: Optional[int] = None) -> str:
        """
        Get history formatted as readable text.

        Args:
            limit: Maximum number of events to return

        Returns:
            Formatted string of shell history
        """
        events = await self.get_history(limit)
        return "\n".join(str(event) for event in events)

    async def get_unread_for_llm(self) -> list[ShellEvent]:
        """
        Get events the LLM hasn't seen yet.

        This is separate from get_output() which is used for toggle mode display.
        Use this in ShellTool to provide context before executing commands.

        Returns:
            List of ShellEvent objects the LLM hasn't seen, in chronological order

        Example:
            # In ShellTool.execute():
            unread = await shell.get_unread_for_llm()
            if unread:
                context = "Events since your last command:\\n" + ...
        """
        async with self._buffer_lock:
            buffer_list = list(self._event_buffer)
            unread_events = buffer_list[self._llm_last_seen_index:]
            self._llm_last_seen_index = len(buffer_list)
            return unread_events

    async def get_unread_for_llm_as_text(self) -> str:
        """
        Get events the LLM hasn't seen yet, formatted as text.

        Returns:
            Formatted string of unread events, or empty string if none
        """
        events = await self.get_unread_for_llm()
        return "\n".join(str(event) for event in events)

    async def _listen_stdout(self):
        """
        Listen for stdout output from the shell subprocess.

        This is a concrete implementation that works for all shells.
        Subclasses can override if they need special handling.
        """
        if not self.process or not self.process.stdout:
            return

        try:
            while self.running and self.process.returncode is None:
                line = await self.process.stdout.readline()

                if not line:
                    break

                output = line.decode('utf-8', errors='replace').rstrip()

                self._add_event('stdout', output, 'system')

                if self.output_callback:
                    await self.output_callback(output)

        except Exception as e:
            error_msg = f"Error in stdout listener: {e}"
            print(error_msg)
            self._add_event('stderr', error_msg, 'system')
        finally:
            self.running = False

    async def _listen_stderr(self):
        """
        Listen for stderr output from the shell subprocess.

        This is a concrete implementation that works for all shells.
        Subclasses can override if they need special handling.
        """
        if not self.process or not self.process.stderr:
            return

        try:
            while self.running and self.process.returncode is None:
                # Read error output line by line
                line = await self.process.stderr.readline()

                if not line:
                    break

                error_output = line.decode('utf-8', errors='replace').rstrip()

                # Add to event buffer
                self._add_event('stderr', error_output, 'system')

                # Call the output callback if provided (for real-time display)
                if self.output_callback:
                    await self.output_callback(f"[ERROR] {error_output}")

        except Exception as e:
            error_msg = f"Error in stderr listener: {e}"
            print(error_msg)
            self._add_event('stderr', error_msg, 'system')

    def _start_listeners(self):
        """
        Start stdout and stderr listener tasks.

        Called by subclasses after spawning the subprocess.
        """
        if self.process:
            self._stdout_listener_task = asyncio.create_task(self._listen_stdout())
            self._stderr_listener_task = asyncio.create_task(self._listen_stderr())

    async def _cleanup_listeners(self):
        """
        Cancel and cleanup listener tasks.

        Called by subclasses during termination.
        """
        if self._stdout_listener_task and not self._stdout_listener_task.done():
            self._stdout_listener_task.cancel()
            try:
                await self._stdout_listener_task
            except asyncio.CancelledError:
                pass

        if self._stderr_listener_task and not self._stderr_listener_task.done():
            self._stderr_listener_task.cancel()
            try:
                await self._stderr_listener_task
            except asyncio.CancelledError:
                pass

        self._stdout_listener_task = None
        self._stderr_listener_task = None

    @abstractmethod
    async def spawn(self) -> bool:
        """
        Spawn a shell subprocess in headless mode with captured input/output.

        Returns:
            True if successfully spawned, False otherwise
        """
        self.running = True

    async def send_command(self, command: str, source: str = 'assistant') -> bool:
        """
        Send a command to the shell subprocess (fire and forget).

        Use this for long-running commands where you'll check output later.

        Args:
            command: The command to execute
            source: Who is sending the command ('user' or 'assistant')

        Returns:
            True if successfully sent, False otherwise

        Example:
            await shell.send_command("nmap -sV 192.168.1.1")
            await asyncio.sleep(10)
            events = await shell.get_output()
        """
        if not self.process or not self.process.stdin:
            error_msg = "Shell process not running"
            print(error_msg)
            self._add_event('stderr', error_msg, 'system')
            return False

        try:
            # Add command to event buffer
            self._add_event('command', command, source)

            # Write command followed by newline (works for bash, zsh, PowerShell)
            self.process.stdin.write(f"{command}\n".encode('utf-8'))
            await self.process.stdin.drain()
            return True

        except Exception as e:
            error_msg = f"Failed to send command: {e}"
            print(error_msg)
            self._add_event('stderr', error_msg, 'system')
            return False

    async def execute_command(
        self,
        command: str,
        source: str = 'assistant',
        timeout: float = 5.0,
    ) -> list[ShellEvent]:
        """
        Send a command and wait for output (blocking with timeout).

        Use this for quick commands where you want immediate results.
        Preserves any previous unread events and returns them along with new output.

        Args:
            command: The command to execute
            source: Who is sending the command ('user' or 'assistant')
            timeout: Maximum seconds to wait for output

        Returns:
            List of ShellEvent objects: previous unread events + new command output

        Example:
            events = await shell.execute_command("whoami", timeout=2.0)
            for event in events:
                if event.event_type == 'stdout':
                    print(event.content)
        """
        # Preserve any existing unread events (don't lose context!)
        async with self._buffer_lock:
            previous_events = self._unread_events.copy()
            self._unread_events.clear()

        # Send the command
        success = await self.send_command(command, source)
        if not success:
            return previous_events  # Return what we had before

        # Wait for output
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            pass

        # Retrieve new events collected during the wait
        new_events = await self.get_output()

        # Return both: previous context + new output
        return previous_events + new_events

    @abstractmethod
    async def terminate(self):
        """Gracefully terminate the shell subprocess."""
        self.running = False

    @property
    def is_running(self) -> bool:
        """Check if the shell process is currently running."""
        return (
            self.process is not None
            and self.process.returncode is None
            and self.running
        )
