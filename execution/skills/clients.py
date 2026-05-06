"""
clients skill — M2b
Scans *-client Slack channels, detects sentiment, unanswered messages,
churn signals, and cross-references Airtable for active video counts.
"""

import logging
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.airtable import AirtableClient
from lib.slack import SlackClient
from constants import (
    INACTIVE_CLIENT_STATUSES,
    POST_DEADLINE_STATUSES,
    CLIENT_RESPONSE_SLOW_HOURS,
    OPS_MANAGER_IDS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword lists
# ---------------------------------------------------------------------------

STRONGLY_POSITIVE_KEYWORDS = [
    "really happy", "love it", "love the", "loving the", "love how",
    "excellent", "amazing", "fantastic", "brilliant", "impressed",
    "blown away", "outstanding", "incredible", "phenomenal",
    "really happy how", "so happy with", "exceeded expectations",
    "best video", "best content", "really pleased",
]

CHURN_KEYWORDS = [
    "cancel", "canceling", "cancelling", "cancellation",
    "hold off", "put on hold", "on hold", "pausing the",
    "taking a break", "stepping back", "not continu", "not continuing",
    "won't be doing", "won't be able to do the videos",
    "won't be able to do the video", "not going to be able to do the video",
    "can't do the video", "can't film", "won't be filming",
    "not a priority", "no longer a priority", "things came up",
    "got slammed", "no longer need",
    "doing it ourselves", "in-house", "found someone else", "going with another",
    "too expensive", "can't afford", "budget cut", "cutting budget",
    "not seeing results", "not working for us",
]

NEGATIVE_KEYWORDS = [
    "disappointed", "frustrat", "upset", "unhappy", "issue", "problem",
    "not happy", "confused", "mistake", "error", "redo",
    "urgent", "asap", "overdue",
    "not what i", "not ideal", "off brand",
]

# ---------------------------------------------------------------------------
# System message detection
# ---------------------------------------------------------------------------

_SYSTEM_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic",
    "channel_purpose", "channel_name", "pinned_item",
    "unpinned_item", "channel_archive", "bot_message",
}

_SYSTEM_RE = re.compile(
    r"has joined the channel|has left the channel|was added to this channel by"
    r"|set the channel (topic|purpose|description)|renamed the channel"
    r"|archived the channel|pinned a message|unpinned a message"
    r"|This content can't be displayed",
    re.IGNORECASE,
)

_ACKNOWLEDGMENT_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r"^(ok|okay|k|kk)[\.\!]?$",
        r"^(thanks|thank you|thx|ty)[\.\!]?$",
        r"^(awesome|great|perfect|nice|cool|sounds good|got it|noted)[\.\!]?$",
        r"^(yes|yep|yup|yeah|yea)[\.\!]?$",
        r"^(no problem|np|no worries|nw)[\.\!]?$",
        r"^\:[\w\+\-]+\:$",
        r"^(will do|on it|doing it now)[\.\!]?$",
    ]
]


def _is_system_message(msg: dict) -> bool:
    if msg.get("subtype", "") in _SYSTEM_SUBTYPES:
        return True
    if _SYSTEM_RE.search(msg.get("text", "")):
        return True
    return False


def _is_acknowledgment(text: str) -> bool:
    t = text.lower().strip()
    if len(t) >= 15:
        return False
    return any(p.match(t) for p in _ACKNOWLEDGMENT_PATTERNS)


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------

def _analyze_sentiment(messages: list, team_ids: set) -> str:
    """Return 'happy'|'at_risk'|'churning'|'neutral' based on client messages."""
    strongly_positive = 0
    negative = 0
    churn_hit = False

    for msg in messages:
        sources = [msg] + msg.get("thread_replies", [])
        for src in sources:
            uid = src.get("user_id", "unknown")
            # Only look at client messages for churn/negative signals
            text = src.get("text", "")
            if not text or len(text) < 5:
                continue
            tl = text.lower()

            if uid not in team_ids and uid != "unknown":
                for kw in CHURN_KEYWORDS:
                    if kw in tl:
                        churn_hit = True
                        break
                for kw in NEGATIVE_KEYWORDS:
                    if kw in tl:
                        negative += 1
                        break

            for kw in STRONGLY_POSITIVE_KEYWORDS:
                if kw in tl:
                    strongly_positive += 1
                    break

    if churn_hit:
        return "churning"
    if negative > 0:
        return "at_risk"
    if strongly_positive > 0:
        return "happy"
    return "neutral"


