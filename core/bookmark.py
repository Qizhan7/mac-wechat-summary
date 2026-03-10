"""
书签系统 - 记录每个群聊上次阅读到的位置 + 上次总结时间
"""
import json
import os
import time
from datetime import datetime

from .config import DATA_DIR

BOOKMARKS_FILE = os.path.join(DATA_DIR, "bookmarks.json")


def load_bookmarks():
    """加载所有书签"""
    if not os.path.exists(BOOKMARKS_FILE):
        return {}
    try:
        with open(BOOKMARKS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_bookmarks(bookmarks):
    """保存所有书签"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BOOKMARKS_FILE, "w") as f:
        json.dump(bookmarks, f, indent=2, ensure_ascii=False)


def _get_entry(username):
    """获取某个群的书签条目（兼容旧格式）"""
    bookmarks = load_bookmarks()
    entry = bookmarks.get(username)
    if entry is None:
        return {"msg_ts": 0, "summary_time": ""}
    # 旧格式兼容：直接是 int 时间戳
    if isinstance(entry, (int, float)):
        return {"msg_ts": int(entry), "summary_time": ""}
    return entry


def get_bookmark(username):
    """获取某个群的上次阅读时间戳"""
    return _get_entry(username).get("msg_ts", 0)


def get_summary_time(username):
    """获取某个群的上次总结时间（人类可读字符串）"""
    return _get_entry(username).get("summary_time", "")


def clear_all_bookmarks():
    """清除所有书签，让所有群聊从头开始总结"""
    save_bookmarks({})


def set_bookmark(username, timestamp=None):
    """设置某个群的阅读位置 + 记录总结时间"""
    if timestamp is None:
        timestamp = int(time.time())
    bookmarks = load_bookmarks()

    # 兼容旧格式
    old = bookmarks.get(username)
    if isinstance(old, (int, float)):
        old = {"msg_ts": int(old), "summary_time": ""}
    elif old is None:
        old = {"msg_ts": 0, "summary_time": ""}

    old["msg_ts"] = timestamp
    old["summary_time"] = datetime.now().strftime("%m-%d %H:%M")
    bookmarks[username] = old
    save_bookmarks(bookmarks)
