"""
editors skill — distills editor pipeline status from Airtable + Slack.

Orchestrator interface: run(editor=None, hours=48) -> dict
"""

import logging
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.airtable import AirtableClient
from lib.slack import SlackClient
from constants import EDITOR_ACTIVE_STATUSES, HEAVY_LOAD_THRESHOLD, POST_DEADLINE_STATUSES

logger = logging.getLogger(__name__)

# Fields to fetch from Videos table
_VIDEO_FIELDS = [
    "Video Number",
    "Format",
    "Client",
    "Editing Status",
    "Assigned Editor",
    "Deadline",
]


def _build_status_filter(statuses):
    clauses = ",".join(f"{{Editing Status}}='{s}'" for s in statuses)
    return f"OR({clauses})"


def _parse_deadline(deadline_str):
    if not deadline_str:
        return None
    try:
        return datetime.strptime(deadline_str[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _is_overdue(deadline_str, status):
    if status in POST_DEADLINE_STATUSES:
        return False
    d = _parse_deadline(deadline_str)
    if d is None:
        return False
    return d < date.today()


def _channel_name_to_editor(channel_name, first_name_lookup):
    """Match a *-editing channel name to an Airtable team member name."""
    stem = channel_name.replace("-editing", "")  # e.g. "seba", "golden-2"

    # Direct match
    if stem in first_name_lookup:
        return first_name_lookup[stem]

    # Partial / abbreviated match (seba -> Sebastian, syed-n -> Syed N, etc.)
    stem_normalized = stem.replace("-", " ")
    for first, full_name in first_name_lookup.items():
        if first.startswith(stem_normalized.split()[0]) and len(stem_normalized.split()[0]) >= 3:
            return full_name
        if stem_normalized.startswith(first) and len(first) >= 3:
            return full_name

    return None  # no match — caller uses stem as fallback


def _hours_since(timestamp_str):
    """Return hours since a Slack message timestamp. None if unparseable."""
    if not timestamp_str:
        return None
    try:
        ts = float(timestamp_str)
        return (datetime.now() - datetime.fromtimestamp(ts)).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def run(editor: str = None, hours: int = 48) -> dict:
    """Return structured editor pipeline status.

    Args:
        editor: If provided, filter to this editor only (case-insensitive name match).
        hours:  Slack activity window in hours (default 48).

    Returns:
        {
            "ok": bool,
            "data": {
                "editors": [...],
                "silent_editors": [str],
                "overdue_count": int,
                "heavy_load_editors": [str],
            },
            "summary": str,
            "actions_needed": [{"priority": str, "action": str, "editor": str}],
        }
    """
    try:
        at = AirtableClient()
        sl = SlackClient()

        # ------------------------------------------------------------------ #
        # 1. Discover editor channels from Slack                              #
        # ------------------------------------------------------------------ #
        raw_channels = sl.list_channels(filter_regex=r"-editing$")
        editing_channels = [ch for ch in raw_channels if not ch.get("is_archived", False)]
        logger.debug("Found %d active *-editing channels", len(editing_channels))

        # ------------------------------------------------------------------ #
        # 2. Build first-name → full-name lookup from Airtable Team table    #
        # ------------------------------------------------------------------ #
        editor_map = at.get_editor_map()  # {record_id: full_name}
        first_name_lookup = {}
        for full_name in editor_map.values():
            first = full_name.strip().split()[0].lower()
            first_name_lookup[first] = full_name

        # Map channel_id → editor_name
        channel_to_editor = {}  # channel_id → str
        editor_to_channel = {}  # editor_name_lower → {id, name}
        for ch in editing_channels:
            matched = _channel_name_to_editor(ch["name"], first_name_lookup)
            if matched:
                key = matched.lower()
            else:
                key = ch["name"].replace("-editing", "").replace("-", " ")
                matched = key.title()
            channel_to_editor[ch["id"]] = matched
            editor_to_channel[key] = {"id": ch["id"], "name": ch["name"]}

        # ------------------------------------------------------------------ #
        # 3. Fetch active videos from Airtable                               #
        # ------------------------------------------------------------------ #
        filter_formula = _build_status_filter(EDITOR_ACTIVE_STATUSES)
        video_records = at.fetch_table(
            "Videos",
            fields=_VIDEO_FIELDS,
            filter_formula=filter_formula,
        )
        logger.debug("Fetched %d active video records", len(video_records))

        # Build client map for format_video_ref
        client_map = at.get_client_map()

        # Group videos by editor name (lower)
        videos_by_editor = {}  # editor_name_lower → [video_dict]
        for rec in video_records:
            fields = rec["fields"]
            editor_name = at.resolve_editor_name(fields, editor_map)
            editor_key = editor_name.lower()
            video_ref = at.format_video_ref(fields, client_map)
            deadline_str = fields.get("Deadline")
            status = fields.get("Editing Status", "")
            video_dict = {
                "record_id": rec["id"],
                "video_ref": video_ref,
                "status": status,
                "deadline": deadline_str,
                "is_overdue": _is_overdue(deadline_str, status),
            }
            videos_by_editor.setdefault(editor_key, []).append(video_dict)

        # ------------------------------------------------------------------ #
        # 4. Determine editor scope (apply filter if provided)               #
        # ------------------------------------------------------------------ #
        all_editor_keys = set(editor_to_channel.keys()) | set(videos_by_editor.keys())

        if editor:
            target = editor.lower()
            all_editor_keys = {k for k in all_editor_keys if target in k}

        # ------------------------------------------------------------------ #
        # 5. Fetch Slack activity and build per-editor records               #
        # ------------------------------------------------------------------ #
        editors_out = []

        for editor_key in sorted(all_editor_keys):
            active_videos = videos_by_editor.get(editor_key, [])
            channel_info = editor_to_channel.get(editor_key)

            if channel_info is None:
                # Editor has Airtable videos but no editing channel — skip Slack
                recent_messages = []
                last_message_hours_ago = None
                is_silent = True
                channel_id = None
            else:
                channel_id = channel_info["id"]
                try:
                    recent_messages = sl.read_channel(channel_id, hours=hours, limit=20)
                except Exception as exc:
                    logger.warning("Could not read channel %s: %s", channel_id, exc)
                    recent_messages = []

                # Compute hours since last human (non-bot) message
                human_msgs = [
                    m for m in recent_messages
                    if "Send your *check in*" not in m.get("text", "")
                    and m.get("username", "") not in ("airtable2",)
                    and m.get("user", "").lower() != "airtable"
                ]
                if human_msgs:
                    latest_ts = max(
                        float(m.get("timestamp", 0)) for m in human_msgs
                    )
                    last_message_hours_ago = round(
                        (datetime.now() - datetime.fromtimestamp(latest_ts)).total_seconds() / 3600, 1
                    )
                    is_silent = last_message_hours_ago > hours
                else:
                    last_message_hours_ago = None
                    is_silent = True

            overdue_videos = [v for v in active_videos if v["is_overdue"]]
            video_count = len(active_videos)
            heavy_load = video_count >= HEAVY_LOAD_THRESHOLD

            # Best-effort display name
            display_name = editor_key.title()
            # Use canonical name from editor_map if available
            for rec_id, full_name in editor_map.items():
                if full_name.lower() == editor_key:
                    display_name = full_name
                    break

            editors_out.append({
                "name": display_name,
                "channel_id": channel_id,
                "active_videos": active_videos,
                "overdue_videos": overdue_videos,
                "recent_messages": recent_messages,
                "last_message_hours_ago": last_message_hours_ago,
                "is_silent": is_silent,
                "video_count": video_count,
                "heavy_load": heavy_load,
            })

        # ------------------------------------------------------------------ #
        # 6. Aggregate stats                                                  #
        # ------------------------------------------------------------------ #
        silent_editors = [e["name"] for e in editors_out if e["is_silent"] and e["video_count"] > 0]
        overdue_count = sum(len(e["overdue_videos"]) for e in editors_out)
        heavy_load_editors = [e["name"] for e in editors_out if e["heavy_load"]]

        # ------------------------------------------------------------------ #
        # 7. Build actions_needed                                             #
        # ------------------------------------------------------------------ #
        actions_needed = []

        for e in editors_out:
            if e["is_silent"] and e["video_count"] > 0:
                actions_needed.append({
                    "priority": "high",
                    "action": f"No Slack activity in {hours}h — follow up with {e['name']}",
                    "editor": e["name"],
                })
            if e["heavy_load"]:
                actions_needed.append({
                    "priority": "medium",
                    "action": f"Heavy load ({e['video_count']} videos) — consider reassigning",
                    "editor": e["name"],
                })
            if e["overdue_videos"]:
                refs = ", ".join(v["video_ref"] for v in e["overdue_videos"][:3])
                actions_needed.append({
                    "priority": "high",
                    "action": f"Overdue past V1 deadline: {refs}",
                    "editor": e["name"],
                })

        # ------------------------------------------------------------------ #
        # 8. Summary                                                          #
        # ------------------------------------------------------------------ #
        active_editor_count = sum(1 for e in editors_out if e["video_count"] > 0)
        summary_parts = [
            f"{active_editor_count} editors with active videos"
            f" ({sum(e['video_count'] for e in editors_out)} total)."
        ]
        if silent_editors:
            summary_parts.append(f"Silent: {', '.join(silent_editors)}.")
        if overdue_count:
            summary_parts.append(f"{overdue_count} video(s) past V1 deadline.")
        if heavy_load_editors:
            summary_parts.append(f"Heavy load: {', '.join(heavy_load_editors)}.")
        summary = " ".join(summary_parts)

        return {
            "ok": True,
            "data": {
                "editors": editors_out,
                "silent_editors": silent_editors,
                "overdue_count": overdue_count,
                "heavy_load_editors": heavy_load_editors,
            },
            "summary": summary,
            "actions_needed": actions_needed,
        }

    except Exception as e:
        logger.exception("editors skill failed")
        return {
            "ok": False,
            "data": {},
            "summary": f"editors skill failed: {e}",
            "actions_needed": [],
        }
