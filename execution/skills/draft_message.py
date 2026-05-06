import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.slack import SlackClient
from lib.state import load_state, save_state, queue_approval
from constants import SIMON_SLACK_USER_ID

logger = logging.getLogger(__name__)


def run(channel_type: str, recipient: str, context: str, draft_text: str) -> dict:
    try:
        if os.environ.get("AGENT_WRITES_ENABLED", "true").lower() == "false":
            logger.info("AGENT_WRITES_ENABLED=false — skipping draft_message")
            return {
                "ok": True,
                "data": {
                    "approval_id": "",
                    "expires": "",
                    "dm_ts": "",
                    "skipped": True,
                },
                "summary": "Writes disabled — draft_message skipped.",
                "actions_needed": [],
            }

        approval_text = (
            f"\U0001f4dd *Draft {channel_type} message for {recipient}*\n\n"
            f"*Context:* {context}\n\n"
            f"*Proposed message:*\n_{draft_text}_\n\n"
            f"React ✅ to send • React ❌ to cancel (expires in 4h)"
        )

        client = SlackClient()
        dm_result = client.send_dm(SIMON_SLACK_USER_ID, approval_text)
        ts = dm_result["ts"]

        action_dict = {
            "channel_type": channel_type,
            "recipient": recipient,
            "draft_text": draft_text,
            "context": context,
            "dm_ts": ts,
        }

        state = load_state()
        state = queue_approval(action_dict, state)
        # Retrieve the generated id and expires from the queued entry
        queued = state["pending_approvals"][-1]
        approval_id = queued["id"]
        expires = queued["expires"]
        save_state(state)

        logger.info("Queued draft_message approval %s (dm_ts=%s)", approval_id, ts)
        return {
            "ok": True,
            "data": {
                "approval_id": approval_id,
                "expires": expires,
                "dm_ts": ts,
                "skipped": False,
            },
            "summary": (
                f"Draft {channel_type} message for {recipient} queued for Simon's approval "
                f"(approval_id={approval_id}, dm_ts={ts})."
            ),
            "actions_needed": [],
        }

    except Exception as e:
        logger.exception("draft_message failed")
        return {
            "ok": False,
            "data": {},
            "summary": f"draft_message failed: {e}",
            "actions_needed": [],
        }
