#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VULHUB_ARG = "vulhub"
DEFAULT_OUTPUT_PATH = ROOT / "config" / "ci" / "vulhub_inventory.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.vulhub_inventory import build_vulhub_inventory, write_vulhub_inventory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a local Vulhub lab inventory")
    parser.add_argument(
        "--vulhub-dir",
        default=DEFAULT_VULHUB_ARG,
        help="Path to a local Vulhub checkout",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Where to write the generated inventory JSON",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the generated inventory JSON instead of writing it to disk",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_label = args.vulhub_dir
    vulhub_dir = Path(args.vulhub_dir).resolve()

    if not vulhub_dir.is_dir():
        raise RuntimeError(f"Expected Vulhub directory does not exist: {vulhub_dir}")

    inventory = build_vulhub_inventory(vulhub_dir, source_label=source_label)

    if args.stdout:
        print(json.dumps(inventory, indent=2))
        return 0

    output_path = Path(args.output).resolve()
    write_vulhub_inventory(output_path, inventory)
    summary = inventory["summary"]
    print(
        "Generated Vulhub inventory at "
        f"{output_path} with {summary['lab_count']} labs across {summary['product_count']} products."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
