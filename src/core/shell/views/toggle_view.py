"""
Toggle Terminal View - Interactive shell mode for user interaction.

Allows users to enter an interactive mode where they can type commands
directly into the shell and see real-time output.

.. deprecated::
    This module is deprecated and no longer in use. Do not add new dependencies on it.
"""

import warnings

warnings.warn(
    "src.core.shell.views.toggle_view is deprecated and no longer in use.",
    DeprecationWarning,
    stacklevel=2,
)

import asyncio
from typing import Optional

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False

from src.core.shell.base_shell import Shell

class ToggleTerminalView:
    """
    Interactive terminal view using toggle mode.

    User enters the shell interactively, types commands, sees output in real-time,
    and exits with Ctrl+D or typing 'exit'.
    """

    def __init__(self, shell: Shell):
        """
        Initialize toggle terminal view.

        Args:
            shell: The Shell instance to interact with
        """
        self.shell: Shell = shell
        self.running = False
        self._output_queue: asyncio.Queue = asyncio.Queue()
        self._printer_task: Optional[asyncio.Task] = None

    async def _output_callback(self, line: str):
        """
        Callback for real-time output streaming.

        This is called by the shell's stdout/stderr listeners.
        """
        await self._output_queue.put(line)

    async def _output_printer(self):
        """
        Background task that prints output as it arrives.
        """
        while self.running:
            try:
                line = await asyncio.wait_for(self._output_queue.get(), timeout=0.1)
                print(f"  {line}")
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[ERROR] Output printer: {e}")
                break

    async def enter_interactive_mode(self):
        """
        Enter interactive toggle mode.

        User can type commands directly. Exit with Ctrl+D or 'exit'.
        """
        if not self.shell.is_running:
            print("[ERROR] No shell is running. Start a shell first.")
            return

        self.shell.output_callback = self._output_callback
        self.running = True

        print("\n" + "=" * 60)
        print("  SHELL TOGGLE MODE")
        print("  Type commands below. Press Ctrl+D or type 'exit' to quit.")
        print("=" * 60 + "\n")

        self._printer_task = asyncio.create_task(self._output_printer())

        try:
            if PROMPT_TOOLKIT_AVAILABLE:
                try:
                    print("[INFO] Using prompt_toolkit for enhanced input\n")
                    await self._prompt_toolkit_loop()
                except Exception as e:
                    # Fallback to basic input if prompt_toolkit fails
                    print(f"[ERROR] prompt_toolkit error: {e}")
            else:
                print("[ERROR] prompt_toolkit not available")
                print("\nInstall prompt_toolkit to use an interactive terminal view")
        except KeyboardInterrupt:
            print("\n[INFO] Interrupted by user")
        finally:
            self.running = False

            await asyncio.sleep(0.2)
            if self._printer_task and not self._printer_task.done():
                self._printer_task.cancel()
                try:
                    await self._printer_task
                except asyncio.CancelledError:
                    pass

            self.shell.output_callback = None

            print("\n" + "=" * 60)
            print("  Exited Shell Toggle Mode")
            print("=" * 60 + "\n")

    async def _prompt_toolkit_loop(self):
        """
        Interactive loop using prompt_toolkit (better experience).
        """
        session = PromptSession()

        while self.running:
            try:
                with patch_stdout():
                    user_input = await session.prompt_async("  $ ")

                if user_input.lower() in ['exit', 'quit']:
                    break

                if not user_input.strip():
                    continue

                await self.shell.send_command(user_input, source='user')
                await asyncio.sleep(0.1)
            except EOFError:
                # Ctrl+D pres sed
                break
            except KeyboardInterrupt:
                # Ctrl+C pressed
                print("\n^C")
                continue
            except Exception as e:
                print(f"[ERROR] {e}")
                continue
