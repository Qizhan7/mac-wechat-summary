"""
微信群聊 MCP Server - 通过 AI 代理查询和总结微信消息

基于 FastMCP，复用现有 core/ 和 ai/ 模块。
STDIO 传输，供 Claude Desktop / Claude Code 使用。

使用方法：
  python mcp_server.py          # 直接运行
  mcp dev mcp_server.py         # MCP Inspector 调试

Claude Desktop 配置 (~/Library/Application Support/Claude/claude_desktop_config.json)：
  {
    "mcpServers": {
      "wechat-summary": {
        "command": "/path/to/.venv/bin/python3",
        "args": ["/path/to/mcp_server.py"]
      }
    }
  }
"""
import os
import sys
import time
from datetime import datetime

# 确保项目根目录可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "wechat-summary",
    instructions=(
        "微信群聊查询、AI 总结和消息发送工具。"
        "可以读取群聊/私聊消息、搜索关键词、用 AI 总结内容、管理分组、"
        "以及通过微信桌面端发送消息。"
        "首次使用请先调用 get_status 确认连接状态。"
        "发送消息前务必向用户确认内容和目标。"
    ),
)

# ── 懒加载单例 ──────────────────────────────────────────

_db = None


def _get_db():
    """懒加载 WeChatDB，首次调用时初始化"""
    global _db
    if _db is not None:
        return _db

    from core.config import load_config
    from core.key_extractor import get_cached_keys
    from core.wechat_db import WeChatDB

    cfg = load_config()
    db_dir = cfg.get("db_dir", "")
    if not db_dir or not os.path.isdir(db_dir):
        raise RuntimeError(
            "未找到微信数据库目录。请先运行菜单栏 app 完成初始化，"
            "或在 ~/.wechat-summary/config.json 中设置 db_dir。"
        )

    keys = get_cached_keys()
    if not keys:
        raise RuntimeError(
            "未找到数据库密钥。请先运行菜单栏 app 的「重新提取密钥」功能。"
        )

    _db = WeChatDB(db_dir, keys)
    return _db


def _get_ai():
    """创建 AI 提供者实例"""
    from ai.factory import create_provider
    from core.config import load_config

    cfg = load_config()
    return create_provider(cfg), cfg


def _resolve(chat_name: str):
    """将群名/昵称解析为 (username, display_name)"""
    db = _get_db()
    username = db.resolve_username(chat_name)
    if not username:
        raise ValueError(f"找不到群聊或联系人：{chat_name}")
    db._load_contacts()
    display = db._contacts.get(username, username)
    return username, display


# ══════════════════════════════════════════════════════════
#  A. 状态与发现
# ══════════════════════════════════════════════════════════


@mcp.tool()
def get_status() -> str:
    """检查微信数据连接状态。

    返回数据库路径、密钥数量、群聊数量、微信是否在运行等信息。
    这是使用其他工具前的第一步，用于确认系统是否就绪。
    """
    from core.config import load_config
    from core.key_extractor import get_cached_keys, is_wechat_running

    lines = ["=== 微信数据连接状态 ==="]

    cfg = load_config()
    db_dir = cfg.get("db_dir", "")
    lines.append(f"数据库目录: {db_dir or '未配置'}")
    lines.append(f"目录存在: {'✅' if os.path.isdir(db_dir) else '❌'}")

    keys = get_cached_keys()
    lines.append(f"密钥数量: {len(keys) if keys else 0}")

    lines.append(f"微信运行中: {'✅' if is_wechat_running() else '❌'}")

    ai_provider = cfg.get("ai_provider", "未配置")
    ai_model = cfg.get("ai_model", "默认")
    lines.append(f"AI 提供者: {ai_provider} ({ai_model})")

    try:
        db = _get_db()
        groups = db.get_groups()
        lines.append(f"群聊数量: {len(groups)}")
        lines.append("\n状态: ✅ 就绪，可以使用所有工具")
    except Exception as e:
        lines.append(f"\n状态: ❌ {e}")

    return "\n".join(lines)