# ---------------------------------------------------------------------------
# Churn signal extraction
# ---------------------------------------------------------------------------

def _detect_churn_signals(messages: list, team_ids: set) -> list:
    """Return list of churn signal phrase strings (deduplicated)."""
    signals = []
    seen_phrases = set()
    now = datetime.now()
    for msg in messages:
        uid = msg.get("user_id", "unknown")
        if uid == "unknown" or uid in team_ids:
            continue
        text = msg.get("text", "")
        if not text:
            continue
        tl = text.lower()
        for kw in CHURN_KEYWORDS:
            if kw in tl and kw not in seen_phrases:
                seen_phrases.add(kw)
                ts = float(msg.get("timestamp", "0"))
                hours_ago = (now - datetime.fromtimestamp(ts)).total_seconds() / 3600
                signals.append(
                    f'"{kw}" — {msg.get("user", "client")} ({round(hours_ago, 1)}h ago)'
                )
                break
    return signals


# ---------------------------------------------------------------------------
# Unanswered message detection
# ---------------------------------------------------------------------------

def _find_unanswered(messages: list, team_ids: set) -> list:
    """
    Return messages sent by clients with no subsequent team reply within
    CLIENT_RESPONSE_SLOW_HOURS hours. Excludes bots and system messages.
    """
    # Flatten and sort chronologically
    flat = []
    for msg in messages:
        if not _is_system_message(msg):
            flat.append(msg)
        for reply in msg.get("thread_replies", []):
            flat.append(reply)
    flat.sort(key=lambda m: float(m.get("timestamp", "0")))

    now = datetime.now()
    unanswered = []

    pending = None  # last client message needing response

    for msg in flat:
        uid = msg.get("user_id", "unknown")
        if uid == "unknown":
            continue
        if _is_system_message(msg):
            continue

        text = msg.get("text", "")
        is_team = uid in team_ids

        if is_team:
            # Team reply clears pending
            pending = None
        else:
            # Client message — replace pending (only track latest chain)
            if not _is_acknowledgment(text) and len(text.strip()) > 3:
                pending = msg

    if pending is not None:
        # Check if team reacted (emoji counts as acknowledgment)
        team_reacted = any(
            uid in team_ids
            for reaction in pending.get("reactions", [])
            for uid in reaction.get("users", [])
        )
        if not team_reacted:
            ts = float(pending.get("timestamp", "0"))
            hours_ago = (now - datetime.fromtimestamp(ts)).total_seconds() / 3600
            if hours_ago > CLIENT_RESPONSE_SLOW_HOURS:
                unanswered.append({
                    "text": pending.get("text", "")[:120],
                    "user": pending.get("user", "Unknown"),
                    "hours_ago": round(hours_ago, 1),
                    "timestamp": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
                })

    return unanswered


# ---------------------------------------------------------------------------
# Last client message age
# ---------------------------------------------------------------------------

def _last_client_message_hours(messages: list, team_ids: set) -> float | None:
    now = datetime.now()
    # messages from SlackClient.read_channel are newest-first
    for msg in messages:
        uid = msg.get("user_id", "unknown")
        if uid == "unknown" or uid in team_ids:
            continue
        if _is_system_message(msg):
            continue
        text = msg.get("text", "")
        if len(text.strip()) < 3:
            continue
        ts = float(msg.get("timestamp", "0"))
        return round((now - datetime.fromtimestamp(ts)).total_seconds() / 3600, 1)
    return None


# ---------------------------------------------------------------------------
# Active videos per client
# ---------------------------------------------------------------------------

