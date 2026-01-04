"""
Demo for Toggle Mode - Interactive shell interface.

This demonstrates how users can enter an interactive shell mode
where they can type commands directly and see output in real-time.
"""

import asyncio

from src.core.shell import ShellManager


async def demo_toggle_mode():
    """
    Demonstrate toggle mode functionality.

    User Flow:
    1. Shell spawns in background (headless/detached mode)
    2. User enters toggle mode for interactive access
    3. User types commands directly (like whoami, dir/ls, etc.)
    4. Output appears in real-time
    5. User exits with Ctrl+D or 'exit'
    6. Returns to detached mode (or terminate)
    """
    print("=" * 60)
    print("  TOGGLE MODE DEMO")
    print("=" * 60)
    print()

    # Create manager and start shell
    manager = ShellManager()

    print("[1] Starting shell in detached mode...")
    result = await manager.start_shell()

    if not result.success:
        print(f"[ERROR] {result.message}")
        return

    print(f"[OK] {result.message}")
    print()

    # Demonstrate detached mode first
    print("[2] Detached mode: Running commands programmatically...")
    print("    Executing: whoami")

    events = await manager.current_shell.execute_command("whoami", timeout=2.0)

    for event in events:
        if event.event_type == 'stdout' and event.content.strip():
            print(f"    Output: {event.content}")

    print()
    print("[3] Now entering TOGGLE MODE for interactive access...")
    print("    You can type commands directly!")
    print("    Try: whoami, dir (Windows) or ls (Linux), echo 'hello'")
    print("    Exit with Ctrl+D or type 'exit'")
    print()

    input("Press Enter to enter toggle mode...")

    # Enter toggle mode - user interacts here
    await manager.enter_toggle_mode()

    # Back from toggle mode
    print("[4] Back in detached mode")
    print("    Shell is still running, can continue programmatically...")

    # Show history
    print("\n[5] Viewing shell history...")
    history = await manager.current_shell.get_history(limit=15)
    

    user_commands = [e for e in history if e.event_type == 'command' and e.source == 'user']
    assistant_commands = [e for e in history if e.event_type == 'command' and e.source == 'assistant']

    print(f"    User commands: {len(user_commands)}")
    print(f"    Assistant commands: {len(assistant_commands)}")

    if user_commands:
        print("\n    Commands you typed:")
        for cmd in user_commands:
            print(f"      - {cmd.content}")

    # Cleanup
    print("\n[6] Terminating shell...")
    await manager.terminate_shell()

    print("\n" + "=" * 60)
    print("  DEMO COMPLETE")
    print("=" * 60)


async def quick_toggle_demo():
    """
    Quick demo - goes straight to toggle mode.
    """
    manager = ShellManager()

    print("Starting shell...")
    result = await manager.start_shell()

    if not result.success:
        print(f"Error: {result.message}")
        return

    print(f"{result.message}\n")
    print("Entering toggle mode...")
    print("(Type commands, Ctrl+D to exit)\n")

    await asyncio.sleep(1)

    # Enter toggle mode immediately
    await manager.enter_toggle_mode()

    # Cleanup
    await manager.terminate_shell()
    print("Shell terminated.")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "quick":
        # Quick mode: python demo_toggle_mode.py quick
        asyncio.run(quick_toggle_demo())
    else:
        # Full demo
        asyncio.run(demo_toggle_mode())