@mcp.tool()
def list_chats(chat_type: str = "all") -> str:
    """列出微信群聊和/或私聊联系人。

    Args:
        chat_type: 筛选类型
            - "all": 列出群聊和私聊（默认）
            - "group": 只列出群聊
            - "private": 只列出私聊联系人
    """
    try:
        db = _get_db()
        db._load_contacts()

        groups = []
        privates = []
        for c in db._contacts_full:
            name = c["remark"] or c["nick_name"] or c["username"]
            if "@chatroom" in c["username"]:
                groups.append(name)
            elif not c["username"].startswith("gh_") and "@" not in c["username"]:
                # 排除公众号(gh_)和特殊账号
                privates.append(name)

        lines = []
        if chat_type in ("all", "group"):
            lines.append(f"群聊（{len(groups)}个）：\n")
            for name in groups:
                lines.append(f"  • {name}")
            lines.append("")

        if chat_type in ("all", "private"):
            lines.append(f"私聊联系人（{len(privates)}个）：\n")
            for name in privates[:100]:  # 联系人可能很多，限制显示
                lines.append(f"  • {name}")
            if len(privates) > 100:
                lines.append(f"  ... 还有 {len(privates) - 100} 个联系人")

        return "\n".join(lines)
    except Exception as e:
        return f"获取列表失败: {e}"


# ══════════════════════════════════════════════════════════
#  B. 消息读取
# ══════════════════════════════════════════════════════════


@mcp.tool()
def get_recent_sessions(limit: int = 20) -> str:
    """获取最近的微信会话列表，包含最新消息摘要和未读数。

    包括群聊和私聊，按最后消息时间倒序排列。

    Args:
        limit: 返回会话数量，默认 20
    """
    try:
        db = _get_db()
        sessions = db.get_recent_sessions(limit=limit)
        if not sessions:
            return "没有最近的会话。"

        lines = [f"最近 {len(sessions)} 个会话：\n"]
        for s in sessions:
            tag = "群" if s["is_group"] else "私"
            unread = f" ({s['unread']}条未读)" if s["unread"] else ""
            summary = s["summary"][:50] + "..." if len(s["summary"]) > 50 else s["summary"]
            lines.append(f"[{tag}] {s['name']}{unread}  {s['time_str']}")
            if summary:
                lines.append(f"     {summary}")
        return "\n".join(lines)
    except Exception as e:
        return f"获取会话列表失败: {e}"


@mcp.tool()
def read_messages(chat_name: str, limit: int = 100, hours: int = 0) -> str:
    """读取指定群聊或私聊的消息记录。

    Args:
        chat_name: 群聊名称或聊天对象名字，支持模糊匹配
        limit: 最大消息数，默认 100
        hours: 只读取最近 N 小时的消息（0 表示不限时间，读取最近 limit 条）
    """
    try:
        username, display = _resolve(chat_name)
        db = _get_db()

        since_ts = 0
        if hours > 0:
            since_ts = time.time() - hours * 3600

        messages = db.get_messages(username, since_ts=since_ts, limit=limit)
        if not messages:
            return f"{display}: 没有{'最近 ' + str(hours) + ' 小时内的' if hours else ''}消息"

        lines = [f"📨 {display} — {len(messages)} 条消息\n"]
        for msg in messages:
            if msg["type"] in (10000, 10002):
                continue
            if msg["sender"]:
                lines.append(f"[{msg['time_str']}] {msg['sender']}: {msg['text']}")
            else:
                lines.append(f"[{msg['time_str']}] {msg['text']}")
        return "\n".join(lines)
    except Exception as e:
        return f"读取消息失败: {e}"


