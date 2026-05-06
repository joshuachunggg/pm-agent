#!/usr/bin/env python3
"""
Airtable write rollback tool. Never called autonomously by the agent.

Usage:
    python execution/tools/rollback.py --last 5
    python execution/tools/rollback.py --last 5 --apply
    python execution/tools/rollback.py --since "2025-05-05T09:00:00Z" --apply
    python execution/tools/rollback.py --trigger "morning_briefing_2025-05-05T09:00" --apply
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from pyairtable import Api

_project_root = Path(__file__).parent.parent.parent
load_dotenv(_project_root / ".env")

_WRITE_LOG = _project_root / ".tmp" / "write_log.jsonl"


def load_log():
    if not _WRITE_LOG.exists():
        return []
    entries = []
    with open(_WRITE_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def save_log(entries):
    with open(_WRITE_LOG, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def filter_entries(entries, last=None, since=None, trigger=None):
    active = [e for e in entries if not e.get("rolled_back") and not e.get("dry_run")]
    if last is not None:
        active = active[-last:]
    if since is not None:
        cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
        active = [
            e for e in active
            if datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) >= cutoff
        ]
    if trigger is not None:
        active = [e for e in active if e.get("trigger") == trigger]
    return active


def do_rollback(to_rollback, all_entries, apply=False):
    if apply:
        api_key = os.getenv("AIRTABLE_API_KEY")
        base_id = os.getenv("AIRTABLE_BASE_ID")
        if not api_key or not base_id:
            print("ERROR: AIRTABLE_API_KEY and AIRTABLE_BASE_ID required", file=sys.stderr)
            sys.exit(1)
        api = Api(api_key)

    now = datetime.now(timezone.utc).isoformat()
    rollback_keys = set()

    for entry in reversed(to_rollback):
        table = entry["table"]
        record_id = entry["record_id"]
        field = entry["field"]
        old_val = entry["old_value"]
        new_val = entry["new_value"]
        key = (entry.get("ts"), record_id, field)
        print(f"  {'APPLY' if apply else 'DRY RUN'}: {table}/{record_id}  {field}: {new_val!r} → {old_val!r}")
        if apply:
            t = api.table(base_id, table)
            t.update(record_id, {field: old_val})
            rollback_keys.add(key)

    if apply:
        for entry in all_entries:
            key = (entry.get("ts"), entry.get("record_id"), entry.get("field"))
            if key in rollback_keys:
                entry["rolled_back"] = True
                entry["rolled_back_at"] = now
        save_log(all_entries)


def main():
    parser = argparse.ArgumentParser(description="Roll back Airtable writes from the agent write log")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--last", type=int, metavar="N", help="Roll back last N writes")
    group.add_argument("--since", metavar="ISO_TIMESTAMP", help="Roll back writes since timestamp")
    group.add_argument("--trigger", metavar="TRIGGER_ID", help="Roll back writes from a specific trigger run")
    parser.add_argument("--apply", action="store_true", help="Execute the rollback (default: dry run)")
    args = parser.parse_args()

    entries = load_log()
    if not entries:
        print("Write log is empty — nothing to roll back.")
        return

    to_rollback = filter_entries(entries, last=args.last, since=args.since, trigger=args.trigger)
    if not to_rollback:
        print("No matching entries to roll back.")
        return

    print(f"\nEntries to roll back ({len(to_rollback)}):")
    do_rollback(to_rollback, entries, apply=args.apply)

    if args.apply:
        print(f"\nRolled back {len(to_rollback)} writes. Log updated.")
    else:
        print(f"\nDry run complete. Pass --apply to execute.")


if __name__ == "__main__":
    main()
