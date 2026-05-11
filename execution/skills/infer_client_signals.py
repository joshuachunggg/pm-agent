"""
infer_client_signals skill — LLM-based detection of implied client approvals from vague messages.

Clients rarely give explicit approval. This skill uses Claude Haiku to detect when a client
message implies approval of a video at status 75 (Sent to Client For Review), catching what
the keyword list in _check_client_approval misses (e.g. "Thanks!", "🔥🔥", "Really happy!").

Unlike editor signals, client approvals NEVER auto-update Airtable. All signals — regardless
of confidence — surface to Simon for confirmation. Client approval is too high-stakes to act on
without a human check.

Interface:
    run(clients_data: list) -> dict

clients_data: list of client dicts from clients.run()["data"]["clients"]
Each dict must have: name, recent_messages (list)

Returns:
    {
        "ok": bool,
        "data": {
            "signals": [
                {
                    "client": str,
                    "video_record_id": str,
                    "video_ref": str,
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
from lib.airtable import AirtableClient
from constants import OPS_MANAGER_IDS

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic()

_SYSTEM_PROMPT = (
    "You analyze Slack messages from a video production client to detect implied approval.\n"
    "The client has received a video for review and may approve it in vague, informal ways.\n\n"
    "Return ONLY a valid JSON array. No markdown fences, no explanation.\n\n"
    "Confidence guide:\n"
    "- 'high': client clearly approves (e.g. 'approved', 'looks great', 'love it', 'good to go')\n"
    "- 'medium': vague positive signal that likely means approval "
    "(e.g. 'Thanks!', 'Perfect 🙏', '🔥🔥', 'Really happy with this', emoji-only positive reaction)\n"
    "- 'low': positive but too ambiguous — could just be social pleasantry, not approval\n\n"
    "Only return entries with confidence 'high' or 'medium'. Return [] if nothing is a clear approval signal.\n"
    "Ignore messages that are questions, complaints, revision requests, or general chit-chat."
)


def _fetch_status75_videos(at: AirtableClient) -> dict:
    """Return {client_record_id: [{record_id, video_ref, status}]} for all status-75 videos."""
    records = at.fetch_table(
        "Videos",
        fields=["Video Number", "Client", "Editing Status", "Format"],
        filter_formula="{Editing Status}='75 - Sent to Client For Review'",
    )
    client_map = at.get_client_map()
    by_client: dict = {}
    for rec in records:
        fields = rec["fields"]
        client_ids = fields.get("Client", [])
        if not client_ids:
            continue
        cid = client_ids[0] if isinstance(client_ids, list) else client_ids
        video_ref = AirtableClient.format_video_ref(fields, client_map)
        by_client.setdefault(cid, []).append({
            "record_id": rec["id"],
            "video_ref": video_ref,
        })
    return by_client, client_map


def _filter_to_client_messages(messages: list) -> list:
    """Keep only messages from non-team members."""
    return [
        m for m in messages
        if m.get("user_id", "unknown") not in OPS_MANAGER_IDS
        and m.get("user_id", "unknown") != "unknown"
        and len(m.get("text", "").strip()) >= 2
    ]


def _infer_for_client(client_name: str, videos: list, messages: list) -> list:
    """One Haiku call per client. Returns list of approval signal dicts."""
    if not messages:
        return []

    videos_txt = "\n".join(
        f"- record_id={v['record_id']} | {v['video_ref']}"
        for v in videos
    )

    sorted_msgs = sorted(messages, key=lambda m: float(m.get("timestamp", 0)))[-10:]
    msgs_txt = "\n".join(
        f"[{m.get('datetime', '')[:16]}] {m.get('text', '')[:300]}"
        for m in sorted_msgs
    )

    user_msg = (
        f"Client: {client_name}\n\n"
        f"Videos sent to this client for review (status 75):\n{videos_txt}\n\n"
        f"Recent client Slack messages (oldest → newest):\n{msgs_txt}\n\n"
        "Detect any approval signals. Only flag messages that imply the client is happy "
        "with the video and giving the green light.\n\n"
        "Return JSON array:\n"
        '[{"video_record_id": "...", "video_ref": "...", "confidence": "high|medium|low", '
        '"trigger_message": "...", "reason": "..."}]'
    )

    response = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        signals = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("infer_client_signals: JSON parse failed for %s. Raw: %s", client_name, raw[:200])
        return []

    if not isinstance(signals, list):
        return []

    return [
        s for s in signals
        if isinstance(s, dict)
        and s.get("confidence") in ("high", "medium")
        and s.get("video_record_id")
    ]


def run(clients_data: list) -> dict:
    """Infer implied client approvals from recent Slack messages.

    Args:
        clients_data: list of client dicts from clients.run()["data"]["clients"]

    Returns:
        Standard skill result dict with approval signals in data["signals"].
    """
    try:
        at = AirtableClient()
        videos_by_client_id, client_map = _fetch_status75_videos(at)
    except Exception as exc:
        return {
            "ok": False,
            "data": {"signals": []},
            "summary": f"infer_client_signals fetch failed: {exc}",
            "actions_needed": [],
        }

    if not videos_by_client_id:
        return {
            "ok": True,
            "data": {"signals": []},
            "summary": "No videos at status 75 — nothing to check.",
            "actions_needed": [],
        }

    # Build reverse map: client name (lower) → record_id
    name_to_id = {v.lower(): k for k, v in client_map.items()}

    all_signals = []

    for client in clients_data:
        client_name = client.get("name", "")
        messages = client.get("recent_messages", [])
        if not client_name or not messages:
            continue

        # Match client name to Airtable record_id
        cid = name_to_id.get(client_name.lower())
        if not cid:
            # Try partial match
            for airtable_name_lower, record_id in name_to_id.items():
                if client_name.lower() in airtable_name_lower or airtable_name_lower in client_name.lower():
                    cid = record_id
                    break

        if not cid or cid not in videos_by_client_id:
            continue

        videos = videos_by_client_id[cid]
        client_messages = _filter_to_client_messages(messages)
        if not client_messages:
            continue

        try:
            signals = _infer_for_client(client_name, videos, client_messages)
            for s in signals:
                s["client"] = client_name
            all_signals.extend(signals)
            if signals:
                logger.info(
                    "infer_client_signals: %d signal(s) for %s", len(signals), client_name
                )
        except Exception as exc:
            logger.warning("infer_client_signals: failed for %s: %s", client_name, exc)

    high = [s for s in all_signals if s.get("confidence") == "high"]
    medium = [s for s in all_signals if s.get("confidence") == "medium"]

    parts = []
    if high:
        parts.append(f"{len(high)} likely approval(s)")
    if medium:
        parts.append(f"{len(medium)} possible approval(s) — Simon to confirm")
    summary = ", ".join(parts) if parts else "No client approval signals detected."

    return {
        "ok": True,
        "data": {"signals": all_signals},
        "summary": summary,
        "actions_needed": [],
    }
