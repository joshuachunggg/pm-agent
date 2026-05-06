"""
checkout.py — EOD checkout data-gathering skill.

Deterministic (no LLM calls). Gathers today's key data points from Airtable
for the end-of-day checkout: QC transitions, scheduling, client sends,
clients needing footage, and close deadlines.

Interface:
    run(days=1) -> dict
"""

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.airtable import AirtableClient
from constants import (
    QC_STATUSES,
    POST_DEADLINE_STATUSES,
    INACTIVE_CLIENT_STATUSES,
    ALL_ACTIVE_STATUSES,
)

logger = logging.getLogger(__name__)

# Videos overdue by more than this many days are stale — skip from close deadlines
OVERDUE_CUTOFF_DAYS = 3

# Status 70+ means QC was cleared
_QC_CLEARED_THRESHOLD = 70


def _today_str(days: int = 1) -> str:
    """Return the ISO date string for the lookback start date.

    days=1 → today only.
    For now only today is implemented; the parameter is reserved.
    """
    return (date.today() - timedelta(days=days - 1)).isoformat()


def _parse_modified_date(value: str):
    """Parse Last Modified (Editing Status) to a date, or None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _resolve_client_name(fields: dict, client_map: dict) -> str:
    client_ids = fields.get("Client", [])
    if client_ids:
        cid = client_ids[0] if isinstance(client_ids, list) else client_ids
        return client_map.get(cid, "Unknown")
    return "Unknown"


def _is_inactive(fields: dict, client_map: dict, inactive_names: set) -> bool:
    name = _resolve_client_name(fields, client_map).lower()
    return name in inactive_names


def _video_ref(fields: dict, client_map: dict) -> str:
    return AirtableClient.format_video_ref(fields, client_map)


def _status_number(status: str) -> int:
    """Extract the leading numeric code from a status string, or 0 if none."""
    try:
        return int(status.split(" - ")[0].strip())
    except (ValueError, IndexError, AttributeError):
        return 0


def run(days: int = 1) -> dict:
    """Gather EOD checkout data from Airtable.

    Args:
        days: Lookback window in days (1 = today only). Future use.

    Returns:
        {
            "ok": bool,
            "data": {
                "qcs_cleared": [str],
                "qcs_pending": [str],
                "scheduled_today": [str],
                "needs_scheduling": [str],
                "sent_to_client": [str],
                "clients_needing_footage": [str],
                "close_deadlines": [
                    {"video_ref": str, "deadline": str, "status": str, "days_remaining": int}
                ],
                "summary_date": str,
            },
            "summary": str,
            "actions_needed": [],
        }
    """
    try:
        client = AirtableClient()
        today = date.today()
        today_str = today.isoformat()

        # ------------------------------------------------------------------
        # 1. Build client map and inactive set
        # ------------------------------------------------------------------
        clients_raw = client.fetch_table("Clients", fields=["Name", "Status"])
        client_map = {r["id"]: r["fields"].get("Name", "Unknown") for r in clients_raw}
        inactive_names = {
            r["fields"].get("Name", "").lower()
            for r in clients_raw
            if r["fields"].get("Status", "") in INACTIVE_CLIENT_STATUSES
        }

        # ------------------------------------------------------------------
        # 2. QCs cleared today: status changed today AND current status >= 70
        #    Also fetch status 60 videos (pending) with Last Modified today to
        #    detect cleared ones, plus current QC queue.
        # ------------------------------------------------------------------
        # Fetch all videos that are currently at 60 OR have recently moved past 60
        # We query status 70+ that were modified today as "cleared",
        # and status 60 as "pending".

        # Cleared: status 70+ with Last Modified today
        cleared_statuses_filter = (
            "OR("
            "{Editing Status}='70 - Approved By Agency',"
            "{Editing Status}='75 - Sent to Client For Review',"
            "{Editing Status}='80 - Approved By Client',"
            "{Editing Status}='90 - Approved But On Hold',"
            "{Editing Status}='100 - Scheduled - DONE'"
            ")"
        )
        cleared_records = client.fetch_table(
            "Videos",
            fields=[
                "Video Number", "Client", "Editing Status", "Format",
                "Assigned Editor", "Editor's Name",
                "Last Modified (Editing Status)",
            ],
            filter_formula=cleared_statuses_filter,
        )

        qcs_cleared = []
        for r in cleared_records:
            fields = r["fields"]
            if _is_inactive(fields, client_map, inactive_names):
                continue
            mod_date = _parse_modified_date(fields.get("Last Modified (Editing Status)", ""))
            if mod_date and str(mod_date) == today_str:
                status_num = _status_number(fields.get("Editing Status", ""))
                if status_num >= _QC_CLEARED_THRESHOLD:
                    qcs_cleared.append(_video_ref(fields, client_map))

        # Pending: currently at status 60
        qc_filter = (
            "OR("
            + ",".join(f"{{Editing Status}}='{s}'" for s in QC_STATUSES)
            + ")"
        )
        qc_records = client.fetch_table(
            "Videos",
            fields=[
                "Video Number", "Client", "Editing Status", "Format",
                "Assigned Editor", "Editor's Name",
                "Last Modified (Editing Status)",
            ],
            filter_formula=qc_filter,
        )

        qcs_pending = []
        for r in qc_records:
            fields = r["fields"]
            if _is_inactive(fields, client_map, inactive_names):
                continue
            qcs_pending.append(_video_ref(fields, client_map))

        # ------------------------------------------------------------------
        # 3. Scheduled today: status 100, Last Modified today
        # ------------------------------------------------------------------
        sched_records = client.fetch_table(
            "Videos",
            fields=[
                "Video Number", "Client", "Editing Status", "Format",
                "Last Modified (Editing Status)",
            ],
            filter_formula="{Editing Status}='100 - Scheduled - DONE'",
        )

        scheduled_today = []
        for r in sched_records:
            fields = r["fields"]
            if _is_inactive(fields, client_map, inactive_names):
                continue
            mod_date = _parse_modified_date(fields.get("Last Modified (Editing Status)", ""))
            if mod_date and str(mod_date) == today_str:
                scheduled_today.append(_video_ref(fields, client_map))

        # ------------------------------------------------------------------
        # 4. Needs scheduling: status 80
        # ------------------------------------------------------------------
        needs_sched_records = client.fetch_table(
            "Videos",
            fields=["Video Number", "Client", "Editing Status", "Format"],
            filter_formula="{Editing Status}='80 - Approved By Client'",
        )

        needs_scheduling = []
        for r in needs_sched_records:
            fields = r["fields"]
            if _is_inactive(fields, client_map, inactive_names):
                continue
            needs_scheduling.append(_video_ref(fields, client_map))

        # ------------------------------------------------------------------
        # 5. Sent to client today: status 75, Last Modified today
        # ------------------------------------------------------------------
        sent_records = client.fetch_table(
            "Videos",
            fields=[
                "Video Number", "Client", "Editing Status", "Format",
                "Last Modified (Editing Status)",
            ],
            filter_formula="{Editing Status}='75 - Sent to Client For Review'",
        )

        sent_to_client = []
        for r in sent_records:
            fields = r["fields"]
            if _is_inactive(fields, client_map, inactive_names):
                continue
            mod_date = _parse_modified_date(fields.get("Last Modified (Editing Status)", ""))
            if mod_date and str(mod_date) == today_str:
                sent_to_client.append(_video_ref(fields, client_map))

        # ------------------------------------------------------------------
        # 6. Clients needing footage: active clients with 0 active videos
        # ------------------------------------------------------------------
        # Fetch all active videos
        active_statuses_filter = (
            "OR("
            + ",".join(f"{{Editing Status}}='{s}'" for s in ALL_ACTIVE_STATUSES)
            + ")"
        )
        active_records = client.fetch_table(
            "Videos",
            fields=["Client", "Editing Status"],
            filter_formula=active_statuses_filter,
        )

        # Build set of client IDs that have at least one active video
        client_ids_with_active = set()
        for r in active_records:
            client_ids = r["fields"].get("Client", [])
            if isinstance(client_ids, list):
                client_ids_with_active.update(client_ids)
            elif client_ids:
                client_ids_with_active.add(client_ids)

        clients_needing_footage = []
        for record_id, name in client_map.items():
            if name.lower() in inactive_names:
                continue
            if record_id not in client_ids_with_active:
                clients_needing_footage.append(name)

        clients_needing_footage.sort()

        # ------------------------------------------------------------------
        # 7. Close deadlines: deadline within 2 days, not in post-deadline statuses
        #    Skip videos overdue by more than OVERDUE_CUTOFF_DAYS (stale).
        # ------------------------------------------------------------------
        deadline_filter = (
            "OR("
            + ",".join(
                f"{{Editing Status}}='{s}'"
                for s in ALL_ACTIVE_STATUSES
                if s not in POST_DEADLINE_STATUSES
            )
            + ")"
        )
        deadline_records = client.fetch_table(
            "Videos",
            fields=[
                "Video Number", "Client", "Editing Status", "Format",
                "Assigned Editor", "Editor's Name", "Deadline",
            ],
            filter_formula=deadline_filter,
        )

        close_deadlines = []
        for r in deadline_records:
            fields = r["fields"]
            if _is_inactive(fields, client_map, inactive_names):
                continue

            deadline_str = fields.get("Deadline", "")
            if not deadline_str:
                continue

            try:
                dl = datetime.strptime(deadline_str[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            days_remaining = (dl - today).days

            # Skip if overdue by more than cutoff (stale)
            if days_remaining < -OVERDUE_CUTOFF_DAYS:
                continue

            # Only include if deadline is within next 2 days or overdue
            if days_remaining > 2:
                continue

            status = fields.get("Editing Status", "")
            close_deadlines.append({
                "video_ref": _video_ref(fields, client_map),
                "deadline": dl.isoformat(),
                "status": status,
                "days_remaining": days_remaining,
            })

        close_deadlines.sort(key=lambda x: x["days_remaining"])

        # ------------------------------------------------------------------
        # Build summary
        # ------------------------------------------------------------------
        parts = []
        if qcs_cleared:
            parts.append(f"{len(qcs_cleared)} QC(s) cleared")
        if qcs_pending:
            parts.append(f"{len(qcs_pending)} QC(s) pending")
        if scheduled_today:
            parts.append(f"{len(scheduled_today)} scheduled today")
        if needs_scheduling:
            parts.append(f"{len(needs_scheduling)} need scheduling")
        if sent_to_client:
            parts.append(f"{len(sent_to_client)} sent to client")
        if clients_needing_footage:
            parts.append(f"{len(clients_needing_footage)} client(s) need footage")
        if close_deadlines:
            parts.append(f"{len(close_deadlines)} close deadline(s)")
        summary = ", ".join(parts) if parts else "No notable activity today"

        return {
            "ok": True,
            "data": {
                "qcs_cleared": qcs_cleared,
                "qcs_pending": qcs_pending,
                "scheduled_today": scheduled_today,
                "needs_scheduling": needs_scheduling,
                "sent_to_client": sent_to_client,
                "clients_needing_footage": clients_needing_footage,
                "close_deadlines": close_deadlines,
                "summary_date": today_str,
            },
            "summary": summary,
            "actions_needed": [],
        }

    except Exception as e:
        logger.exception("checkout skill failed")
        return {
            "ok": False,
            "data": {},
            "summary": f"checkout skill failed: {e}",
            "actions_needed": [],
        }
