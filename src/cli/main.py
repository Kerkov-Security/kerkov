#!/usr/bin/env python3
"""
Kerkov Bot - Security-focused conversational AI CLI
Main entry point for the command-line interface
"""

import asyncio
import sys

from rich.console import Console

from src.cli.kerkov_cli import KerkovCLI

console = Console()


def main() -> None:
    """Main entry point for the CLI."""
    try:
        cli = KerkovCLI()
        asyncio.run(cli.run())
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/dim]")
    except Exception as e:
        console.print(f"[bold red]Fatal error:[/bold red] {str(e)}", style="bold red")
        sys.exit(1)


if __name__ == "__main__":
    main()
