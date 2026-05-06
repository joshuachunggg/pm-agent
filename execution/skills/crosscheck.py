"""
M2c crosscheck skill — nine cross-check operations between Slack and Airtable.

Interface:
    run(checks=None, hours=48) -> dict
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.airtable import AirtableClient
from lib.slack import SlackClient
from constants import (
    QC_STATUSES, STATUS_STALE_DAYS, THUMBNAIL_ACTIVE_STATUSES,
    APPROVAL_KEYWORDS, FOOTAGE_KEYWORDS,
    SAMU_SLACK_USER_ID, SIMON_SLACK_USER_ID,
    OPS_MANAGER_IDS, ALL_ACTIVE_STATUSES,
    POST_DEADLINE_STATUSES, INACTIVE_CLIENT_STATUSES,
)

logger = logging.getLogger(__name__)

ALL_CHECKS = [
    "new_footage", "client_approval", "thumbnail_blockers",
    "unanswered", "gaps", "stale", "deliverables", "assignments", "pm_tasks",
]

# ---------------------------------------------------------------------------
# PM task detection helpers (ported from monolith)
# ---------------------------------------------------------------------------

_TASK_SIGNAL_PHRASES = [
    "can you", "could you", "please", "pls", "make sure", "don't forget",
    "remember to", "need you to", "follow up", "check on",
    "reach out", "remind", "let me know",
    "when you get a chance",
    "priority for today", "priority is", "vid priority",
]

_IMPERATIVE_STARTERS = [
    "schedule ", "assign ", "send ", "add ", "fix ", "update ",
    "check ", "ping ", "reach ", "follow ", "remind ", "review ",
    "look ", "message ", "move ", "set ", "confirm ", "clear ",
    "handle ", "take ", "make ", "onboard ",
    "connect ", "coordinate ", "train ", "double check ", "double-check ",
]

_CLUSTER_WINDOW_SECONDS = 300
_INCHANNEL_REPLY_WINDOW = 600


def _looks_like_task(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("-----") or stripped.startswith("----"):
        return True
    if f"<@{SIMON_SLACK_USER_ID}>" in stripped:
        return True
    lower = text.lower()
    for phrase in _TASK_SIGNAL_PHRASES:
        if phrase in lower:
            return True
    for starter in _IMPERATIVE_STARTERS:
        if lower.startswith(starter):
            return True
    return False


def _simon_responded(msg, all_messages: list) -> bool:
    for reply in msg.get("thread_replies", []):
        if reply.get("user_id") == SIMON_SLACK_USER_ID:
            return True
    for reaction in msg.get("reactions", []):
        if SIMON_SLACK_USER_ID in reaction.get("users", []):
            return True
    msg_ts = float(msg.get("timestamp", "0"))
    window_end = msg_ts + _INCHANNEL_REPLY_WINDOW
    for other in all_messages:
        if other.get("user_id") != SIMON_SLACK_USER_ID:
            continue
        other_ts = float(other.get("timestamp", "0"))
        if msg_ts < other_ts <= window_end:
            return True
    return False


def _cluster_tasks(raw_tasks: list) -> list:
    if not raw_tasks:
        return []
    sorted_tasks = sorted(raw_tasks, key=lambda t: t["timestamp"])
    clusters: list = []
    current: list = [sorted_tasks[0]]
    for task in sorted_tasks[1:]:
        if task["timestamp"] - current[-1]["timestamp"] <= _CLUSTER_WINDOW_SECONDS:
            current.append(task)
        else:
            clusters.append(current)
            current = [task]
    clusters.append(current)

    result = []
    for cluster in clusters:
        best = None
        for msg in cluster:
            text = msg["text"]
            if f"<@{SIMON_SLACK_USER_ID}>" in text:
                best = msg
                break
            if text.strip().startswith("----"):
                best = msg
                break
        if not best:
            best = max(cluster, key=lambda m: len(m["text"]))
        result.append({
            "message": best["text"][:160].replace("|", "/"),
            "when": best["when"],
            "hours_ago": best["hours_ago"],
            "count": len(cluster),
        })
    result.sort(key=lambda t: t["hours_ago"])
    return result


# ---------------------------------------------------------------------------
# Unanswered detection helper
# ---------------------------------------------------------------------------

_RESPONSE_NEEDED_PATTERNS = [
    r"\?",
    r"\bcan you\b", r"\bcould you\b", r"\bwould you\b", r"\bplease\b",
    r"\bhelp\b", r"\bwhen\b.*\b(will|can|should)\b",
    r"\bneed\b", r"\bwaiting\b", r"\bstill\b",
    r"\bupdate\b", r"\bstatus\b", r"\bcheck\b",
    r"\bhave you\b", r"\bdid you\b", r"\bany news\b",
]
_RESPONSE_NEEDED_RE = re.compile("|".join(_RESPONSE_NEEDED_PATTERNS), re.IGNORECASE)


def _needs_response(text: str) -> bool:
    return bool(_RESPONSE_NEEDED_RE.search(text))


# ---------------------------------------------------------------------------
# Deliverables parser (ported from monolith)
# ---------------------------------------------------------------------------

def _parse_deliverables(raw_str):
    if not raw_str:
        return {"long_form": 0, "shorts": 0, "total": 0}
    text = str(raw_str).lower()
    text = re.sub(r"\b(720|1080|2160|4k)\w*", "", text)
    long_form = 0
    shorts = 0
    shorts_match = re.search(r"(\d+)\s*(?:short|shorts|short-form)", text)
    if shorts_match:
        shorts = int(shorts_match.group(1))
    long_match = re.search(r"(\d+)\s*(?:long-form|long|videos?|/mo)", text)
    if long_match:
        long_form = int(long_match.group(1))
    elif not shorts_match:
        package_keywords = re.search(r"video|/mo|per month|monthly|long-form|short-form", text)
        if package_keywords:
            nums = re.findall(r"\d+", text)
            if nums:
                long_form = int(nums[0])
    total = long_form + shorts
    return {"long_form": long_form, "shorts": shorts, "total": total}


# ---------------------------------------------------------------------------
# Channel matching helper
# ---------------------------------------------------------------------------

def _match_client_to_channel(client_name: str, client_channels: list):
    if not client_name:
        return None
    cn_lower = client_name.lower().strip()
    for ch in client_channels:
        ch_lower = ch["client_name"].lower().strip()
        if cn_lower == ch_lower or cn_lower in ch_lower or ch_lower in cn_lower:
            return ch
    return None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_new_footage(active_videos, client_map, client_channels, team_slack_ids,
                       hours, channel_cache, slack):
    clients_with_assignment = set()
    for v in active_videos:
        fields = v["fields"]
        status = fields.get("Editing Status", "")
        if status in ("40 - Client Sent Raw Footage", "41 - Sent to Editor"):
            client_ids = fields.get("Client", [])
            if client_ids:
                cid = client_ids[0] if isinstance(client_ids, list) else client_ids
                name = client_map.get(cid, "")
                if name:
                    clients_with_assignment.add(name.lower())

    findings = []
    for ch in client_channels:
        if ch["client_name"].lower() in clients_with_assignment:
            continue
        if channel_cache is not None:
            messages = channel_cache.get(ch["id"], [])
        else:
            try:
                messages = slack.read_channel(ch["id"], hours=hours, limit=100,
                                              include_threads=False)
            except Exception:
                continue

        latest = None
        for msg in messages:
            if team_slack_ids and msg.get("user_id", "") in team_slack_ids:
                continue
            text = msg.get("text", "").lower()
            for keyword in FOOTAGE_KEYWORDS:
                if keyword in text:
                    false_positive_patterns = [
                        "to the sponsor", "to sponsor", "to my sponsor",
                        "to the client", "to their team", "to my team",
                        "to the brand", "to my producer", "to their",
                        "waiting on them", "waiting for them", "waiting for approval",
                        "waiting on the", "for approval", "for their review",
                        "sponsor for", "approval from",
                    ]
                    if any(fp in text for fp in false_positive_patterns):
                        break
                    latest = msg
                    break

        if latest:
            findings.append({
                "client": ch["client_name"],
                "message": latest.get("text", "")[:120].replace("|", "/"),
                "when": latest.get("datetime", "")[:16],
                "channel": ch["name"],
            })

    return findings


def _check_client_approval(active_videos, client_map, client_channels, team_slack_ids,
                           hours, channel_cache, slack):
    client_videos_at_75: dict = {}
    for v in active_videos:
        fields = v["fields"]
        if fields.get("Editing Status", "") != "75 - Sent to Client For Review":
            continue
        client_ids = fields.get("Client", [])
        if not client_ids:
            continue
        cid = client_ids[0] if isinstance(client_ids, list) else client_ids
        name = client_map.get(cid, "")
        if not name:
            continue
        ref = AirtableClient.format_video_ref(fields, client_map)
        vid_num = str(fields.get("Video Number", ""))
        client_videos_at_75.setdefault(name, []).append({"ref": ref, "num": vid_num})

    if not client_videos_at_75:
        return []

    findings = []
    _team_ids = team_slack_ids or set()
    _question_frames = [
        "do we", "should we", "or we", "if we", "can we",
        "shall we", "would we", "could we",
        "do i", "should i", "or i", "if i", "can i",
    ]

    for client_name, video_info_list in client_videos_at_75.items():
        ch = _match_client_to_channel(client_name, client_channels)
        if not ch:
            continue

        if channel_cache is not None:
            messages = channel_cache.get(ch["id"], [])
        else:
            try:
                messages = slack.read_channel(ch["id"], hours=hours, limit=100,
                                              include_threads=True)
            except Exception:
                continue

        video_refs = [vi["ref"] for vi in video_info_list]

        def _check_msg(msg):
            if _team_ids and msg.get("user_id", "") in _team_ids:
                return False
            text = msg.get("text", "")
            text_lower = text.lower()
            if "?" in text:
                return False
            if any(qf in text_lower for qf in _question_frames):
                return False
            for keyword in APPROVAL_KEYWORDS:
                if keyword in text_lower:
                    matched_refs = []
                    for vi in video_info_list:
                        if vi["num"] and (
                            f"video {vi['num']}" in text_lower
                            or f"#{vi['num']}" in text_lower
                            or f"vid {vi['num']}" in text_lower
                        ):
                            matched_refs.append(vi["ref"])
                    display_refs = matched_refs if matched_refs else video_refs
                    findings.append({
                        "client": client_name,
                        "videos_at_75": ", ".join(display_refs),
                        "message": text[:120].replace("|", "/"),
                        "when": msg.get("datetime", "")[:16],
                        "matched_specific": bool(matched_refs),
                    })
                    return True
            return False

        found = False
        for msg in messages:
            if _check_msg(msg):
                found = True
                break
            if not found:
                for reply in msg.get("thread_replies", []):
                    if _check_msg(reply):
                        found = True
                        break
            if found:
                break

    return findings


def _check_thumbnail_blockers(active_videos, client_map, editor_map):
    blockers = []
    target_statuses = [
        "70 - Approved By Agency",
        "75 - Sent to Client For Review",
        "80 - Approved By Client",
    ]
    for v in active_videos:
        fields = v["fields"]
        status = fields.get("Editing Status", "")
        if status not in target_statuses:
            continue
        fmt = str(fields.get("Format", "")).lower()
        if "short" in fmt or "vsl" in fmt:
            continue
        thumb_status = fields.get("Thumbnail Status", "")
        if not thumb_status or thumb_status in THUMBNAIL_ACTIVE_STATUSES:
            blockers.append({
                "video": AirtableClient.format_video_ref(fields, client_map),
                "video_status": status.split(" - ", 1)[-1] if " - " in status else status,
                "thumbnail_status": thumb_status or "(not set)",
                "editor": AirtableClient.resolve_editor_name(fields, editor_map),
            })
    return blockers


def _check_unanswered(client_channels, team_slack_ids, hours, channel_cache, slack):
    findings = []
    for ch in client_channels:
        if channel_cache is not None:
            messages = channel_cache.get(ch["id"], [])
        else:
            try:
                messages = slack.read_channel(ch["id"], hours=hours, limit=100,
                                              include_threads=True)
            except Exception:
                continue

        if not messages:
            continue

        expanded = []
        for msg in messages:
            expanded.append(msg)
            for reply in msg.get("thread_replies", []):
                expanded.append(reply)
        sorted_msgs = sorted(expanded, key=lambda m: float(m.get("timestamp", "0")))

        pending_client_msg = None
        pending_client_time = None

        for msg in sorted_msgs:
            user_id = msg.get("user_id", "unknown")
            if user_id == "unknown":
                continue
            ts = float(msg.get("timestamp", "0"))
            msg_time = datetime.fromtimestamp(ts)
            is_team = user_id in team_slack_ids

            if is_team:
                pending_client_msg = None
                pending_client_time = None
            else:
                text = msg.get("text", "")
                if _needs_response(text):
                    pending_client_msg = msg
                    pending_client_time = msg_time

        if pending_client_msg and pending_client_time:
            team_reacted = False
            for reaction in pending_client_msg.get("reactions", []):
                reactors = set(reaction.get("users", []))
                if reactors & team_slack_ids:
                    team_reacted = True
                    break
            if not team_reacted:
                hours_ago = (datetime.now() - pending_client_time).total_seconds() / 3600
                if hours_ago >= 4:
                    findings.append({
                        "client": ch["client_name"],
                        "message": pending_client_msg.get("text", "")[:120].replace("|", "/"),
                        "user": pending_client_msg.get("user", "Unknown"),
                        "hours_ago": round(hours_ago, 1),
                        "when": pending_client_time.strftime("%Y-%m-%d %H:%M"),
                        "channel": ch["name"],
                    })

    findings.sort(key=lambda f: -f["hours_ago"])
    return findings


def _check_gaps(active_videos, client_map, editor_map, hours, slack):
    channel_videos: dict = {}
    skip_statuses = set(POST_DEADLINE_STATUSES) | {"75 - Sent to Client For Review"}
    for v in active_videos:
        fields = v["fields"]
        status = fields.get("Editing Status", "")
        if status in skip_statuses:
            continue
        channel_ids = (
            fields.get("Editor's Slack Channel", [])
            or fields.get("Slack ID Channel (from Assigned Editor)", [])
        )
        if not channel_ids:
            continue
        ch_id = channel_ids[0] if isinstance(channel_ids, list) else channel_ids
        channel_videos.setdefault(ch_id, []).append(v)

    def _fetch_editor(ch_id):
        try:
            return ch_id, slack.read_channel(ch_id, hours=hours, limit=100,
                                             include_threads=True)
        except Exception:
            return ch_id, []

    editor_messages: dict = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_editor, ch_id): ch_id for ch_id in channel_videos}
        for future in as_completed(futures):
            ch_id, msgs = future.result()
            editor_messages[ch_id] = msgs

    gaps = []
    for ch_id, videos in channel_videos.items():
        messages = editor_messages.get(ch_id, [])
        has_activity = bool(messages) or any(
            msg.get("thread_replies") for msg in (messages or [])
        )
        if not has_activity:
            editor = AirtableClient.resolve_editor_name(videos[0]["fields"], editor_map)
            video_refs = [AirtableClient.format_video_ref(v["fields"], client_map) for v in videos]
            gaps.append({
                "editor": editor,
                "active_videos": video_refs,
                "video_count": len(videos),
                "silent_hours": hours,
            })
    return gaps


def _check_stale(active_videos, client_map, editor_map):
    stale = []
    now = datetime.now()
    for v in active_videos:
        fields = v["fields"]
        status = fields.get("Editing Status", "")
        threshold = STATUS_STALE_DAYS.get(status)
        if not threshold:
            continue
        lm = fields.get("Last Modified (Editing Status)", "")
        if not lm:
            continue
        try:
            modified_dt = datetime.fromisoformat(lm.replace("Z", "+00:00"))
            days_stuck = (now.astimezone(modified_dt.tzinfo) - modified_dt).days
        except (ValueError, TypeError):
            continue
        if days_stuck >= threshold:
            stale.append({
                "video": AirtableClient.format_video_ref(fields, client_map),
                "editor": AirtableClient.resolve_editor_name(fields, editor_map),
                "status": status.split(" - ", 1)[-1] if " - " in status else status,
                "days_stuck": days_stuck,
                "threshold": threshold,
            })
    stale.sort(key=lambda s: -s["days_stuck"])
    return stale


def _check_deliverables(airtable):
    all_videos = airtable.fetch_table(
        "Videos",
        fields=["Client", "Editing Status", "Last Modified (Editing Status)"],
    )
    client_records = airtable.fetch_table("Clients", fields=["Name", "Status", "Deliverables"])
    client_info = {
        r["id"]: {
            "name": r["fields"].get("Name", "Unknown"),
            "status": r["fields"].get("Status", ""),
            "deliverables": r["fields"].get("Deliverables", ""),
        }
        for r in client_records
    }

    today = date.today()
    first_of_month = today.replace(day=1)
    client_counts: dict = {}

    for v in all_videos:
        fields = v["fields"]
        status = fields.get("Editing Status", "")
        client_ids = fields.get("Client", [])
        if not client_ids:
            continue
        cid = client_ids[0] if isinstance(client_ids, list) else client_ids
        info = client_info.get(cid)
        if not info:
            continue
        if info["status"] in INACTIVE_CLIENT_STATUSES:
            continue
        name = info["name"]
        if name not in client_counts:
            client_counts[name] = {
                "delivered": 0,
                "active": 0,
                "deliverables_raw": info.get("deliverables", ""),
            }
        if status and "100 -" not in status and "DONE" not in status:
            client_counts[name]["active"] += 1
        if "100 -" in status or "DONE" in status:
            modified_str = fields.get("Last Modified (Editing Status)", "")
            if modified_str:
                try:
                    modified_date = datetime.fromisoformat(
                        modified_str.replace("Z", "+00:00")
                    ).date()
                    if modified_date >= first_of_month:
                        client_counts[name]["delivered"] += 1
                except (ValueError, TypeError):
                    pass

    results = []
    for name, counts in sorted(client_counts.items()):
        parsed = _parse_deliverables(counts["deliverables_raw"])
        package_total = parsed["total"]
        if parsed["shorts"] > 0:
            package_str = f"{parsed['long_form']}LF+{parsed['shorts']}S/mo"
        elif package_total > 0:
            package_str = f"{package_total}/mo"
        else:
            package_str = "?"
        delivered = counts["delivered"]
        remaining = max(0, package_total - delivered) if package_total > 0 else 0
        results.append({
            "client": name,
            "package": package_str,
            "package_total": package_total,
            "delivered": delivered,
            "active": counts["active"],
            "remaining": remaining,
            "on_track": delivered >= package_total if package_total > 0 else None,
        })
    return results


def _check_assignments(deliverables_results):
    gaps = []
    for d in deliverables_results:
        if d["remaining"] > 0 and d["active"] == 0:
            gaps.append({
                "client": d["client"],
                "remaining": d["remaining"],
                "package": d["package"],
            })
    return gaps


def _check_pm_tasks(slack, hours):
    try:
        messages = slack.read_channel(
            "project-management",
            hours=hours,
            limit=200,
            include_threads=True,
            max_threads=100,
        )
    except Exception:
        return []

    raw_tasks = []
    now = datetime.now()

    for msg in messages:
        if msg.get("user_id") != SAMU_SLACK_USER_ID:
            continue
        text = msg.get("text", "").strip()
        if not text or not _looks_like_task(text):
            continue
        if _simon_responded(msg, messages):
            continue
        ts = float(msg.get("timestamp", "0"))
        hours_ago = (now - datetime.fromtimestamp(ts)).total_seconds() / 3600
        raw_tasks.append({
            "text": text,
            "message": text[:160].replace("|", "/"),
            "when": msg.get("datetime", "")[:16],
            "hours_ago": round(hours_ago, 1),
            "timestamp": ts,
        })

    return _cluster_tasks(raw_tasks)


# ---------------------------------------------------------------------------
# Summary and actions_needed builders
# ---------------------------------------------------------------------------

def _build_summary(data: dict) -> str:
    parts = []
    label_map = [
        ("new_footage",        "footage flag",       "footage flags"),
        ("client_approval",    "approval signal",    "approval signals"),
        ("thumbnail_blockers", "thumbnail blocker",  "thumbnail blockers"),
        ("unanswered",         "unanswered message", "unanswered messages"),
        ("gaps",               "silent editor",      "silent editors"),
        ("stale",              "stale status",       "stale statuses"),
        ("deliverables",       "deliverable issue",  "deliverable issues"),
        ("assignments",        "assignment gap",     "assignment gaps"),
        ("pm_tasks",           "Samu task",          "Samu tasks"),
    ]
    for key, singular, plural in label_map:
        items = data.get(key, [])
        n = len(items)
        if n > 0:
            parts.append(f"{n} {plural if n != 1 else singular}")
    return ", ".join(parts) + "." if parts else "All clear."


def _build_actions_needed(data: dict) -> list:
    actions = []
    if data.get("unanswered"):
        for item in data["unanswered"]:
            actions.append({
                "priority": "high",
                "action": f"Reply to {item['client']} — unanswered {item['hours_ago']}h: \"{item['message'][:60]}\"",
                "check": "unanswered",
            })
    if data.get("new_footage"):
        for item in data["new_footage"]:
            actions.append({
                "priority": "high",
                "action": f"Assign video for {item['client']} — footage mentioned but no Airtable entry",
                "check": "new_footage",
            })
    if data.get("client_approval"):
        for item in data["client_approval"]:
            actions.append({
                "priority": "high",
                "action": f"Verify approval from {item['client']} for {item['videos_at_75']} — update Airtable if confirmed",
                "check": "client_approval",
            })
    if data.get("pm_tasks"):
        for item in data["pm_tasks"]:
            actions.append({
                "priority": "high",
                "action": f"Complete Samu task ({item['hours_ago']}h ago): \"{item['message'][:80]}\"",
                "check": "pm_tasks",
            })
    if data.get("thumbnail_blockers"):
        for item in data["thumbnail_blockers"]:
            actions.append({
                "priority": "medium",
                "action": f"Resolve thumbnail for {item['video']} (status: {item['thumbnail_status']})",
                "check": "thumbnail_blockers",
            })
    if data.get("stale"):
        for item in data["stale"]:
            actions.append({
                "priority": "medium",
                "action": f"Check {item['video']} stuck at {item['status']} for {item['days_stuck']}d",
                "check": "stale",
            })
    if data.get("assignments"):
        for item in data["assignments"]:
            actions.append({
                "priority": "medium",
                "action": f"Assign video for {item['client']} — {item['remaining']} remaining of {item['package']}, 0 active",
                "check": "assignments",
            })
    if data.get("gaps"):
        for item in data["gaps"]:
            actions.append({
                "priority": "low",
                "action": f"Follow up with {item['editor']} — silent {item['silent_hours']}h with {item['video_count']} active video(s)",
                "check": "gaps",
            })
    if data.get("deliverables"):
        behind = [d for d in data["deliverables"] if d.get("on_track") is False]
        for d in behind:
            actions.append({
                "priority": "low",
                "action": f"{d['client']} behind on deliverables: {d['delivered']} delivered, {d['remaining']} remaining of {d['package']}",
                "check": "deliverables",
            })
    return actions


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(checks: list = None, hours: int = 48) -> dict:
    """Run cross-check operations between Slack and Airtable.

    Args:
        checks: List of check names to run. None means run all nine checks.
        hours:  Lookback window in hours for Slack reads (default 48).

    Returns:
        {
            "ok": bool,
            "data": { <check_name>: [...], ... },
            "summary": str,
            "actions_needed": [ {"priority": ..., "action": ..., "check": ...}, ... ]
        }
    """
    checks_to_run = set(checks) if checks else set(ALL_CHECKS)

    empty_data = {k: [] for k in ALL_CHECKS}

    try:
        airtable = AirtableClient()
        slack = SlackClient()

        # ---- fetch base maps ---------------------------------------------------
        client_map = airtable.get_client_map()
        editor_map = airtable.get_editor_map()

        # ---- fetch team IDs for unanswered / new_footage / client_approval ----
        team_slack_ids: set = set(OPS_MANAGER_IDS)

        # ---- fetch active videos (multiple checks need this) -------------------
        needs_active = checks_to_run & {
            "new_footage", "client_approval", "thumbnail_blockers", "gaps", "stale"
        }
        active_videos = []
        if needs_active:
            active_videos = airtable.fetch_table(
                "Videos",
                fields=[
                    "Video Number", "Client", "Editing Status", "Format",
                    "Assigned Editor", "Editor's Name",
                    "Editor's Slack Channel", "Slack ID Channel (from Assigned Editor)",
                    "Last Modified (Editing Status)",
                    "Thumbnail Status",
                ],
                filter_formula="AND({Editing Status} != '', FIND('100 -', {Editing Status}) = 0)",
            )

        # ---- fetch client channels (multiple checks need this) -----------------
        needs_client_channels = checks_to_run & {
            "new_footage", "client_approval", "unanswered"
        }
        client_channels: list = []
        channel_cache: dict = {}

        if needs_client_channels:
            raw_channels = slack.list_channels(filter_regex=r"-client$")
            client_channels = [
                {
                    "id": ch["id"],
                    "name": ch["name"],
                    "client_name": ch["name"].replace("-client", "").replace("-", " ").title(),
                }
                for ch in raw_channels
                if not ch.get("is_archived")
            ]

            # pre-fetch all client channel messages in parallel
            def _fetch_ch(ch):
                try:
                    msgs = slack.read_channel(ch["id"], hours=hours, limit=100,
                                              include_threads=True, max_threads=10)
                    return ch["id"], msgs
                except Exception:
                    return ch["id"], []

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(_fetch_ch, ch): ch for ch in client_channels}
                for future in as_completed(futures):
                    ch_id, msgs = future.result()
                    channel_cache[ch_id] = msgs

        # ---- run each check ----------------------------------------------------
        data: dict = {k: [] for k in ALL_CHECKS}

        if "new_footage" in checks_to_run:
            try:
                data["new_footage"] = _check_new_footage(
                    active_videos, client_map, client_channels, team_slack_ids,
                    hours, channel_cache, slack,
                )
            except Exception as e:
                logger.warning("new_footage check failed: %s", e)
                data["new_footage"] = []

        if "client_approval" in checks_to_run:
            try:
                data["client_approval"] = _check_client_approval(
                    active_videos, client_map, client_channels, team_slack_ids,
                    hours, channel_cache, slack,
                )
            except Exception as e:
                logger.warning("client_approval check failed: %s", e)
                data["client_approval"] = []

        if "thumbnail_blockers" in checks_to_run:
            try:
                data["thumbnail_blockers"] = _check_thumbnail_blockers(
                    active_videos, client_map, editor_map,
                )
            except Exception as e:
                logger.warning("thumbnail_blockers check failed: %s", e)
                data["thumbnail_blockers"] = []

        if "unanswered" in checks_to_run:
            try:
                data["unanswered"] = _check_unanswered(
                    client_channels, team_slack_ids, hours, channel_cache, slack,
                )
            except Exception as e:
                logger.warning("unanswered check failed: %s", e)
                data["unanswered"] = []

        if "gaps" in checks_to_run:
            try:
                data["gaps"] = _check_gaps(
                    active_videos, client_map, editor_map, hours, slack,
                )
            except Exception as e:
                logger.warning("gaps check failed: %s", e)
                data["gaps"] = []

        if "stale" in checks_to_run:
            try:
                data["stale"] = _check_stale(active_videos, client_map, editor_map)
            except Exception as e:
                logger.warning("stale check failed: %s", e)
                data["stale"] = []

        # deliverables + assignments share one Airtable fetch
        deliverables_results: list = []
        if "deliverables" in checks_to_run or "assignments" in checks_to_run:
            try:
                deliverables_results = _check_deliverables(airtable)
            except Exception as e:
                logger.warning("deliverables fetch failed: %s", e)

        if "deliverables" in checks_to_run:
            data["deliverables"] = deliverables_results

        if "assignments" in checks_to_run:
            try:
                data["assignments"] = _check_assignments(deliverables_results)
            except Exception as e:
                logger.warning("assignments check failed: %s", e)
                data["assignments"] = []

        if "pm_tasks" in checks_to_run:
            try:
                data["pm_tasks"] = _check_pm_tasks(slack, hours)
            except Exception as e:
                logger.warning("pm_tasks check failed: %s", e)
                data["pm_tasks"] = []

        summary = _build_summary(data)
        actions_needed = _build_actions_needed(data)

        return {
            "ok": True,
            "data": data,
            "summary": summary,
            "actions_needed": actions_needed,
        }

    except Exception as e:
        logger.exception("crosscheck skill run() failed")
        return {
            "ok": False,
            "data": {},
            "summary": f"crosscheck skill failed: {e}",
            "actions_needed": [],
        }
