#!/usr/bin/env python3
import argparse
import logging
import os
import sys
from pathlib import Path

# resolve execution/ directory (parent of tools/)
_execution_dir = Path(__file__).parent.parent
sys.path.insert(0, str(_execution_dir))

from dotenv import load_dotenv
load_dotenv(_execution_dir.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

TRIGGERS = [
    "morning_briefing",
    "midday_crosscheck",
    "eod_checkout",
    "urgent_watch",
    "weekly_summary",
    "simon_query",
]


def main():
    parser = argparse.ArgumentParser(description="Fire a Samu agent trigger for E2E testing.")
    parser.add_argument("trigger", choices=TRIGGERS)
    parser.add_argument("--context", default="What's the current editor and client status?")
    parser.add_argument("--dry-run", action="store_true", help="Disable writes (read-only)")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["AGENT_WRITES_ENABLED"] = "false"
        print("[DRY RUN] Writes disabled.")

    import orchestrator

    print(f"\nFiring: {args.trigger}")
    if args.trigger == "simon_query":
        print(f"Context: {args.context}")
    print("-" * 40)

    result = orchestrator.run(args.trigger, context=args.context)

    print("\n--- Response to Simon ---")
    print(result)
    print("--- Done ---")


if __name__ == "__main__":
    main()
