import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import anthropic
from lib.state import load_state, save_state, mark_escalated
from skills import editors, clients, crosscheck, checkout, infer_signals, infer_client_signals
from skills import notify_simon, update_status, draft_message

logger = logging.getLogger(__name__)

_anthropic_client = anthropic.Anthropic()

SYSTEM_PROMPT = (
    "You are the orchestration brain for a PM agent managing a video editing team.\n"
    "Your job: analyze skill output, decide what Simon needs to know, and decide what actions to take.\n\n"
    "Tier 1 actions (act freely): DM Simon, update Airtable Editing Status.\n"
    "Tier 2 actions (queue for Simon approval): send message to editor channel, send message to client channel.\n\n"
    "When infer_signals results are present, route by confidence:\n"
    "- 'high': add an update_status tier1 action — editor clearly indicated completion.\n"
    "- 'medium': add a notify_simon tier1 action with message: "
    "\"Heads up: [editor] may have indicated [video_ref] is ready for [implied_status] "
    "(their message: '[trigger_message]'). Confirm and update Airtable if correct.\"\n"
    "- 'low': no action.\n\n"
    "When infer_client_signals results are present:\n"
    "- ANY confidence ('high' or 'medium'): add a notify_simon tier1 action — "
    "NEVER auto-update for client approvals. Message format: "
    "\"Client [client] may have approved [video_ref] (message: '[trigger_message]'). "
    "Confirm and update to '80 - Approved By Client' if correct.\"\n\n"
    "For status updates NOT from infer_signals: only update if editor explicitly confirmed.\n"
    "Avoid duplicate escalations — check state.last_escalated before escalating a name again.\n\n"
    "Return ONLY valid JSON. No markdown fences. No extra text."
)

_SKILL_FALLBACK = {"ok": False, "summary": "Skill failed — check logs.", "data": {}, "actions_needed": []}

_WRITES_ENABLED = os.getenv("AGENT_WRITES_ENABLED", "true").lower() != "false"


def _run_skill(name: str, fn, **kwargs) -> dict:
    t0 = time.monotonic()
    try:
        result = fn(**kwargs)
        elapsed = time.monotonic() - t0
        logger.info("Skill %s completed in %.2fs", name, elapsed)
        return result
    except Exception as exc:
        logger.warning("Skill %s failed: %s", name, exc)
        return {**_SKILL_FALLBACK, "summary": f"{name} failed: {exc}"}


def _run_relevant_skills(trigger: str, context: str, state: dict) -> dict:
    results = {}

    if trigger == "morning_briefing":
        results["editors"] = _run_skill("editors", editors.run, hours=48)
        results["clients"] = _run_skill("clients", clients.run, hours=48)
        editors_data = results["editors"].get("data", {}).get("editors", [])
        if editors_data:
            results["infer_signals"] = _run_skill(
                "infer_signals", infer_signals.run, editors_data=editors_data
            )
        clients_data = results["clients"].get("data", {}).get("clients", [])
        if clients_data:
            results["infer_client_signals"] = _run_skill(
                "infer_client_signals", infer_client_signals.run, clients_data=clients_data
            )

    elif trigger == "midday_crosscheck":
        results["crosscheck"] = _run_skill("crosscheck", crosscheck.run, hours=24)
        results["editors"] = _run_skill("editors", editors.run, hours=24)
        editors_data = results["editors"].get("data", {}).get("editors", [])
        if editors_data:
            results["infer_signals"] = _run_skill(
                "infer_signals", infer_signals.run, editors_data=editors_data
            )

    elif trigger == "eod_checkout":
        results["checkout"] = _run_skill("checkout", checkout.run, days=1)

    elif trigger == "urgent_watch":
        results["clients"] = _run_skill("clients", clients.run, hours=6)
        clients_data = results["clients"].get("data", {}).get("clients", [])
        if clients_data:
            results["infer_client_signals"] = _run_skill(
                "infer_client_signals", infer_client_signals.run, clients_data=clients_data
            )

    elif trigger == "weekly_summary":
        results["editors"] = _run_skill("editors", editors.run, hours=168)
        results["clients"] = _run_skill("clients", clients.run, hours=168)
        results["crosscheck"] = _run_skill("crosscheck", crosscheck.run, hours=168)
        results["checkout"] = _run_skill("checkout", checkout.run, days=7)
        editors_data = results["editors"].get("data", {}).get("editors", [])
        if editors_data:
            results["infer_signals"] = _run_skill(
                "infer_signals", infer_signals.run, editors_data=editors_data
            )
        clients_data = results["clients"].get("data", {}).get("clients", [])
        if clients_data:
            results["infer_client_signals"] = _run_skill(
                "infer_client_signals", infer_client_signals.run, clients_data=clients_data
            )

    elif trigger == "simon_query":
        results["editors"] = _run_skill("editors", editors.run)
        results["clients"] = _run_skill("clients", clients.run)
        results["crosscheck"] = _run_skill("crosscheck", crosscheck.run)
        results["checkout"] = _run_skill("checkout", checkout.run)
        editors_data = results["editors"].get("data", {}).get("editors", [])
        if editors_data:
            results["infer_signals"] = _run_skill(
                "infer_signals", infer_signals.run, editors_data=editors_data
            )
        clients_data = results["clients"].get("data", {}).get("clients", [])
        if clients_data:
            results["infer_client_signals"] = _run_skill(
                "infer_client_signals", infer_client_signals.run, clients_data=clients_data
            )

    return results