def _fetch_active_videos(at: AirtableClient, client_record_id: str, client_map: dict) -> list:
    """Return formatted active video strings for this client (excludes POST_DEADLINE_STATUSES)."""
    exclude_statuses = POST_DEADLINE_STATUSES
    # Build filter: client matches AND status not in post-deadline
    client_filter = AirtableClient.build_linked_record_filter(client_record_id, "Client")
    status_filters = [f"{{Editing Status}}!='{s}'" for s in exclude_statuses]
    formula = f"AND({client_filter}, {', '.join(status_filters)})"
    try:
        records = at.fetch_table(
            "Videos",
            fields=["Client", "Video Number", "Format", "Editing Status"],
            filter_formula=formula,
        )
    except Exception as exc:
        logger.warning("fetch_active_videos failed for %s: %s", client_record_id, exc)
        return []
    return [AirtableClient.format_video_ref(r["fields"], client_map) for r in records]


# ---------------------------------------------------------------------------
# Risk level
# ---------------------------------------------------------------------------

def _compute_risk(sentiment: str, unanswered: list, churn_signals: list) -> str:
    if churn_signals or sentiment == "churning":
        return "high"
    if unanswered or sentiment == "at_risk":
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Actions needed
# ---------------------------------------------------------------------------

def _build_actions(clients_data: list) -> list:
    actions = []
    for c in clients_data:
        name = c["name"]
        if c["sentiment"] == "churning" or c["risk_level"] == "high":
            if c["churn_signals"]:
                actions.append({"priority": "high", "action": "Address churn signals immediately", "client": name})
            if c["unanswered_messages"]:
                actions.append({"priority": "high", "action": "Reply to unanswered message", "client": name})
            elif c["churn_signals"]:
                pass  # already added above
            else:
                actions.append({"priority": "high", "action": "Review high-risk client", "client": name})
        elif c["risk_level"] == "medium":
            if c["unanswered_messages"]:
                actions.append({"priority": "medium", "action": "Reply to unanswered message", "client": name})
            else:
                actions.append({"priority": "medium", "action": "Monitor client closely", "client": name})
    return actions


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(client: str = None, hours: int = 48) -> dict:
    try:
        at = AirtableClient()
        sc = SlackClient()

        # Team IDs = OPS_MANAGER_IDS + Airtable Team table Slack IDs
        team_ids = set(OPS_MANAGER_IDS)
        try:
            team_records = at.fetch_table("Team", fields=["Name", "Slack ID"])
            for r in team_records:
                sid = r["fields"].get("Slack ID", "")
                if sid:
                    team_ids.add(sid)
        except Exception as exc:
            logger.warning("Could not load team Slack IDs from Airtable: %s", exc)

        # Airtable: client map (record_id → name) + status
        client_records = at.fetch_table("Clients", fields=["Name", "Status"])
        active_client_ids = {}   # record_id → name (current/onboarding only)
        inactive_names = set()
        for r in client_records:
            status = r["fields"].get("Status", "")
            name = r["fields"].get("Name", "Unknown")
            if status in INACTIVE_CLIENT_STATUSES:
                inactive_names.add(name.lower())
            elif status in ("Current", "Onboarding"):
                active_client_ids[r["id"]] = name

        client_map = {r["id"]: r["fields"].get("Name", "Unknown") for r in client_records}

        # Reverse map: lowercase name → record_id (for channel→airtable matching)
        name_to_record_id = {v.lower(): k for k, v in active_client_ids.items()}

        # Discover client channels
        all_channels = sc.list_channels(filter_regex=r"-client$")
        channels = [ch for ch in all_channels if not ch.get("is_archived", False)]

        clients_out = []

        for ch in channels:
            ch_name = ch["name"]  # e.g. "josh-client"
            ch_id = ch["id"]

            # Derive display name from channel name
            derived = ch_name.replace("-client", "").replace("-", " ").title()

            # Match to Airtable (exact first, then word-prefix)
            matched_name = None
            matched_record_id = None

            for rec_id, airtable_name in active_client_ids.items():
                if airtable_name.lower() == derived.lower():
                    matched_name = airtable_name
                    matched_record_id = rec_id
                    break

            if not matched_name:
                for rec_id, airtable_name in active_client_ids.items():
                    a = airtable_name.lower()
                    c = derived.lower()
                    if c.startswith(a + " ") or a.startswith(c + " "):
                        matched_name = airtable_name
                        matched_record_id = rec_id
                        break

            display_name = matched_name or derived

            # Skip inactive clients
            if display_name.lower() in inactive_names:
                continue

            # Filter by client param if provided
            if client and client.lower() not in display_name.lower():
                continue

            # Fetch Slack messages
            try:
                messages = sc.read_channel(ch_id, hours=hours, limit=200, include_threads=True, max_threads=30)
            except Exception as exc:
                logger.warning("Could not read channel %s (%s): %s", ch_name, ch_id, exc)
                messages = []

            # Analyze
            sentiment = _analyze_sentiment(messages, team_ids)
            churn_signals = _detect_churn_signals(messages, team_ids)
            unanswered = _find_unanswered(messages, team_ids)
            risk_level = _compute_risk(sentiment, unanswered, churn_signals)
            last_hrs = _last_client_message_hours(messages, team_ids)
            needs_resp = len(unanswered) > 0

            # Active videos
            if matched_record_id:
                active_videos = _fetch_active_videos(at, matched_record_id, client_map)
            else:
                active_videos = []

            # Recent messages (last 5 from anyone, non-system)
            recent = []
            flat_msgs = []
            for msg in messages:
                if not _is_system_message(msg):
                    flat_msgs.append(msg)
                for reply in msg.get("thread_replies", []):
                    flat_msgs.append(reply)
            flat_msgs.sort(key=lambda m: float(m.get("timestamp", "0")), reverse=True)
            for msg in flat_msgs[:5]:
                ts = float(msg.get("timestamp", "0"))
                recent.append({
                    "text": msg.get("text", "")[:200],
                    "user": msg.get("user", "Unknown"),
                    "user_id": msg.get("user_id", "unknown"),
                    "hours_ago": round((datetime.now() - datetime.fromtimestamp(ts)).total_seconds() / 3600, 1),
                })

            clients_out.append({
                "name": display_name,
                "channel_id": ch_id,
                "sentiment": sentiment,
                "risk_level": risk_level,
                "unanswered_messages": unanswered,
                "recent_messages": recent,
                "active_videos": active_videos,
                "churn_signals": churn_signals,
                "last_client_message_hours_ago": last_hrs,
                "needs_response": needs_resp,
            })

        # Aggregate stats
        high_risk = [c["name"] for c in clients_out if c["risk_level"] == "high"]
        churning = [c["name"] for c in clients_out if c["sentiment"] == "churning"]
        unanswered_count = sum(len(c["unanswered_messages"]) for c in clients_out)

        # Sort: high → medium → low
        risk_order = {"high": 0, "medium": 1, "low": 2}
        clients_out.sort(key=lambda c: risk_order.get(c["risk_level"], 3))

        actions = _build_actions(clients_out)

        total = len(clients_out)
        attention = len(high_risk) + len([c for c in clients_out if c["risk_level"] == "medium"])
        summary = (
            f"{total} clients scanned. "
            f"{len(high_risk)} high-risk, {len(churning)} churning, "
            f"{unanswered_count} unanswered messages."
            + (f" {attention} need attention." if attention else " All healthy.")
        )

        return {
            "ok": True,
            "data": {
                "clients": clients_out,
                "high_risk_clients": high_risk,
                "unanswered_count": unanswered_count,
                "churning_clients": churning,
            },
            "summary": summary,
            "actions_needed": actions,
        }

    except Exception as exc:
        logger.exception("clients skill failed")
        return {
            "ok": False,
            "data": {},
            "summary": f"clients skill failed: {exc}",
            "actions_needed": [],
        }
