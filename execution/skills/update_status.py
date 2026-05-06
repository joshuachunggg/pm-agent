import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.airtable import AirtableClient
from lib.state import load_state, save_state, was_actioned, mark_actioned

logger = logging.getLogger(__name__)


def run(
    video_record_id: str,
    new_status: str,
    reason: str,
    trigger: str = None,
    dry_run: bool = False,
) -> dict:
    try:
        state = load_state()

        if was_actioned(video_record_id, state):
            return {
                "ok": True,
                "data": {
                    "record_id": video_record_id,
                    "new_status": new_status,
                    "dry_run": dry_run,
                    "skipped": True,
                    "skip_reason": "already actioned",
                },
                "summary": f"Skipped {video_record_id}: already actioned.",
                "actions_needed": [],
            }

        result = AirtableClient().update_record(
            "Videos",
            video_record_id,
            {"Editing Status": new_status},
            dry_run=dry_run,
            trigger=trigger,
            reason=reason,
        )

        writes_disabled = result.get("skipped") and result.get("reason") == "writes_disabled"
        skipped = writes_disabled
        skip_reason = "writes_disabled" if writes_disabled else None

        if not dry_run and not skipped:
            state = mark_actioned(video_record_id, state)
            save_state(state)

        return {
            "ok": True,
            "data": {
                "record_id": video_record_id,
                "new_status": new_status,
                "dry_run": dry_run,
                "skipped": skipped,
                "skip_reason": skip_reason,
            },
            "summary": (
                f"[DRY RUN] Would update {video_record_id} → '{new_status}'."
                if dry_run
                else (
                    f"Skipped {video_record_id}: {skip_reason}."
                    if skipped
                    else f"Updated {video_record_id} → '{new_status}'."
                )
            ),
            "actions_needed": [],
        }

    except Exception as e:
        return {
            "ok": False,
            "data": {},
            "summary": f"update_status failed: {e}",
            "actions_needed": [],
        }
