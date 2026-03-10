"""
群聊分组 - 将多个群聊归类到自定义分组，支持按组批量总结
"""
import json
import os

from .config import DATA_DIR

GROUPS_FILE = os.path.join(DATA_DIR, "chat_groups.json")


def load_groups():
    """加载所有分组

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
    """保存所有分组"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(GROUPS_FILE, "w") as f:
        json.dump({"groups": groups}, f, indent=2, ensure_ascii=False)


def create_group(name):
    """创建新分组

    Args:
        name: 分组名称

    Returns:
        bool: 是否成功（名称重复则失败）
    """
    groups = load_groups()
    if any(g["name"] == name for g in groups):
        return False
    groups.append({"name": name, "chats": []})
    save_groups(groups)
    return True


def delete_group(name):
    """删除分组"""
    groups = load_groups()
    groups = [g for g in groups if g["name"] != name]
    save_groups(groups)


def rename_group(old_name, new_name):
    """重命名分组"""
    groups = load_groups()
    for g in groups:
        if g["name"] == old_name:
            g["name"] = new_name
            save_groups(groups)
            return True
    return False


def add_chat_to_group(group_name, chat_username):
    """将群聊添加到分组

    Args:
        group_name: 分组名称
        chat_username: 群聊的 username (xxx@chatroom)

    Returns:
        bool: 是否成功
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
    """将群聊从分组中移除"""
    groups = load_groups()
    for g in groups:
        if g["name"] == group_name:
            if chat_username in g["chats"]:
                g["chats"].remove(chat_username)
                save_groups(groups)
            return True
    return False


def get_group_chats(group_name):
    """获取分组中的所有群聊 username 列表"""
    groups = load_groups()
    for g in groups:
        if g["name"] == group_name:
            return list(g["chats"])
    return []


def get_chat_group(chat_username):
    """查找群聊所在的分组名称，不在任何分组返回 None"""
    groups = load_groups()
    for g in groups:
        if chat_username in g["chats"]:
            return g["name"]
    return None


def set_group_summary_time(group_name, summary_time_str):
    """设置分组的上次总结时间"""
    groups = load_groups()
    for g in groups:
        if g["name"] == group_name:
            g["summary_time"] = summary_time_str
            save_groups(groups)
            return


def get_group_summary_time(group_name):
    """获取分组的上次总结时间"""
    groups = load_groups()
    for g in groups:
        if g["name"] == group_name:
            return g.get("summary_time", "")
    return ""
