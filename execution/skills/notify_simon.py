import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.slack import SlackClient
from constants import SIMON_SLACK_USER_ID

logger = logging.getLogger(__name__)


def run(message: str, blocks: list = None, urgent: bool = False) -> dict:
    if os.getenv("AGENT_WRITES_ENABLED", "true").lower() == "false":
        logger.warning("AGENT_WRITES_ENABLED=false — skipping DM to Simon")
        return {
            "ok": True,
            "data": {"skipped": True},
            "summary": "writes disabled — DM skipped",
            "actions_needed": [],
        }

    try:
        text = f"🚨 URGENT: {message}" if urgent else message
        client = SlackClient()
        result = client.send_dm(SIMON_SLACK_USER_ID, text, blocks)
        return {
            "ok": True,
            "data": {"ts": result.get("ts", ""), "channel": result.get("channel", "")},
            "summary": f"DM sent to Simon (ts={result.get('ts', '')})",
            "actions_needed": [],
        }
    except Exception as e:
        return {
            "ok": False,
            "data": {},
            "summary": f"notify_simon failed: {e}",
            "actions_needed": [],
        }
