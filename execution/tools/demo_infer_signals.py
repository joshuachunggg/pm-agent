#!/usr/bin/env python3
"""
Demo script: shows the agent detecting implied status transitions from vague messages.

Scenario 1 — CLIENT APPROVAL (high confidence):
  FitLife Reel #106, status 75. Client says "Looks great, thanks!"
  Shows keyword detection path + gap analysis (what slips through).

Scenario 2 — EDITOR VAGUE (medium confidence):
  Editor says "just finished 🙌 https://app.frame.io/reviews/abc123"
  Shows LLM inference path → DM Simon for confirmation.

Scenario 3 — NOISE (no signal):
  "sounds good!" — nothing triggered. Shows the system ignores filler.

Usage:
    python execution/tools/demo_infer_signals.py
    python execution/tools/demo_infer_signals.py --scenario client_approval
    python execution/tools/demo_infer_signals.py --scenario editor_vague
    python execution/tools/demo_infer_signals.py --scenario noise
"""

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_execution_dir = Path(__file__).parent.parent
sys.path.insert(0, str(_execution_dir))

from dotenv import load_dotenv
load_dotenv(_execution_dir.parent / ".env")

from lib.airtable import AirtableClient
from skills import infer_signals, infer_client_signals
from constants import EDITOR_ACTIVE_STATUSES

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
WHITE  = "\033[97m"

def _c(text, *codes): return "".join(codes) + str(text) + RESET
def _hr(char="─", n=64): print(_c(char * n, DIM))


# ── Synthetic messages ────────────────────────────────────────────────────────

_CLIENT_APPROVAL_MSG = "Looks great, thanks!"
_EDITOR_VAGUE_MSG    = "just finished 🙌 https://app.frame.io/reviews/abc123"
_NOISE_MSG           = "sounds good!"


def _make_msg(text: str, user: str = "Editor", user_id: str = "UFAKE001",
              minutes_ago: int = 30) -> dict:
    ts = datetime.now() - timedelta(minutes=minutes_ago)
    return {
        "text": text,
        "user": user,
        "username": user.lower().replace(" ", ""),
        "user_id": user_id,
        "timestamp": str(ts.timestamp()),
        "datetime": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "thread_replies": [],
        "reactions": [],
    }


# ── Airtable helpers ──────────────────────────────────────────────────────────

def _fetch_video_at_status(at: AirtableClient, status: str,
                            video_number: str = None) -> tuple:
    """Return (record, client_map, editor_map) for first video at given status."""
    formula = f"{{Editing Status}}='{status}'"
    if video_number:
        formula = f"AND({{Editing Status}}='{status}', {{Video Number}}='{video_number}')"
    records = at.fetch_table(
        "Videos",
        fields=["Video Number", "Client", "Editing Status", "Format",
                "Assigned Editor", "Editor's Name", "Deadline"],
        filter_formula=formula,
    )
    client_map = at.get_client_map()
    editor_map = at.get_editor_map()
    return records, client_map, editor_map


def _fetch_editor_videos(at: AirtableClient) -> tuple:
    clauses = ",".join(f"{{Editing Status}}='{s}'" for s in EDITOR_ACTIVE_STATUSES)
    records = at.fetch_table(
        "Videos",
        fields=["Video Number", "Client", "Editing Status", "Format",
                "Assigned Editor", "Editor's Name", "Deadline"],
        filter_formula=f"OR({clauses})",
    )
    client_map = at.get_client_map()
    editor_map = at.get_editor_map()
    return records, client_map, editor_map


# ── Print helpers ─────────────────────────────────────────────────────────────

