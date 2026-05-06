#!/usr/bin/env python3
"""
Slack Bolt bot + APScheduler — wired to orchestrator (Wave 3).
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Env var validation (fail fast) ---
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

if not SLACK_BOT_TOKEN:
    raise ValueError("Missing required env var: SLACK_BOT_TOKEN")
if not SLACK_SIGNING_SECRET:
    raise ValueError("Missing required env var: SLACK_SIGNING_SECRET")

SLACK_SIMON_DM_CHANNEL = os.getenv("SLACK_SIMON_DM_CHANNEL")  # Optional — can be None
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

sys.path.insert(0, str(Path(__file__).parent))
import orchestrator
from lib.slack import SlackClient
from lib.state import load_state, save_state, resolve_approval
from constants import SIMON_SLACK_USER_ID
from flask import Flask, jsonify, request as flask_request
from slack_bolt.adapter.flask import SlackRequestHandler

# --- Slack Bolt app setup (HTTP mode) ---
from slack_bolt import App

app = App(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET,
)

slack_client = SlackClient()

# --- Handlers ---

@app.event("message")
def handle_dm(event, say, logger):
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id"):
        return
    text = event.get("text", "").strip()
    if not text:
        return
    logger.info("DM received: %s", text[:80])
    try:
        response = orchestrator.run("simon_query", context=text)
        say(response or "Done.")
    except Exception as exc:
        logger.exception("Orchestrator failed: %s", exc)
        say("Error processing request — check logs.")


@app.event("reaction_added")
def handle_reaction(event, logger):
    reaction = event.get("reaction")
    if reaction not in ("white_check_mark", "x"):
        return

    item_ts = event.get("item", {}).get("ts")
    approved = reaction == "white_check_mark"

    state = load_state()
    approvals = state.get("pending_approvals", [])
    match = next((a for a in approvals if a.get("dm_ts") == item_ts), None)

    if match is None:
        logger.info("No pending approval found for ts=%s", item_ts)
        return

    approval_id = match["id"]
    recipient = match.get("recipient", "")
    draft_text = match.get("draft_text", "")

    if approved:
        channels = slack_client.list_channels()
        name_fragment = recipient.lower().split()[0]
        target_channel = next(
            (c for c in channels if name_fragment in c.get("name", "").lower()),
            None,
        )
        if target_channel:
            try:
                slack_client.send_channel_message(target_channel["id"], draft_text)
                slack_client.send_dm(SIMON_SLACK_USER_ID, f"✅ Sent to {recipient}.")
            except Exception as exc:
                logger.exception("Failed to send draft to channel: %s", exc)
                slack_client.send_dm(
                    SIMON_SLACK_USER_ID,
                    f"Error sending to {recipient}: {exc}\n\n{draft_text}",
                )
        else:
            slack_client.send_dm(
                SIMON_SLACK_USER_ID,
                f"⚠️ No channel found for '{recipient}'. Send manually:\n\n{draft_text}",
            )
    else:
        slack_client.send_dm(
            SIMON_SLACK_USER_ID,
            f"❌ Cancelled — draft for {recipient} discarded.",
        )

    state = resolve_approval(approval_id, approved, state)
    save_state(state)


# --- Cron handlers ---

def make_cron_handler(trigger_name: str):
    def handler():
        logger.info("Cron fired: %s", trigger_name)
        try:
            result = orchestrator.run(trigger_name)
            logger.info("Cron %s complete: %s", trigger_name, (result or "")[:120])
        except Exception as exc:
            logger.exception("Cron %s failed: %s", trigger_name, exc)
    handler.__name__ = f"cron_{trigger_name}"
    return handler


# --- Flask adapter ---

flask_app = Flask(__name__)
bolt_handler = SlackRequestHandler(app)

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return bolt_handler.handle(flask_request)

@flask_app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# --- Startup/shutdown ---
if __name__ == "__main__":
    from apscheduler.schedulers.background import BackgroundScheduler
    import pytz

    scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Singapore"))

    scheduler.add_job(
        make_cron_handler("morning_briefing"),
        "cron",
        hour=9,
        minute=0,
        day_of_week="mon-fri",
        id="morning_briefing",
    )
    scheduler.add_job(
        make_cron_handler("midday_crosscheck"),
        "cron",
        hour=12,
        minute=0,
        day_of_week="mon-fri",
        id="midday_crosscheck",
    )
    scheduler.add_job(
        make_cron_handler("eod_checkout"),
        "cron",
        hour=17,
        minute=0,
        day_of_week="mon-fri",
        id="eod_checkout",
    )
    scheduler.add_job(
        make_cron_handler("urgent_watch"),
        "cron",
        hour="*/2",
        minute=0,
        id="urgent_watch",
    )
    scheduler.add_job(
        make_cron_handler("weekly_summary"),
        "cron",
        hour=9,
        minute=0,
        day_of_week="mon",
        id="weekly_summary",
    )

    scheduler.start()

    port = int(os.getenv("PORT", 3000))
    logger.info("Starting bot on port %d", port)

    try:
        flask_app.run(host="0.0.0.0", port=port)
    finally:
        scheduler.shutdown()