@mcp.tool()
def get_chat_images(
    chat_name: str,
    limit: int = 5,
    hours: int = 0,
    full_size: bool = False,
) -> list:
    """获取聊天中的图片和表情包，返回图片内容供 AI 查看和理解。

    同时支持普通图片（type=3）和表情包/动图（type=47）。
    读取本地文件或从 CDN 下载表情包，以 base64 编码返回。
    默认返回缩略图（较小较快），使用 full_size=True 获取原图。

    注意：2026 年 3 月起微信启用 V2 加密格式，该格式图片暂不支持查看，
    会自动跳过。2026 年 2 月及之前的图片均可正常查看。

    Args:
        chat_name: 群聊名称或聊天对象名字，支持模糊匹配
        limit: 最多返回几张图片，默认 5（最大 10）
        hours: 只读取最近 N 小时的图片（0 表示不限时间，读取最近的图片）
        full_size: 是否返回原图而非缩略图，默认 False
    """
    import base64
    from mcp.types import ImageContent, TextContent

    try:
        username, display = _resolve(chat_name)
        db = _get_db()

        limit = min(limit, 10)

        since_ts = 0
        if hours > 0:
            since_ts = time.time() - hours * 3600

        # 同时获取普通图片和表情包
        image_msgs = db.get_image_messages(
            username, since_ts=since_ts, limit=limit
        )
        emoji_msgs = db.get_emoji_messages(
            username, since_ts=since_ts, limit=limit
        )

        # 合并并按时间排序，取 limit 条
        all_msgs = image_msgs + emoji_msgs
        all_msgs.sort(key=lambda m: m["timestamp"])
        if since_ts > 0:
            all_msgs = all_msgs[:limit]
        else:
            all_msgs = all_msgs[-limit:]

        if not all_msgs:
            return [TextContent(
                type="text",
                text=(
                    f"{display}: 没有"
                    f"{'最近 ' + str(hours) + ' 小时内的' if hours else ''}"
                    f"图片或表情包消息"
                ),
            )]

        img_count = sum(1 for m in all_msgs if m.get("msg_type") != "emoji")
        emoji_count = sum(1 for m in all_msgs if m.get("msg_type") == "emoji")
        header_parts = []
        if img_count:
            header_parts.append(f"{img_count} 张图片")
        if emoji_count:
            header_parts.append(f"{emoji_count} 个表情包")
        results = [TextContent(
            type="text",
            text=f"📷 {display} — {'、'.join(header_parts)}\n",
        )]

        MAX_TOTAL_BYTES = 3 * 1024 * 1024  # 3MB 总图片数据上限
        found_count = 0
        total_bytes = 0
        skipped_v2 = 0
        skipped_size = False
        for msg in all_msgs:
            sender_info = f"{msg['sender']}发送" if msg.get("sender") else "发送"
            is_emoji = msg.get("msg_type") == "emoji"
            type_label = "表情包" if is_emoji else "图片"

            results.append(TextContent(
                type="text",
                text=f"[{msg['time_str']}] {sender_info}的{type_label}：",
            ))

            if is_emoji:
                # 表情包：从 CDN 下载
                image_data = _download_emoji(msg)
                if image_data is None:
                    results.append(TextContent(
                        type="text",
                        text="  (表情包下载失败，CDN 链接可能已过期)",
                    ))
                    continue
            else:
                # 普通图片：读取本地文件
                if full_size:
                    file_path = msg.get("image_path") or msg.get("thumb_path")
                else:
                    file_path = msg.get("thumb_path") or msg.get("image_path")

                if not file_path or not os.path.isfile(file_path):
                    results.append(TextContent(
                        type="text",
                        text="  (图片文件未找到，可能已被清理)",
                    ))
                    continue

                try:
                    with open(file_path, "rb") as f:
                        image_data = f.read()
                except OSError as e:
                    results.append(TextContent(
                        type="text",
                        text=f"  (读取失败: {e})",
                    ))
                    continue

                # V2 加密格式 (WeChat 2026+) — 尝试解密
                if image_data[:6] == b"\x07\x08\x56\x32\x08\x07":
                    from core.wechat_db import WeChatDB
                    from core.config import load_config

                    aes_key = load_config().get("image_aes_key", "")
                    if aes_key:
                        decoded = WeChatDB.decode_image_v2(image_data, aes_key)
                        if decoded:
                            image_data = decoded
                        else:
                            results.append(TextContent(
                                type="text",
                                text="  (V2 解密失败，密钥可能已过期)",
                            ))
                            continue
                    else:
                        results.append(TextContent(
                            type="text",
                            text="  (V2 加密图片，需要提取密钥后才能查看)",
                        ))
                        skipped_v2 += 1
                        continue

            # 检测 MIME 类型
            mime_type = _detect_mime(image_data)
            if not mime_type:
                results.append(TextContent(
                    type="text",
                    text="  (不支持的图片格式)",
                ))
                continue

            if len(image_data) > 5 * 1024 * 1024:
                size_mb = len(image_data) / (1024 * 1024)
                results.append(TextContent(
                    type="text",
                    text=f"  (图片太大: {size_mb:.1f}MB，跳过)",
                ))
                continue

            if total_bytes + len(image_data) > MAX_TOTAL_BYTES:
                results.append(TextContent(
                    type="text",
                    text="  (已达到总数据量上限，跳过剩余图片)",
                ))
                skipped_size = True
                break

            b64_data = base64.b64encode(image_data).decode()
            results.append(ImageContent(
                type="image",
                data=b64_data,
                mimeType=mime_type,
            ))
            found_count += 1
            total_bytes += len(image_data)

        summary = f"\n共获取 {found_count}/{len(all_msgs)} 张图片/表情包"
        if skipped_size:
            summary += "\n📦 因数据量上限跳过了部分图片，可减少 limit 值"
        if skipped_v2 > 0:
            summary += "\n💡 部分图片为 V2 加密格式，需要提取密钥才能查看"
        results.append(TextContent(type="text", text=summary))
        return results

    except Exception as e:
        from mcp.types import TextContent

        return [TextContent(type="text", text=f"获取图片失败: {e}")]


