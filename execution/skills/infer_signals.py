"""
infer_signals skill — LLM-based inference of implied status transitions from vague editor messages.

Editors rarely state explicit status changes. This skill uses Claude Haiku to interpret
vague messages (e.g. "check it out", "just finished", sharing a Frame.io link) and infer
whether a status transition is implied.

Interface:
    run(editors_data: list) -> dict

editors_data: list of editor dicts from editors.run()["data"]["editors"]
Each dict must have: name, active_videos (list), recent_messages (list)

Returns:
    {
        "ok": bool,
        "data": {
            "signals": [
                {
                    "editor": str,
                    "video_record_id": str,
                    "video_ref": str,
                    "current_status": str,
                    "implied_status": str,
                    "confidence": "high" | "medium" | "low",
                    "trigger_message": str,
                    "reason": str,
                }
            ]
        },
        "summary": str,
        "actions_needed": [],
    }
"""

import json
import logging
import sys
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).parent.parent))
from constants import VALID_TRANSITIONS

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic()

# Only editor-driven transitions — editors cannot push past status 60
_EDITOR_TARGET_STATUSES = [
    "50 - Editor Confirmed",
    "59 - Editing Revisions",
    "60 - Submitted for QC",
    "60 - Internal Review",
]

# Statuses where editor is done — no inference needed
_SKIP_STATUSES = {
    "70 - Approved By Agency",
    "75 - Sent to Client For Review",
    "80 - Approved By Client",
    "90 - Approved But On Hold",
    "100 - Scheduled - DONE",
}

_BOT_USERNAMES = {"airtable2", "airtable", "slackbot"}
_CHECKIN_MARKER = "Send your *check in*"

_SYSTEM_PROMPT = (
    "You analyze Slack messages from a video editor to detect implied status transitions.\n"
    "Editors are often vague — they rarely say 'I'm moving this to QC'. "
    "Look for implicit signals: 'done', 'finished', 'check it out', sharing a Frame.io link, etc.\n\n"
    "Return ONLY a valid JSON array. No markdown fences, no explanation.\n\n"
    "Confidence guide:\n"
    "- 'high': editor explicitly states completion or submission for a specific video "
    "(e.g. 'submitted for QC', 'done with [video number]', 'sent the draft')\n"
    "- 'medium': vague but context strongly implies a transition "
    "(e.g. 'check it out' + shared a link, 'just finished', 'done' without specifying which video)\n"
    "- 'low': weak or ambiguous — could mean anything\n\n"
    "Only return entries with confidence 'high' or 'medium'. Return [] if nothing clear."
)


def _filter_messages(messages: list) -> list:
    """Remove bot messages and check-in prompts."""
    return [
        m for m in messages
        if _CHECKIN_MARKER not in m.get("text", "")
        and m.get("username", "").lower() not in _BOT_USERNAMES
        and m.get("user", "").lower() not in _BOT_USERNAMES
    ]


def _infer_for_editor(editor_name: str, active_videos: list, messages: list) -> list:
    """One Haiku call per editor. Returns list of signal dicts."""
    relevant_videos = [v for v in active_videos if v.get("status") not in _SKIP_STATUSES]
    if not relevant_videos:
        return []

    human_messages = _filter_messages(messages)
    if not human_messages:
        return []

    videos_txt = "\n".join(
        f"- record_id={v['record_id']} | {v['video_ref']} | current status: {v['status']}"
        for v in relevant_videos
    )

    # Chronological order, last 15 messages
    sorted_msgs = sorted(human_messages, key=lambda m: float(m.get("timestamp", 0)))[-15:]
    msgs_txt = "\n".join(
        f"[{m.get('datetime', '')[:16]}] {m.get('text', '')[:300]}"
        for m in sorted_msgs
    )

    user_msg = (
        f"Editor: {editor_name}\n\n"
        f"Active videos:\n{videos_txt}\n\n"
        f"Recent Slack messages (oldest → newest):\n{msgs_txt}\n\n"
        f"Valid target statuses: {', '.join(_EDITOR_TARGET_STATUSES)}\n\n"
        "Return JSON array of implied transitions:\n"
        '[{"video_record_id": "...", "video_ref": "...", "current_status": "...", '
        '"implied_status": "...", "confidence": "high|medium|low", '
        '"trigger_message": "...", "reason": "..."}]'
    )

    response = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()

    # Strip markdown fences if model ignores instructions
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        signals = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("infer_signals: JSON parse failed for %s. Raw: %s", editor_name, raw[:200])
        return []

    if not isinstance(signals, list):
        return []

    valid = []
    for s in signals:
        if not isinstance(s, dict):
            continue
        confidence = s.get("confidence", "low")
        if confidence not in ("high", "medium"):
            continue
        if not s.get("video_record_id") or not s.get("implied_status"):
            continue

        # Downgrade high → medium if the transition skips intermediate statuses.
        # Auto-updating a skipped transition could corrupt pipeline state;
        # safer to surface to Simon for confirmation.
        current = s.get("current_status", "")
        implied = s.get("implied_status", "")
        valid_nexts = VALID_TRANSITIONS.get(current, [])
        if valid_nexts and implied not in valid_nexts and confidence == "high":
            s["confidence"] = "medium"
            s["reason"] = (
                f"[status skip: '{current}' → '{implied}' skips intermediate steps — "
                f"valid next: {valid_nexts}] " + s.get("reason", "")
            )

        valid.append(s)

    return valid


def run(editors_data: list) -> dict:
    """Infer implied status transitions from editor Slack messages.

    Args:
        editors_data: list of editor dicts from editors.run()["data"]["editors"]

    Returns:
        Standard skill result dict with signals in data["signals"].
    """
    all_signals = []

    for editor in editors_data:
        name = editor.get("name", "Unknown")
        active_videos = editor.get("active_videos", [])
        messages = editor.get("recent_messages", [])

        if not active_videos or not messages:
            continue

        try:
            signals = _infer_for_editor(name, active_videos, messages)
            for s in signals:
                s["editor"] = name
            all_signals.extend(signals)
            if signals:
                logger.info(
                    "infer_signals: %d signal(s) found for %s", len(signals), name
                )
        except Exception as exc:
            logger.warning("infer_signals: failed for editor %s: %s", name, exc)

    high = [s for s in all_signals if s.get("confidence") == "high"]
    medium = [s for s in all_signals if s.get("confidence") == "medium"]

    parts = []
    if high:
        parts.append(f"{len(high)} high-confidence transition(s)")
    if medium:
        parts.append(f"{len(medium)} medium-confidence signal(s) for Simon review")
    summary = ", ".join(parts) if parts else "No implied status transitions detected."

    return {
        "ok": True,
        "data": {"signals": all_signals},
        "summary": summary,
        "actions_needed": [],
    }
