"""Chat groups - organize chats into custom groups for batch summarization."""
import json
import os

from .config import DATA_DIR

GROUPS_FILE = os.path.join(DATA_DIR, "chat_groups.json")


def load_groups():
    """Load all groups.

    Returns:
        list[dict]: [{"name": "购物群", "chats": ["xxx@chatroom", ...]}, ...]
    """
    if not os.path.exists(GROUPS_FILE):
        return []
    try:
        with open(GROUPS_FILE) as f:
            data = json.load(f)
        return data.get("groups", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_groups(groups):
    """Save all groups."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(GROUPS_FILE, "w") as f:
        json.dump({"groups": groups}, f, indent=2, ensure_ascii=False)


def create_group(name):
    """Create a new group.

    Args:
        name: Group name.

    Returns:
        bool: True if created, False if name already exists.
    """
    groups = load_groups()
    if any(g["name"] == name for g in groups):
        return False
    groups.append({"name": name, "chats": []})
    save_groups(groups)
    return True


def delete_group(name):
    """Delete a group."""
    groups = load_groups()
    groups = [g for g in groups if g["name"] != name]
    save_groups(groups)


def rename_group(old_name, new_name):
    """Rename a group."""
    groups = load_groups()
    for g in groups:
        if g["name"] == old_name:
            g["name"] = new_name
            save_groups(groups)
            return True
    return False


def add_chat_to_group(group_name, chat_username):
    """Add a chat to a group.

    Args:
        group_name: Group name.
        chat_username: Chat username (xxx@chatroom).

    Returns:
        bool: True if the group exists, False otherwise.
    """
    groups = load_groups()
    for g in groups:
        if g["name"] == group_name:
            if chat_username not in g["chats"]:
                g["chats"].append(chat_username)
                save_groups(groups)
            return True
    return False


def remove_chat_from_group(group_name, chat_username):
    """Remove a chat from a group."""
    groups = load_groups()
    for g in groups:
        if g["name"] == group_name:
            if chat_username in g["chats"]:
                g["chats"].remove(chat_username)
                save_groups(groups)
            return True
    return False


def get_group_chats(group_name):
    """Get all chat usernames in a group."""
    groups = load_groups()
    for g in groups:
        if g["name"] == group_name:
            return list(g["chats"])
    return []


def get_chat_group(chat_username):
    """Find which group a chat belongs to, returns None if not in any group."""
    groups = load_groups()
    for g in groups:
        if chat_username in g["chats"]:
            return g["name"]
    return None


def set_group_summary_time(group_name, summary_time_str):
    """Set last summary time for a group."""
    groups = load_groups()
    for g in groups:
        if g["name"] == group_name:
            g["summary_time"] = summary_time_str
            save_groups(groups)
            return


def get_group_summary_time(group_name):
    """Get last summary time for a group."""
    groups = load_groups()
    for g in groups:
        if g["name"] == group_name:
            return g.get("summary_time", "")
    return ""