def _detect_mime(data: bytes) -> str | None:
    """检测图片数据的 MIME 类型"""
    if not data or len(data) < 4:
        return None
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) > 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _download_emoji(msg: dict, timeout: int = 10) -> bytes | None:
    """从 CDN 下载表情包图片

    Args:
        msg: 表情包消息字典，需包含 cdnurl
        timeout: 下载超时秒数

    Returns:
        bytes | None: 图片数据，失败返回 None
    """
    import urllib.request

    cdnurl = msg.get("cdnurl", "")
    if not cdnurl:
        return None

    try:
        req = urllib.request.Request(
            cdnurl,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        # 限制最大读取 2MB
        data = resp.read(2 * 1024 * 1024)

        # 验证是否为有效图片
        if _detect_mime(data):
            return data

        return None
    except Exception:
        return None


@mcp.tool()
def search_messages(keywords: str, chat_names: str = "", days: int = 30) -> str:
    """跨群搜索微信消息。支持多关键词（空格分隔，AND 逻辑）。

    Args:
        keywords: 搜索关键词，多个关键词用空格分隔（同时包含才匹配）
        chat_names: 限定搜索范围的群聊名称，逗号分隔（留空搜索所有群）
        days: 搜索最近多少天的消息，默认 30
    """
    try:
        db = _get_db()
        end_ts = time.time()
        start_ts = end_ts - days * 86400

        # 解析群聊范围
        usernames = []
        if chat_names.strip():
            for name in chat_names.split(","):
                name = name.strip()
                if not name:
                    continue
                uname = db.resolve_username(name)
                if uname:
                    usernames.append(uname)

        results = db.search_messages(
            keywords=keywords.split(),
            usernames=usernames or None,
            start_ts=start_ts,
            end_ts=end_ts,
        )

        if not results:
            return f"未搜索到包含「{keywords}」的消息（最近 {days} 天）"

        db._load_contacts()
        total = sum(len(msgs) for msgs in results.values())
        lines = [f"🔍 搜索「{keywords}」— 共 {total} 条结果\n"]

        for username, messages in results.items():
            if not messages:
                continue
            group_name = db._contacts.get(username, username)
            lines.append(f"\n--- {group_name}（{len(messages)}条）---")
            for msg in messages[:50]:  # 每群最多显示 50 条
                if msg["sender"]:
                    lines.append(f"[{msg['time_str']}] {msg['sender']}: {msg['text']}")
                else:
                    lines.append(f"[{msg['time_str']}] {msg['text']}")

        return "\n".join(lines)
    except Exception as e:
        return f"搜索失败: {e}"


@mcp.tool()
def count_new_messages(chat_name: str) -> str:
    """统计指定群聊自上次总结以来的新消息数量。

    用于快速判断是否需要总结。

    Args:
        chat_name: 群聊名称，支持模糊匹配
    """
    try:
        username, display = _resolve(chat_name)
        db = _get_db()

        from core.bookmark import get_bookmark, get_summary_time

        bookmark_ts = get_bookmark(username)
        summary_time = get_summary_time(username)
        count = db.count_messages_since(username, bookmark_ts)

        if bookmark_ts == 0:
            return f"{display}: 尚未总结过（共有新消息约 {count} 条）"

        return (
            f"{display}: 上次总结于 {summary_time or '未知'}，"
            f"之后有 {count} 条新消息"
        )
    except Exception as e:
        return f"统计失败: {e}"


# ══════════════════════════════════════════════════════════
#  C. AI 总结
# ══════════════════════════════════════════════════════════


@mcp.tool()
def summarize_chat(
    chat_name: str,
    hours: int = 0,
    limit: int = 500,
    update_bookmark: bool = True,
) -> str:
    """用 AI 总结指定群聊的消息。

    默认总结上次书签位置之后的新消息。可通过 hours 指定时间范围。

    Args:
        chat_name: 群聊名称，支持模糊匹配
        hours: 总结最近 N 小时的消息（0 表示从上次书签位置开始）
        limit: 最大消息数，默认 500
        update_bookmark: 总结后是否更新书签位置，默认 True
    """
    try:
        username, display = _resolve(chat_name)
        db = _get_db()

        # 确定时间范围
        if hours > 0:
            since_ts = time.time() - hours * 3600
        else:
            from core.bookmark import get_bookmark
            since_ts = get_bookmark(username)

        messages = db.get_messages(username, since_ts=since_ts, limit=limit)
        if not messages:
            return f"{display}: 没有新消息需要总结"

        messages_text = db.format_messages_for_ai(messages)
        start_time = messages[0]["time_str"]
        end_time = messages[-1]["time_str"]
        msg_count = len(messages)

        ai, cfg = _get_ai()
        prompt = ai.build_prompt(
            group_name=display,
            messages_text=messages_text,
            start_time=start_time,
            end_time=end_time,
            msg_count=msg_count,
        )
        summary = ai.summarize(prompt)

        if update_bookmark and messages:
            from core.bookmark import set_bookmark
            set_bookmark(username, messages[-1]["timestamp"])

        header = (
            f"📋 {display} 总结\n"
            f"时间范围: {start_time} ~ {end_time}  |  消息数: {msg_count}\n"
            f"{'=' * 40}\n"
        )
        return header + summary

    except Exception as e:
        return f"总结失败: {e}"


@mcp.tool()
def summarize_group_batch(group_name: str, hours: int = 0) -> str:
    """批量总结一个分组下所有群聊的消息。

    分组通过菜单栏 app 或 manage_chat_groups 工具创建。

    Args:
        group_name: 分组名称（如「工作群」「购物群」）
        hours: 总结最近 N 小时的消息（0 表示从上次书签位置开始）
    """
    try:
        from core.bookmark import get_bookmark, set_bookmark
        from core.chat_groups import get_group_chats

        chat_usernames = get_group_chats(group_name)
        if not chat_usernames:
            return f"分组「{group_name}」不存在或没有群聊"

        db = _get_db()
        db._load_contacts()
        groups_data = []

        for username in chat_usernames:
            display = db._contacts.get(username, username)

            if hours > 0:
                since_ts = time.time() - hours * 3600
            else:
                since_ts = get_bookmark(username)

            messages = db.get_messages(username, since_ts=since_ts, limit=500)
            msg_count = len(messages)

            if msg_count > 0:
                messages_text = db.format_messages_for_ai(messages)
                start_time = messages[0]["time_str"]
                end_time = messages[-1]["time_str"]
                # 更新书签
                set_bookmark(username, messages[-1]["timestamp"])
            else:
                messages_text = ""
                start_time = ""
                end_time = ""

            groups_data.append({
                "name": display,
                "messages_text": messages_text,
                "start_time": start_time,
                "end_time": end_time,
                "msg_count": msg_count,
            })

        # 如果所有群都没有新消息
        total = sum(g["msg_count"] for g in groups_data)
        if total == 0:
            return f"分组「{group_name}」下所有群聊都没有新消息"

        ai, cfg = _get_ai()
        prompt = ai.build_batch_prompt(group_name, groups_data)
        summary = ai.summarize(prompt)

        header = f"📋 分组「{group_name}」批量总结（{len(groups_data)}个群，共{total}条消息）\n{'=' * 40}\n"
        return header + summary

    except Exception as e:
        return f"批量总结失败: {e}"


@mcp.tool()
def summarize_search_results(
    keywords: str,
    chat_names: str = "",
    days: int = 30,
) -> str:
    """搜索消息并用 AI 总结搜索结果。

    先跨群搜索关键词，然后对命中的消息进行 AI 归纳总结。

    Args:
        keywords: 搜索关键词，空格分隔
        chat_names: 限定搜索的群聊名称，逗号分隔（留空搜索所有群）
        days: 搜索最近多少天，默认 30
    """
    try:
        db = _get_db()
        end_ts = time.time()
        start_ts = end_ts - days * 86400

        usernames = []
        if chat_names.strip():
            for name in chat_names.split(","):
                name = name.strip()
                if not name:
                    continue
                uname = db.resolve_username(name)
                if uname:
                    usernames.append(uname)

        results = db.search_messages(
            keywords=keywords.split(),
            usernames=usernames or None,
            start_ts=start_ts,
            end_ts=end_ts,
        )

        if not results:
            return f"未搜索到包含「{keywords}」的消息，无法生成总结"

        # 为搜索结果添加 group_name
        db._load_contacts()
        for username, messages in results.items():
            group_name_display = db._contacts.get(username, username)
            for msg in messages:
                msg["group_name"] = group_name_display

        start_time_str = datetime.fromtimestamp(start_ts).strftime("%m-%d %H:%M")
        end_time_str = datetime.fromtimestamp(end_ts).strftime("%m-%d %H:%M")

        ai, cfg = _get_ai()
        prompt = ai.build_search_prompt(keywords, results, start_time_str, end_time_str)
        summary = ai.summarize(prompt)

        total = sum(len(msgs) for msgs in results.values())
        header = f"🔍 搜索「{keywords}」AI 总结（{total}条命中，最近{days}天）\n{'=' * 40}\n"
        return header + summary

    except Exception as e:
        return f"搜索总结失败: {e}"


# ══════════════════════════════════════════════════════════
#  D. 管理
# ══════════════════════════════════════════════════════════


@mcp.tool()
def get_bookmark_status(chat_name: str = "") -> str:
    """查看群聊的书签（上次总结位置）状态。

    Args:
        chat_name: 群聊名称（留空查看所有有书签的群聊）
    """
    try:
        from core.bookmark import get_bookmark, get_summary_time, load_bookmarks

        if chat_name:
            username, display = _resolve(chat_name)
            ts = get_bookmark(username)
            summary_time = get_summary_time(username)
            if ts == 0:
                return f"{display}: 尚未总结过"
            time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            return f"{display}: 上次读到 {time_str}，总结于 {summary_time or '未知'}"

        # 列出所有书签
        bookmarks = load_bookmarks()
        if not bookmarks:
            return "还没有任何书签记录。"

        db = _get_db()
        db._load_contacts()
        lines = [f"共 {len(bookmarks)} 个书签：\n"]
        for username, entry in bookmarks.items():
            display = db._contacts.get(username, username)
            if isinstance(entry, (int, float)):
                ts, st = int(entry), ""
            else:
                ts = entry.get("msg_ts", 0)
                st = entry.get("summary_time", "")
            time_str = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "无"
            lines.append(f"  • {display}: 读到 {time_str}，总结于 {st or '未知'}")
        return "\n".join(lines)
    except Exception as e:
        return f"获取书签失败: {e}"


@mcp.tool()
def manage_chat_groups(
    action: str,
    group_name: str = "",
    chat_name: str = "",
) -> str:
    """管理群聊分组。分组可用于批量总结。

    Args:
        action: 操作类型，可选值:
            - "list": 列出所有分组及其包含的群聊
            - "create": 创建新分组（需要 group_name）
            - "delete": 删除分组（需要 group_name）
            - "add": 将群聊加入分组（需要 group_name 和 chat_name）
            - "remove": 将群聊从分组移除（需要 group_name 和 chat_name）
        group_name: 分组名称
        chat_name: 群聊名称（用于 add/remove 操作）
    """
    try:
        from core.chat_groups import (
            add_chat_to_group,
            create_group,
            delete_group,
            load_groups,
            remove_chat_from_group,
        )

        if action == "list":
            groups = load_groups()
            if not groups:
                return "还没有创建任何分组。"
            db = _get_db()
            db._load_contacts()
            lines = [f"共 {len(groups)} 个分组：\n"]
            for g in groups:
                chat_displays = [
                    db._contacts.get(c, c) for c in g["chats"]
                ]
                chats_str = "、".join(chat_displays) if chat_displays else "（空）"
                lines.append(f"📁 {g['name']}: {chats_str}")
            return "\n".join(lines)

        elif action == "create":
            if not group_name:
                return "请提供分组名称（group_name 参数）"
            ok = create_group(group_name)
            return f"✅ 分组「{group_name}」创建成功" if ok else f"❌ 分组「{group_name}」已存在"

        elif action == "delete":
            if not group_name:
                return "请提供分组名称（group_name 参数）"
            delete_group(group_name)
            return f"✅ 分组「{group_name}」已删除"

        elif action == "add":
            if not group_name or not chat_name:
                return "请提供分组名称（group_name）和群聊名称（chat_name）"
            username, display = _resolve(chat_name)
            ok = add_chat_to_group(group_name, username)
            return f"✅ 已将「{display}」加入分组「{group_name}」" if ok else f"❌ 分组「{group_name}」不存在"

        elif action == "remove":
            if not group_name or not chat_name:
                return "请提供分组名称（group_name）和群聊名称（chat_name）"
            username, display = _resolve(chat_name)
            ok = remove_chat_from_group(group_name, username)
            return f"✅ 已将「{display}」从分组「{group_name}」移除" if ok else f"❌ 分组「{group_name}」不存在"

        else:
            return f"未知操作: {action}。支持: list, create, delete, add, remove"

    except Exception as e:
        return f"分组操作失败: {e}"


@mcp.tool()
def get_ai_config() -> str:
    """查看当前 AI 总结配置（提供者、模型等）。

    AI 配置通过菜单栏 app 设置，MCP 服务端共享相同配置。
    """
    try:
        from core.config import load_config
        from core.keychain import load_key

        cfg = load_config()
        provider = cfg.get("ai_provider", "未配置")
        model = cfg.get("ai_model", "默认")
        has_key = bool(load_key("ai-api-key") or cfg.get("ai_api_key"))

        lines = [
            "=== AI 总结配置 ===",
            f"提供者: {provider}",
            f"模型: {model or '默认'}",
            f"API Key: {'✅ 已设置' if has_key else '❌ 未设置'}",
        ]

        if provider == "ollama":
            lines.append(f"Ollama 地址: {cfg.get('ollama_url', 'http://localhost:11434')}")
            lines.append(f"Ollama 模型: {cfg.get('ollama_model', 'qwen3:8b')}")

        if provider == "custom":
            lines.append(f"自定义 Base URL: {cfg.get('ai_base_url', '未设置')}")

        return "\n".join(lines)
    except Exception as e:
        return f"获取配置失败: {e}"


# ── E. 消息发送 ──────────────────────────────────────────


@mcp.tool()
def send_message(text: str, chat_name: str = "") -> str:
    """通过微信桌面端发送消息。

    使用 macOS AppleScript UI 自动化控制微信桌面端。
    需要微信已登录并在前台，运行此工具的 app 需要辅助功能权限。

    ⚠️ 重要：发送前请向用户确认消息内容和发送目标！

    Args:
        text: 要发送的消息内容
        chat_name: 目标聊天名称（群名或联系人名）。
                   留空则发送到微信当前打开的聊天。
    """
    try:
        from core.sender import send_message as _send

        target = chat_name if chat_name else None
        ok, msg = _send(text, target)
        return msg
    except ImportError:
        return "❌ 发送模块未找到，请确认 core/sender.py 存在"
    except Exception as e:
        return f"❌ 发送失败: {e}"


# ── 入口 ─────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
