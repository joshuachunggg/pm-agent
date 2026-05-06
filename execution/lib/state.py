import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

_project_root = Path(__file__).parent.parent.parent
_TMP_DIR = _project_root / ".tmp"
_STATE_FILE = _TMP_DIR / "agent_state.json"

logger = logging.getLogger(__name__)

_EMPTY_STATE: Dict = {
    "last_escalated": {},
    "pending_approvals": [],
    "actioned_slack_ts": [],
}


def load_state() -> Dict:
    _TMP_DIR.mkdir(exist_ok=True)
    if not _STATE_FILE.exists():
        return dict(_EMPTY_STATE)
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt state file — resetting to empty state")
        return dict(_EMPTY_STATE)


def save_state(state: Dict) -> None:
    _TMP_DIR.mkdir(exist_ok=True)
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def was_recently_escalated(name: str, hours: float = 24, state: Dict = None) -> bool:
    if state is None:
        state = load_state()
    ts_str = state.get("last_escalated", {}).get(name)
    if not ts_str:
        return False
    ts = datetime.fromisoformat(ts_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts > datetime.now(timezone.utc) - timedelta(hours=hours)


def mark_escalated(name: str, state: Dict = None) -> Dict:
    if state is None:
        state = load_state()
    state.setdefault("last_escalated", {})[name] = datetime.now(timezone.utc).isoformat()
    return state


def queue_approval(action_dict: Dict, state: Dict = None) -> Dict:
    if state is None:
        state = load_state()
    if "id" not in action_dict:
        action_dict = {**action_dict, "id": str(uuid.uuid4())}
    if "expires" not in action_dict:
        action_dict = {**action_dict, "expires": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()}
    state.setdefault("pending_approvals", []).append(action_dict)
    return state


def resolve_approval(approval_id: str, approved: bool, state: Dict = None) -> Dict:
    if state is None:
        state = load_state()
    state["pending_approvals"] = [
        a for a in state.get("pending_approvals", []) if a.get("id") != approval_id
    ]
    return state


def mark_actioned(slack_ts: str, state: Dict = None) -> Dict:
    if state is None:
        state = load_state()
    ts_list = state.setdefault("actioned_slack_ts", [])
    if slack_ts not in ts_list:
        ts_list.append(slack_ts)
    return state


def was_actioned(slack_ts: str, state: Dict = None) -> bool:
    if state is None:
        state = load_state()
    return slack_ts in state.get("actioned_slack_ts", [])
