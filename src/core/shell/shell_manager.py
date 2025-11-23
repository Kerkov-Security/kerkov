import sys
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass
from enum import Enum

from src.core.shell.base_shell import Shell
from src.core.shell.powershell import PowerShell
from src.core.shell.views.toggle_view import ToggleTerminalView

class ViewMode(Enum):
    """Different modes for displaying shell output in terminal views."""
    DETACHED = "detached"  # Shell runs in background, no visible output
    TOGGLE = "toggle"      # Full-screen toggle mode (/shell enter)
    SPLIT = "split"        # Split view with chat + terminal (future)

@dataclass
class ShellSpawnResult:
    """
    Result of attempting to spawn a shell.

    Attributes:
        success: Whether the shell was spawned successfully
        message: Additional context about the result
    """
    success: bool
    message: str

class ShellManager:
    """
    Manages shell subprocesses (PowerShell, bash, zsh).

    All shells run in headless mode. ViewMode controls how output is displayed.
    """

    def __init__(self):
        self.platform = sys.platform
        self.current_shell: Optional[Shell] = None
        self.view_mode: ViewMode = ViewMode.TOGGLE # TODO: Support other view modes. Toggle is the only currently supported

    def create_shell(
        self,
        output_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Optional[Shell]:
        """
        Create a platform-appropriate shell instance.

        Args:
            output_callback: Optional callback for real-time output

        Returns:
            Shell instance or None if platform not supported
        """
        if self.platform == 'win32':
            return PowerShell(output_callback=output_callback)
        elif self.platform == 'darwin':
            # TODO: Implement Zsh class
            return None
        elif self.platform == 'linux':
            # TODO: Implement Bash class
            return None
        else:
            return None

    async def start_shell(
        self,
        output_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> ShellSpawnResult:
        """
        Start a platform-appropriate shell subprocess.

        Args:
            output_callback: Optional async callback for real-time output

        Returns:
            ShellSpawnResult with success status and message
        """
        if self.current_shell and self.current_shell.is_running:
            return ShellSpawnResult(
                success=False,
                message="A shell is already running",
            )

        self.current_shell = self.create_shell(output_callback=output_callback)

        if self.current_shell is None:
            return ShellSpawnResult(
                success=False,
                message=f"No shell implementation for platform '{self.platform}'",
            )

        spawn_success = await self.current_shell.spawn()

        if not spawn_success:
            shell_type = type(self.current_shell).__name__
            self.current_shell = None
            return ShellSpawnResult(
                success=False,
                message=f"Failed to spawn {shell_type}",
            )

        shell_type = type(self.current_shell).__name__
        return ShellSpawnResult(
            success=True,
            message=f"{shell_type} spawned successfully",
        )

    async def terminate_shell(self):
        """Terminate the current shell subprocess."""
        if self.current_shell:
            await self.current_shell.terminate()
            self.current_shell = None

    async def is_shell_running(self):
        return self.current_shell and self.current_shell.is_running

    async def set_view_mode(self, mode: ViewMode):
        """
        Set the terminal view mode.

        Args:
            mode: The ViewMode to use
        """
        self.view_mode = mode

    async def enter_toggle_mode(self):
        """
        Convenience method to enter interactive toggle mode.

        User can type commands directly in the shell. Exit with Ctrl+D or 'exit'.

        Raises:
            RuntimeError: If no shell is currently running

        Example:
            manager = ShellManager()
            await manager.start_shell()
            await manager.enter_toggle_mode()  # User interacts here
        """
        if not self.current_shell or not self.current_shell.is_running:
            raise RuntimeError("No shell is running. Call start_shell() first.")

        view = ToggleTerminalView(self.current_shell)
        await view.enter_interactive_mode()
