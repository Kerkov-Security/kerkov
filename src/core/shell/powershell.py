import asyncio
import sys
from typing import Optional, Callable, Awaitable

from src.core.shell.base_shell import Shell

class PowerShell(Shell):
    """PowerShell shell implementation for Windows."""

    def __init__(
        self,
        output_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        super().__init__(output_callback)
        self.ps_exec = "powershell.exe" if sys.platform == "win32" else "pwsh"

    def get_info(self) -> dict[str, str]:
        return {
            'shell_type': self.__class__.__name__,
            'shell_process_name': self.ps_exec,
            'is_running': self.is_running,
            'unread_event_count': len(self._unread_events),
            'total_event_count': len(self._event_buffer),
        }

    async def spawn(self) -> bool:
        """
        Spawn a PowerShell subprocess in headless mode with captured input/output.

        Returns:
            True if successfully spawned, False otherwise
        """
        if self.process and self.process.returncode is None:
            print("PowerShell process already running")
            return False

        try:
            self.process = await asyncio.create_subprocess_exec(
                self.ps_exec,
                "-NoProfile",
                "-NoLogo",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            self.running = True

            self._start_listeners()

            self._add_event('stdout', f"PowerShell started ({self.ps_exec})", 'system')

            return True

        except Exception as e:
            error_msg = f"Failed to spawn PowerShell: {e}"
            print(error_msg)
            self._add_event('stderr', error_msg, 'system')
            return False

    async def terminate(self):
        """Gracefully terminate the PowerShell subprocess."""
        if not self.process:
            return

        self.running = False

        try:
            if self.process.stdin:
                self.process.stdin.write(b"exit\n")
                await self.process.stdin.drain()

            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
                self._add_event('stdout', 'PowerShell terminated gracefully', 'system')
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
                self._add_event('stdout', 'PowerShell terminated forcefully', 'system')

        except Exception as e:
            error_msg = f"Error during termination: {e}"
            print(error_msg)
            self._add_event('stderr', error_msg, 'system')
            if self.process.returncode is None:
                self.process.kill()

        finally:
            await self._cleanup_listeners()

            self.process = None
