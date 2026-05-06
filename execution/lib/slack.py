import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

_project_root = Path(__file__).parent.parent.parent
load_dotenv(_project_root / ".env")

logger = logging.getLogger(__name__)


class SlackClient:
    def __init__(self, user_token: str = None, bot_token: str = None):
        self._user_token = user_token or os.getenv("SLACK_USER_TOKEN")
        self._bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN")
        if not self._user_token and not self._bot_token:
            raise ValueError("SLACK_USER_TOKEN or SLACK_BOT_TOKEN required")
        read_token = self._user_token or self._bot_token
        write_token = self._bot_token or self._user_token
        self._reader = WebClient(token=read_token)
        self._writer = WebClient(token=write_token)
        self._channel_cache: Dict[str, str] = {}  # name → id
        self._user_cache: Dict[str, Dict] = {}    # user_id → {name, username}

    def _resolve_channel_id(self, channel: str) -> str:
        if channel.startswith(("C", "G", "D")):
            return channel
        name = channel.lstrip("#")
        if name in self._channel_cache:
            return self._channel_cache[name]
        self.list_channels()  # populate cache
        return self._channel_cache.get(name, channel)

    def _get_user_info(self, user_id: str) -> Dict:
        if not user_id or user_id == "unknown":
            return {"name": "Unknown", "username": "unknown"}
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            info = self._reader.users_info(user=user_id)
            data = {
                "name": info["user"].get("real_name", "Unknown"),
                "username": info["user"].get("name", "unknown"),
            }
        except SlackApiError:
            data = {"name": "Unknown", "username": user_id}
        self._user_cache[user_id] = data
        return data

    def read_channel(
        self,
        channel: str,
        hours: float = None,
        limit: int = 100,
        include_threads: bool = True,
        max_threads: int = 50,
    ) -> List[Dict]:
        channel_id = self._resolve_channel_id(channel)
        kwargs: Dict = {"channel": channel_id, "limit": limit}
        if hours:
            oldest = (datetime.now() - timedelta(hours=hours)).timestamp()
            kwargs["oldest"] = str(oldest)
        try:
            response = self._reader.conversations_history(**kwargs)
        except SlackApiError as e:
            raise RuntimeError(f"Slack read_channel error: {e.response['error']}")

        messages = response["messages"]
        formatted = []
        for msg in messages:
            uid = msg.get("user", "unknown")
            udata = self._get_user_info(uid)
            ts = float(msg.get("ts", 0))
            formatted.append({
                "text": msg.get("text", ""),
                "user": udata["name"],
                "username": udata["username"],
                "user_id": uid,
                "timestamp": msg.get("ts"),
                "datetime": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                "thread_ts": msg.get("thread_ts"),
                "reply_count": msg.get("reply_count", 0),
                "reactions": msg.get("reactions", []),
                "thread_replies": [],
            })

        if include_threads:
            threads_fetched = 0
            for fmt_msg, raw_msg in zip(formatted, messages):
                if raw_msg.get("reply_count", 0) > 0 and threads_fetched < max_threads:
                    try:
                        result = self._reader.conversations_replies(
                            channel=channel_id, ts=raw_msg["ts"]
                        )
                        for reply in result.get("messages", [])[1:]:
                            ruid = reply.get("user", "unknown")
                            rudata = self._get_user_info(ruid)
                            rts = float(reply.get("ts", 0))
                            fmt_msg["thread_replies"].append({
                                "text": reply.get("text", ""),
                                "user": rudata["name"],
                                "username": rudata["username"],
                                "user_id": ruid,
                                "timestamp": reply.get("ts"),
                                "datetime": datetime.fromtimestamp(rts).strftime("%Y-%m-%d %H:%M:%S"),
                                "reactions": reply.get("reactions", []),
                            })
                        threads_fetched += 1
                    except SlackApiError:
                        pass

        return formatted

    def list_channels(self, filter_regex: str = None) -> List[Dict]:
        try:
            response = self._reader.conversations_list(
                types="public_channel,private_channel", limit=1000
            )
        except SlackApiError as e:
            raise RuntimeError(f"Slack list_channels error: {e.response['error']}")

        channels = response["channels"]
        for ch in channels:
            self._channel_cache[ch["name"]] = ch["id"]

        if filter_regex:
            pat = re.compile(filter_regex, re.IGNORECASE)
            channels = [ch for ch in channels if pat.search(ch["name"])]

        return [
            {
                "id": ch["id"],
                "name": ch["name"],
                "is_archived": ch.get("is_archived", False),
                "is_private": ch.get("is_private", False),
                "num_members": ch.get("num_members", 0),
            }
            for ch in channels
        ]

    def send_dm(self, user_id: str, text: str, blocks: List[Dict] = None) -> Dict:
        try:
            response = self._writer.conversations_open(users=[user_id])
            channel_id = response["channel"]["id"]
        except SlackApiError as e:
            raise RuntimeError(f"Slack open_dm error: {e.response['error']}")
        return self._post_message(channel_id, text, blocks)

    def send_channel_message(self, channel_id: str, text: str, blocks: List[Dict] = None) -> Dict:
        resolved = self._resolve_channel_id(channel_id)
        return self._post_message(resolved, text, blocks)

    def _post_message(self, channel: str, text: str, blocks: List[Dict] = None) -> Dict:
        kwargs: Dict = {"channel": channel, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        try:
            response = self._writer.chat_postMessage(**kwargs)
            return {"ok": True, "channel": response["channel"], "ts": response["ts"]}
        except SlackApiError as e:
            raise RuntimeError(f"Slack send error: {e.response['error']}")