def _call_claude(trigger: str, context: str, results: dict, state: dict) -> dict:
    user_msg = (
        f"TRIGGER: {trigger}\n"
        f"CONTEXT: {context}\n\n"
        f"SKILL RESULTS:\n{json.dumps(results, indent=2)}\n\n"
        f"CURRENT STATE:\n"
        f"last_escalated: {state.get('last_escalated', {})}\n"
        f"pending_approvals_count: {len(state.get('pending_approvals', []))}\n\n"
        'Return JSON:\n'
        '{\n'
        '  "response_to_simon": "...",\n'
        '  "tier1_actions": [\n'
        '    {"type": "notify_simon", "message": "...", "urgent": false},\n'
        '    {"type": "update_status", "video_record_id": "...", "new_status": "...", "reason": "..."}\n'
        '  ],\n'
        '  "tier2_actions": [\n'
        '    {"type": "draft_message", "channel_type": "editor|client", "recipient": "...", "context": "...", "draft_text": "..."}\n'
        '  ],\n'
        '  "escalations": ["EditorName1", "EditorName2"]\n'
        '}'
    )

    response = _anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Claude response parse failed. Raw response: %s", raw)
        return {
            "response_to_simon": "Orchestrator error — check logs.",
            "tier1_actions": [],
            "tier2_actions": [],
            "escalations": [],
        }


def _execute_actions(decision: dict, state: dict) -> None:
    writes_enabled = os.getenv("AGENT_WRITES_ENABLED", "true").lower() != "false"

    if not writes_enabled:
        logger.warning("AGENT_WRITES_ENABLED=false — skipping all write actions")
        return

    for action in decision.get("tier1_actions", []):
        action_type = action.get("type")
        try:
            if action_type == "notify_simon":
                notify_simon.run(message=action.get("message", ""), urgent=action.get("urgent", False))
            elif action_type == "update_status":
                update_status.run(
                    video_record_id=action.get("video_record_id", ""),
                    new_status=action.get("new_status", ""),
                    reason=action.get("reason", ""),
                )
            else:
                logger.warning("Unknown tier1 action type: %s", action_type)
        except Exception as exc:
            logger.warning("Tier1 action %s failed: %s", action_type, exc)

    for action in decision.get("tier2_actions", []):
        action_type = action.get("type")
        try:
            if action_type == "draft_message":
                draft_message.run(
                    channel_type=action.get("channel_type", ""),
                    recipient=action.get("recipient", ""),
                    context=action.get("context", ""),
                    draft_text=action.get("draft_text", ""),
                )
            else:
                logger.warning("Unknown tier2 action type: %s", action_type)
        except Exception as exc:
            logger.warning("Tier2 action %s failed: %s", action_type, exc)

    for name in decision.get("escalations", []):
        mark_escalated(name, state)


def run(trigger: str, context: str = "") -> str:
    state = load_state()
    results = _run_relevant_skills(trigger, context, state)
    decision = _call_claude(trigger, context, results, state)
    _execute_actions(decision, state)
    save_state(state)
    return decision.get("response_to_simon", "")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "trigger",
        choices=["morning_briefing", "midday_crosscheck", "eod_checkout", "urgent_watch", "weekly_summary", "simon_query"],
    )
    parser.add_argument("--context", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["AGENT_WRITES_ENABLED"] = "false"

    logging.basicConfig(level=logging.INFO)
    result = run(args.trigger, args.context)
    print(result)
