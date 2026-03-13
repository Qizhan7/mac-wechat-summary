"""Bookmark system - track last-read position and last summary time per chat."""
import json
import os
import time
from datetime import datetime

from .config import DATA_DIR

BOOKMARKS_FILE = os.path.join(DATA_DIR, "bookmarks.json")


def load_bookmarks():
    """Load all bookmarks."""
    if not os.path.exists(BOOKMARKS_FILE):
        return {}
    try:
        with open(BOOKMARKS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_bookmarks(bookmarks):
    """Save all bookmarks."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BOOKMARKS_FILE, "w") as f:
        json.dump(bookmarks, f, indent=2, ensure_ascii=False)


def _get_entry(username):
    """Get bookmark entry for a chat (compatible with legacy format)."""
    bookmarks = load_bookmarks()
    entry = bookmarks.get(username)
    if entry is None:
        return {"msg_ts": 0, "summary_time": ""}
    # Legacy format: raw int timestamp
    if isinstance(entry, (int, float)):
        return {"msg_ts": int(entry), "summary_time": ""}
    return entry


def get_bookmark(username):
    """Get last-read timestamp for a chat."""
    return _get_entry(username).get("msg_ts", 0)


def get_summary_time(username):
    """Get last summary time for a chat (human-readable string)."""
    return _get_entry(username).get("summary_time", "")


def clear_all_bookmarks():
    """Clear all bookmarks, reset all chats to summarize from scratch."""
    save_bookmarks({})


def set_bookmark(username, timestamp=None):
    """Set read position for a chat and record summary time."""
    if timestamp is None:
        timestamp = int(time.time())
    bookmarks = load_bookmarks()

    # Handle legacy format
    old = bookmarks.get(username)
    if isinstance(old, (int, float)):
        old = {"msg_ts": int(old), "summary_time": ""}
    elif old is None:
        old = {"msg_ts": 0, "summary_time": ""}

    old["msg_ts"] = timestamp
    old["summary_time"] = datetime.now().strftime("%m-%d %H:%M")
    bookmarks[username] = old
    save_bookmarks(bookmarks)
