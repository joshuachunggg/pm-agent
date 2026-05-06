import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from pyairtable import Api

_project_root = Path(__file__).parent.parent.parent
load_dotenv(_project_root / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent))
from constants import VALID_TRANSITIONS

WRITABLE_FIELDS = ["Editing Status", "Assigned Editor", "Thumbnail Status"]
_TMP_DIR = _project_root / ".tmp"
_WRITE_LOG = _TMP_DIR / "write_log.jsonl"

logger = logging.getLogger(__name__)


class AirtableClient:
    def __init__(self, api_key: str = None, base_id: str = None):
        self.api_key = api_key or os.getenv("AIRTABLE_API_KEY")
        self.base_id = base_id or os.getenv("AIRTABLE_BASE_ID")
        if not self.api_key:
            raise ValueError("AIRTABLE_API_KEY not set")
        if not self.base_id:
            raise ValueError("AIRTABLE_BASE_ID not set")
        self._api = Api(self.api_key)
        self._rate_delay = 0.2  # 5 req/sec
        self._last_call = 0.0
        self.writes_enabled = os.getenv("AGENT_WRITES_ENABLED", "true").lower() == "true"
        _TMP_DIR.mkdir(exist_ok=True)

    def _rate_limit(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._rate_delay:
            time.sleep(self._rate_delay - elapsed)
        self._last_call = time.monotonic()

    def fetch_table(
        self,
        table: str,
        fields: List[str] = None,
        filter_formula: str = None,
        max_records: int = None,
    ) -> List[Dict]:
        self._rate_limit()
        t = self._api.table(self.base_id, table)
        kwargs: Dict[str, Any] = {}
        if fields:
            kwargs["fields"] = fields
        if filter_formula:
            kwargs["formula"] = filter_formula
        if max_records:
            kwargs["max_records"] = max_records
        records = t.all(**kwargs)
        return [
            {"id": r["id"], "created_time": r.get("createdTime"), "fields": r["fields"]}
            for r in records
        ]

    def update_record(
        self,
        table: str,
        record_id: str,
        fields: Dict[str, Any],
        dry_run: bool = False,
        trigger: str = None,
        reason: str = None,
    ) -> Dict:
        if not self.writes_enabled:
            logger.warning("AGENT_WRITES_ENABLED=false — write skipped: %s %s %s", table, record_id, fields)
            return {"id": record_id, "fields": fields, "skipped": True, "reason": "writes_disabled"}

        for field in fields:
            if field not in WRITABLE_FIELDS:
                raise ValueError(f"Field '{field}' not in write whitelist {WRITABLE_FIELDS}")

        # Read current record once (needed for transition guard + write log)
        self._rate_limit()
        t = self._api.table(self.base_id, table)
        current_fields = t.get(record_id).get("fields", {})

        # Status transition guard
        if "Editing Status" in fields:
            new_status = fields["Editing Status"]
            current_status = current_fields.get("Editing Status")
            if current_status and current_status in VALID_TRANSITIONS:
                allowed = VALID_TRANSITIONS[current_status]
                if new_status not in allowed:
                    raise ValueError(
                        f"Invalid status transition: '{current_status}' → '{new_status}'. "
                        f"Allowed: {allowed}"
                    )

        if dry_run:
            for field, new_val in fields.items():
                entry = self._make_log_entry(table, record_id, field, current_fields.get(field), new_val, trigger, reason)
                entry["dry_run"] = True
                logger.info("DRY RUN write: %s", json.dumps(entry))
            return {"id": record_id, "fields": fields, "dry_run": True}

        self._rate_limit()
        updated = t.update(record_id, fields)

        for field, new_val in fields.items():
            entry = self._make_log_entry(table, record_id, field, current_fields.get(field), new_val, trigger, reason)
            self._append_log(entry)

        return {"id": updated["id"], "fields": updated["fields"]}

    @staticmethod
    def _make_log_entry(table, record_id, field, old_value, new_value, trigger, reason) -> Dict:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "table": table,
            "record_id": record_id,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "trigger": trigger,
            "reason": reason,
        }

    @staticmethod
    def _append_log(entry: Dict):
        _TMP_DIR.mkdir(exist_ok=True)
        with open(_WRITE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")

    @staticmethod
    def build_linked_record_filter(record_id: str, field: str) -> str:
        return f"FIND('{record_id}', ARRAYJOIN({{{field}}}))"

    def get_client_map(self) -> Dict[str, str]:
        records = self.fetch_table("Clients", fields=["Name"])
        return {r["id"]: r["fields"].get("Name", "Unknown") for r in records}

    def get_editor_map(self) -> Dict[str, str]:
        records = self.fetch_table("Team", fields=["Name"])
        return {r["id"]: r["fields"].get("Name", "Unknown") for r in records}

    @staticmethod
    def format_video_ref(fields: Dict, client_map: Dict[str, str] = None) -> str:
        client_name = "Unknown"
        client_ids = fields.get("Client", [])
        if client_map and client_ids:
            if isinstance(client_ids, list) and client_ids:
                client_name = client_map.get(client_ids[0], "Unknown")
            elif isinstance(client_ids, str):
                client_name = client_map.get(client_ids, "Unknown")
        if client_name == "Unknown":
            client_name = (
                fields.get("Client Name")
                or fields.get("Client name")
                or "Unknown"
            )
        video_num = fields.get("Video Number", "?")
        video_type = "Shorts" if "short" in str(fields.get("Format", "")).lower() else "Video"
        return f"{client_name} {video_type} #{video_num}"

    @staticmethod
    def resolve_editor_name(fields: Dict, editor_map: Dict[str, str] = None) -> str:
        editor_name = fields.get("Editor's Name")
        if isinstance(editor_name, list):
            editor_name = editor_name[0] if editor_name else None
        if editor_name and not editor_name.startswith("rec"):
            return editor_name
        editor_ids = fields.get("Assigned Editor", [])
        if editor_map and editor_ids:
            if isinstance(editor_ids, list) and editor_ids:
                return editor_map.get(editor_ids[0], "Unassigned")
            elif isinstance(editor_ids, str):
                return editor_map.get(editor_ids, "Unassigned")
        return editor_name or "Unassigned"