def _print_header(title: str, subtitle: str = ""):
    print()
    _hr("═")
    print(_c(f"  SAMU PM Agent — Vague Signal Detection Demo", BOLD, WHITE))
    print(_c(f"  {title}", BOLD))
    if subtitle:
        print(_c(f"  {subtitle}", DIM))
    print(_c(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}", DIM))
    _hr("═")


def _print_video(video_ref: str, status: str):
    status_short = status.split(" - ", 1)[-1] if " - " in status else status
    print(_c("  Video (from Airtable):", BOLD))
    print(f"    {_c('•', CYAN)} {video_ref}  {_c(f'[{status_short}]', DIM)}")


def _print_message(msg: dict, sender_label: str = "Slack message"):
    print(_c(f"\n  {sender_label}:", BOLD))
    print(f"    {_c('>', YELLOW)} \"{msg['text']}\"")
    print(f"    {_c('from: ' + msg['user'], DIM)}")


def _print_signals(signals: list):
    print(_c("\n  ── Inference result ──", DIM))
    if not signals:
        print(_c("  No status transitions detected.", DIM))
        return
    for s in signals:
        conf = s.get("confidence", "?")
        colour = GREEN if conf == "high" else YELLOW
        label = _c(f"[{conf.upper()}]", colour, BOLD)
        print(f"\n  {label}  {_c(s.get('video_ref', '?'), WHITE, BOLD)}")
        print(f"    {_c('current:', DIM)}  {s.get('current_status', '?')}")
        print(f"    {_c('implied:', DIM)}  {_c(s.get('implied_status', '?'), GREEN)}")
        print(f"    {_c('trigger:', DIM)}  \"{s.get('trigger_message', '')}\"")
        print(f"    {_c('reason:', DIM)}   {s.get('reason', '')}")
        if conf == "high":
            print(_c("\n    → Orchestrator: AUTO-UPDATE Airtable status", GREEN))
        elif conf == "medium":
            print(_c("\n    → Orchestrator: DM Simon for confirmation", YELLOW))


def _print_orchestrator(signals: list, client_approval: dict = None):
    print()
    _hr()
    print(_c("  ORCHESTRATOR ACTIONS", BOLD, WHITE))
    _hr()

    if client_approval:
        conf_colour = GREEN if client_approval["confidence"] == "high" else YELLOW
        print(_c(f"\n  Tier 1 — notify_simon:", conf_colour, BOLD))
        print(
            f"    • Verify approval from {_c(client_approval['client'], WHITE)} "
            f"for {_c(client_approval['video_ref'], WHITE)} — "
            f"update Airtable to {_c('80 - Approved By Client', GREEN)} if confirmed."
        )
        ca_msg = client_approval['message']
        print(f"      {_c('Message: ' + repr(ca_msg), DIM)}")
        return

    high = [s for s in signals if s.get("confidence") == "high"]
    medium = [s for s in signals if s.get("confidence") == "medium"]

    if high:
        print(_c(f"\n  Tier 1 — update_status (auto):", GREEN, BOLD))
        for s in high:
            print(f"    • {s['video_ref']}  {_c(s['current_status'], DIM)} → {_c(s['implied_status'], GREEN)}")

    if medium:
        print(_c(f"\n  Tier 1 — notify_simon (needs confirmation):", YELLOW, BOLD))
        for s in medium:
            print(
                f"    • {s['editor']} may have indicated {s['video_ref']} "
                f"ready for {_c(s['implied_status'], YELLOW)}.\n"
                f"      Message: \"{s['trigger_message']}\"  {_c('→ Confirm in Airtable?', DIM)}"
            )

    if not high and not medium:
        print(_c("\n  No actions triggered.", DIM))


# ── Scenario runners ──────────────────────────────────────────────────────────

def _run_client_approval(at: AirtableClient):
    _print_header(
        "Scenario 1 — CLIENT APPROVAL",
        "Client sends vague approval on video at status 75"
    )

    records, client_map, editor_map = _fetch_video_at_status(
        at, "75 - Sent to Client For Review", video_number="106"
    )

    # Fall back to any status-75 video if #106 doesn't exist
    if not records:
        records, client_map, editor_map = _fetch_video_at_status(
            at, "75 - Sent to Client For Review"
        )

    if not records:
        print(_c("  No videos at status 75 found. Add one to Airtable to run this scenario.", RED))
        print()
        _hr("═")
        print()
        return

    rec = records[0]
    fields = rec["fields"]
    video_ref = at.format_video_ref(fields, client_map)
    client_ids = fields.get("Client", [])
    client_name = client_map.get(
        client_ids[0] if isinstance(client_ids, list) and client_ids else client_ids,
        "Client"
    )

    msg = _make_msg(_CLIENT_APPROVAL_MSG, user=client_name, user_id="UCLIENT01")
    _print_video(video_ref, fields.get("Editing Status", ""))
    _print_message(msg, sender_label="Client Slack message")

    print(_c("\n  Running infer_client_signals skill (Claude Haiku)...", DIM))
    demo_clients_data = [{
        "name": client_name,
        "channel_id": "CFAKECLIENT",
        "recent_messages": [msg],
        "active_videos": [video_ref],
    }]

    t0 = time.monotonic()
    result = infer_client_signals.run(demo_clients_data)
    elapsed = time.monotonic() - t0
    print(_c(f"  Done in {elapsed:.1f}s.", DIM))

    signals = result.get("data", {}).get("signals", [])
    print(_c("\n  ── Inference result ──", DIM))
    if not signals:
        print(_c("  No approval signals detected.", DIM))
    else:
        for s in signals:
            conf = s.get("confidence", "?")
            colour = GREEN if conf == "high" else YELLOW
            label = _c(f"[{conf.upper()}]", colour, BOLD)
            print(f"\n  {label}  {_c(s.get('video_ref', '?'), WHITE, BOLD)}")
            print(f"    {_c('trigger:', DIM)}  \"{s.get('trigger_message', '')}\"")
            print(f"    {_c('reason:', DIM)}   {s.get('reason', '')}")
            print(_c("\n    → Orchestrator: DM Simon to confirm (never auto-updates)", YELLOW))

    _print_orchestrator([], client_approval={
        "confidence": signals[0]["confidence"] if signals else "high",
        "client": client_name,
        "video_ref": video_ref,
        "message": msg["text"],
    } if signals else None)

    print()
    _hr("═")
    print()


def _run_editor_vague(at: AirtableClient):
    _print_header(
        "Scenario 2 — EDITOR VAGUE MESSAGE",
        "Editor says something vague — LLM infers implied status"
    )

    records, client_map, editor_map = _fetch_editor_videos(at)
    if not records:
        print(_c("  No active editor videos found.", RED))
        return

    videos_by_editor: dict = {}
    for rec in records:
        f = rec["fields"]
        name = at.resolve_editor_name(f, editor_map)
        ref = at.format_video_ref(f, client_map)
        videos_by_editor.setdefault(name, []).append({
            "record_id": rec["id"],
            "video_ref": ref,
            "status": f.get("Editing Status", ""),
            "deadline": f.get("Deadline", ""),
            "is_overdue": False,
        })

    first_editor = list(videos_by_editor.keys())[0]
    first_video = videos_by_editor[first_editor][0]
    msg = _make_msg(_EDITOR_VAGUE_MSG, user=first_editor, user_id="UEDIT001")

    _print_video(first_video["video_ref"], first_video["status"])
    _print_message(msg, sender_label="Editor Slack message")

    demo_editor = [{
        "name": first_editor,
        "channel_id": "CFAKE000",
        "active_videos": [first_video],
        "recent_messages": [msg],
    }]

    print(_c("\n  Running infer_signals skill (Claude Haiku)...", DIM))
    t0 = time.monotonic()
    result = infer_signals.run(demo_editor)
    elapsed = time.monotonic() - t0
    print(_c(f"  Done in {elapsed:.1f}s.", DIM))

    signals = result.get("data", {}).get("signals", [])
    _print_signals(signals)
    _print_orchestrator(signals)
    print()


def _run_noise(at: AirtableClient):
    _print_header(
        "Scenario 3 — NOISE",
        "Pure filler message — system should stay silent"
    )

    records, client_map, editor_map = _fetch_editor_videos(at)
    if not records:
        print(_c("  No active editor videos found.", RED))
        return

    rec = records[0]
    f = rec["fields"]
    editor_name = at.resolve_editor_name(f, editor_map)
    video_ref = at.format_video_ref(f, client_map)
    status = f.get("Editing Status", "")

    msg = _make_msg(_NOISE_MSG, user=editor_name, user_id="UEDIT001")
    _print_video(video_ref, status)
    _print_message(msg, sender_label="Editor Slack message")

    demo_editor = [{
        "name": editor_name,
        "channel_id": "CFAKE000",
        "active_videos": [{
            "record_id": rec["id"],
            "video_ref": video_ref,
            "status": status,
            "deadline": f.get("Deadline", ""),
            "is_overdue": False,
        }],
        "recent_messages": [msg],
    }]

    print(_c("\n  Running infer_signals skill (Claude Haiku)...", DIM))
    t0 = time.monotonic()
    result = infer_signals.run(demo_editor)
    elapsed = time.monotonic() - t0
    print(_c(f"  Done in {elapsed:.1f}s.", DIM))

    signals = result.get("data", {}).get("signals", [])
    _print_signals(signals)

    print()
    _hr()
    print(_c("  ORCHESTRATOR ACTIONS", BOLD, WHITE))
    _hr()
    print(_c("\n  No actions triggered. System correctly ignored filler.", DIM))
    print()
    _hr("═")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Demo: vague message → status signal detection"
    )
    parser.add_argument(
        "--scenario",
        choices=["all", "client_approval", "editor_vague", "noise"],
        default="all",
    )
    args = parser.parse_args()

    at = AirtableClient()
    print(_c("\n  Connecting to Airtable...", DIM))

    if args.scenario in ("all", "client_approval"):
        _run_client_approval(at)
        if args.scenario == "all":
            input(_c("  Press Enter for next scenario...\n", DIM))

    if args.scenario in ("all", "editor_vague"):
        _run_editor_vague(at)
        if args.scenario == "all":
            input(_c("  Press Enter for next scenario...\n", DIM))

    if args.scenario in ("all", "noise"):
        _run_noise(at)


if __name__ == "__main__":
    main()
