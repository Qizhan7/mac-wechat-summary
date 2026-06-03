"""
WeChat Group Chat AI Summary - macOS menu bar tool
"""
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

import rumps

# --- For dialog top-most + custom dialogs ---
try:
    from AppKit import (NSApplication, NSAlert, NSTextField, NSView, NSObject,
                        NSButton, NSImage, NSFont, NSScrollView, NSTextView,
                        NSBezelBorder, NSPopUpButton)
    import objc
    _HAS_APPKIT = True
except ImportError:
    _HAS_APPKIT = False

# ── Menu open detection (NSMenuDelegate) ──────────────────────
if _HAS_APPKIT:
    class _MenuOpenDelegate(NSObject):
        """NSMenuDelegate: detect menu open events to trigger auto-refresh."""

        def init(self):
            self = objc.super(_MenuOpenDelegate, self).init()
            if self is None:
                return None
            self.app_ref = None
            self._last_refresh = 0.0
            return self

        def menuWillOpen_(self, menu):
            app = self.app_ref
            if not app:
                return
            # On menu click, if in done/error state and not summarizing, restore normal icon
            if (not app._summarizing
                    and getattr(app, '_current_status', None) in (ICON_DONE, ICON_ERROR)):
                app._set_status(ICON_NORMAL)
            if (app.config.get("auto_refresh_on_open")
                    and app.db and not app._summarizing):
                now = time.time()
                if now - self._last_refresh > 5:  # At least 5 seconds between refreshes
                    self._last_refresh = now
                    print("[auto-refresh] 菜单打开，后台刷新群聊...")
                    threading.Thread(target=app._do_silent_refresh, daemon=True).start()

from core.config import load_config, save_config, CONFIG_FILE, DATA_DIR
from core.keychain import save_key, load_key
from core.key_extractor import (
    is_wechat_running,
    is_wechat_signed,
    extract_keys,
    get_cached_keys,
    compile_scanner,
    check_new_databases,
)
from core.wechat_db import WeChatDB
from core.bookmark import get_bookmark, set_bookmark, get_summary_time, clear_all_bookmarks
from core.chat_groups import (
    load_groups, save_groups, create_group, delete_group,
    add_chat_to_group, remove_chat_from_group, get_group_chats, get_chat_group,
    set_group_summary_time, get_group_summary_time,
)
from core.knowledge import KNOWLEDGE_DB, OBSIDIAN_ROOT, KnowledgeStore
from core.monitor import HITS_DIR, MonitorConfigError, TopicMonitor, initialize_state_if_needed
from ai.factory import create_provider

# Summary history save directory
SUMMARY_DIR = os.path.join(DATA_DIR, "summaries")
os.makedirs(SUMMARY_DIR, exist_ok=True)

# AI service list
AI_PROVIDERS = [
    ("qwen", "通义千问 (推荐)"),
    ("deepseek", "DeepSeek"),
    ("ollama", "本地 Ollama (免费)"),
    ("claude", "Claude"),
    ("openai", "OpenAI"),
]

# Menu bar icon: prefer PNG (more reliable), emoji as fallback
_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
ICON_PNG = os.path.join(_ICON_DIR, "icon.png")
ICON_LOADING_PNG = os.path.join(_ICON_DIR, "icon_loading.png")
ICON_DONE_PNG = os.path.join(_ICON_DIR, "icon_done.png")
ICON_ERROR_PNG = os.path.join(_ICON_DIR, "icon_error.png")
APP_ICON_PNG = os.path.join(_ICON_DIR, "app_icon.png")
_USE_PNG_ICON = os.path.isfile(ICON_PNG)

# Add space after emoji to force macOS stable width, prevent clipping
ICON_NORMAL = "💬 "
ICON_LOADING = "⏳ "
ICON_DONE = "✅ "
ICON_ERROR = "❌ "

# PNG icon state mapping
_ICON_PNG_MAP = {
    ICON_NORMAL: ICON_PNG,
    ICON_LOADING: ICON_LOADING_PNG,
    ICON_DONE: ICON_DONE_PNG,
    ICON_ERROR: ICON_ERROR_PNG,
}


def _notification_text(value, limit=700):
    """Normalize notification text and keep the body small enough for macOS."""
    text = "" if value is None else str(value)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _notify_with_osascript(title, subtitle, message):
    script = """
on run argv
    set notificationTitle to item 1 of argv
    set notificationSubtitle to item 2 of argv
    set notificationMessage to item 3 of argv
    display notification notificationMessage with title notificationTitle subtitle notificationSubtitle sound name "default"
end run
"""
    result = subprocess.run(
        ["osascript", "-e", script, title, subtitle, message],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript notification failed")


def _notify(title, subtitle, message):
    """Send notification safely, falling back when a backend is unavailable."""
    title = _notification_text(title, 120) or "微信总结"
    subtitle = _notification_text(subtitle, 180)
    message = _notification_text(message, 700)

    try:
        rumps.notification(title, subtitle, message)
        return
    except Exception as e:
        print(f"[notify] rumps failed: {e}")

    if sys.platform == "darwin":
        try:
            _notify_with_osascript(title, subtitle, message)
            return
        except Exception as e:
            print(f"[notify] osascript failed: {e}")

    print(f"[{title}] {subtitle}: {message}")


def _wechat_signing_message():
    return "请重新双击启动.command，完成微信授权"


class WeChatSummaryApp(rumps.App):
    def __init__(self):
        if _USE_PNG_ICON:
            super().__init__("微信总结", icon=ICON_PNG, template=True, quit_button="退出")
            self.title = None  # Show icon only, no text
        else:
            super().__init__("微信总结", title=ICON_NORMAL, quit_button="退出")
        # Set app icon (replace Python rocket icon in dialogs and Dock)
        if _HAS_APPKIT and os.path.isfile(APP_ICON_PNG):
            try:
                ns_icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if ns_icon:
                    NSApplication.sharedApplication().setApplicationIconImage_(ns_icon)
            except Exception:
                pass
        self.config = load_config()
        self.db = None
        self.ai = None
        self._summarizing = False
        self._last_summary = None
        self._current_status = ICON_NORMAL
        self._monitor_timer = None
        self._monitor_lock = threading.Lock()
        self._monitor_last_error = ""

        # Build menu
        self.menu = [
            rumps.MenuItem("刷新群聊列表", callback=self.refresh_groups),
            rumps.MenuItem("🔍 关键词搜索", callback=self._on_search_click),
            rumps.separator,
            # Dynamic area: ungrouped chats (📎) inserted via insert_after
            rumps.separator,
            # Dynamic area: groups (📂) inserted via insert_before "📋 ..."
            # Dynamic area: latest summary (📝) inserted before "📋 ..."
            rumps.MenuItem("📋 最近总结"),
            rumps.separator,
            self._build_mcp_menu(),
            self._build_monitor_menu(),
            self._build_settings_menu(),
            rumps.MenuItem("🔄 刷新数据源", callback=self.reextract_keys),
        ]

        self._rebuild_summary_history()

        # Main thread queue: background threads safely update UI via this queue
        self._main_queue = queue.Queue()
        self._queue_timer = rumps.Timer(self._process_main_queue, 0.3)
        self._queue_timer.start()

        # Auto-refresh on menu open (NSMenuDelegate)
        self._menu_delegate = None
        if _HAS_APPKIT:
            self._setup_delegate_timer = rumps.Timer(self._setup_menu_delegate, 1)
            self._setup_delegate_timer.start()

        self._configure_monitor_timer()

        # Background initialization
        threading.Thread(target=self._init_background, daemon=True).start()

    # ── Safely set menu bar title ───────────────────────────────

    def _set_status(self, new_title):
        """Safely set menu bar status icon."""
        try:
            if _USE_PNG_ICON:
                # Switch PNG icon, hide text
                png_path = _ICON_PNG_MAP.get(new_title, ICON_PNG)
                self.icon = png_path
                self.title = None
            else:
                self.title = " "       # Set placeholder space first
                time.sleep(0.05)       # Give macOS time to release old width
                self.title = new_title # Then set new emoji
            self._current_status = new_title
        except Exception:
            self.title = new_title

    # ── Settings menu ────────────────────────────────────────

    def _build_settings_menu(self):
        """Build settings submenu."""
        settings = rumps.MenuItem("⚙️ 设置")

        # AI service selection
        ai_menu = rumps.MenuItem("🤖 AI 服务")
        current = self.config.get("ai_provider", "qwen")
        for key, label in AI_PROVIDERS:
            prefix = "✅ " if key == current else "    "
            item = rumps.MenuItem(
                f"{prefix}{label}",
                callback=self._make_provider_callback(key),
            )
            ai_menu.add(item)
        settings.add(ai_menu)

        # API Key settings
        has_key = bool(load_key("ai-api-key"))
        key_status = "已设置 ✅" if has_key else "未设置 ❌"
        settings.add(rumps.MenuItem(
            f"🔑 API Key ({key_status})",
            callback=self._set_api_key,
        ))

        # Reset
        settings.add(rumps.separator)
        settings.add(rumps.MenuItem(
            "🗑️ 重置所有书签",
            callback=self._reset_bookmarks,
        ))

        # Current status
        settings.add(rumps.separator)
        settings.add(rumps.MenuItem(
            "📂 打开配置文件",
            callback=self.open_config_file,
        ))
        settings.add(rumps.MenuItem(
            "📁 打开总结目录",
            callback=self._open_summary_dir,
        ))

        # Auto-refresh toggle
        settings.add(rumps.separator)
        auto_refresh = self.config.get("auto_refresh_on_open", False)
        refresh_prefix = "✅ " if auto_refresh else "      "
        settings.add(rumps.MenuItem(
            f"{refresh_prefix}打开菜单时自动刷新",
            callback=self._toggle_auto_refresh,
        ))

        # Show group nickname toggle
        show_nickname = self.config.get("show_group_nickname", True)
        nick_prefix = "✅ " if show_nickname else "      "
        settings.add(rumps.MenuItem(
            f"{nick_prefix}总结中显示群昵称",
            callback=self._toggle_group_nickname,
        ))

        # Batch summary message limit per group
        batch_limit = self.config.get("batch_msg_limit", 100)
        batch_menu = rumps.MenuItem("📊 小组总结每群条数")
        for val in [50, 100, 200, 500]:
            prefix = "✅ " if batch_limit == val else "      "
            batch_menu.add(rumps.MenuItem(
                f"{prefix}{val} 条",
                callback=self._make_batch_limit_callback(val),
            ))
        settings.add(batch_menu)

        # Hide inactive chats
        hide_months = self.config.get("hide_inactive_months", 1)
        hide_menu = rumps.MenuItem("🕐 隐藏不活跃群聊")
        options = [("关闭", 0), ("1 个月", 1), ("3 个月", 3), ("6 个月", 6)]
        for label, val in options:
            prefix = "✅ " if hide_months == val else "      "
            hide_menu.add(rumps.MenuItem(
                f"{prefix}{label}",
                callback=self._make_hide_inactive_callback(val),
            ))
        settings.add(hide_menu)

        return settings

    def _rebuild_settings_menu(self):
        """Rebuild settings menu (after config change)."""
        if "⚙️ 设置" in self.menu:
            del self.menu["⚙️ 设置"]
        self.menu.insert_before("🔄 刷新数据源", self._build_settings_menu())

    def _rebuild_monitor_menu(self):
        """Rebuild top-level monitor menu."""
        if "🔔 关注推送" in self.menu:
            del self.menu["🔔 关注推送"]
        self.menu.insert_after("🔌 MCP 服务", self._build_monitor_menu())

    # ── Topic monitor menu ────────────────────────────────────

    def _build_monitor_menu(self):
        """Build macOS notification monitor submenu."""
        monitor = rumps.MenuItem("🔔 关注推送")

        enabled = self.config.get("monitor_enabled", False)
        interval = self.config.get("monitor_interval_minutes", 3)
        chat_name = self.config.get("monitor_chat_display_name", "Claude恋爱技术群")
        topic = self.config.get("monitor_topic", "").strip()
        topic_label = topic[:24] + "..." if len(topic) > 24 else topic

        status = "已开启" if enabled else "已暂停"
        monitor.add(rumps.MenuItem(f"状态: {status} · 每 {interval} 分钟"))
        monitor.add(rumps.MenuItem(f"群聊: {chat_name}"))
        monitor.add(rumps.MenuItem(f"关注: {topic_label or '未设置'}"))
        monitor.add(rumps.separator)

        toggle_label = "⏸ 暂停监控" if enabled else "▶️ 启用监控"
        monitor.add(rumps.MenuItem(toggle_label, callback=self._toggle_monitor))
        monitor.add(rumps.MenuItem("📝 设置关注描述...", callback=self._set_monitor_topic))
        monitor.add(rumps.MenuItem("⏱ 设置检查间隔...", callback=self._set_monitor_interval))
        monitor.add(rumps.separator)
        monitor.add(rumps.MenuItem("🧪 测试检查一次", callback=self._test_monitor_once))
        monitor.add(rumps.MenuItem("📁 打开命中记录目录", callback=self._open_monitor_hits_dir))
        monitor.add(rumps.MenuItem("📂 设置 Obsidian 仓库位置...", callback=self._set_monitor_obsidian_root))
        monitor.add(rumps.MenuItem("🗂 打开知识库目录", callback=self._open_monitor_knowledge_dir))
        monitor.add(rumps.MenuItem("🧹 整理去重 + 重导出...", callback=self._run_monitor_maintenance))
        return monitor

    def _configure_monitor_timer(self):
        """Start/stop the monitor timer based on config."""
        if self._monitor_timer:
            try:
                self._monitor_timer.stop()
            except Exception:
                pass
            self._monitor_timer = None

        if not self.config.get("monitor_enabled", False):
            return
        if not self.config.get("monitor_topic", "").strip():
            return

        try:
            initialize_state_if_needed()
        except Exception:
            traceback.print_exc()

        interval_seconds = max(1, self.config.get("monitor_interval_minutes", 3)) * 60
        self._monitor_timer = rumps.Timer(self._on_monitor_timer, interval_seconds)
        self._monitor_timer.start()
        print(f"[monitor] 已启动，每 {interval_seconds // 60} 分钟检查一次")

    def _on_monitor_timer(self, _):
        """Timer callback: dispatch monitor work to a background thread."""
        if not self.config.get("monitor_enabled", False) or not self.db:
            return
        threading.Thread(
            target=self._run_monitor_check,
            kwargs={"manual": False, "dry_run": False},
            daemon=True,
        ).start()

    def _toggle_monitor(self, _):
        current = self.config.get("monitor_enabled", False)
        if current:
            self.config["monitor_enabled"] = False
            save_config(self.config)
            self._configure_monitor_timer()
            _notify("关注推送", "已暂停", "后台关注推送已暂停")
            self._rebuild_monitor_menu()
            return

        if not self.config.get("monitor_topic", "").strip():
            _notify("关注推送", "先设置关注描述", "写下你想盯什么内容后再开启")
            self._delayed_run(self._show_monitor_topic_dialog, True)
            return

        self.config["monitor_enabled"] = True
        save_config(self.config)
        initialize_state_if_needed()
        self._configure_monitor_timer()
        _notify("关注推送", "已开启", "从当前时间开始，只检查新增消息")
        self._rebuild_monitor_menu()

    def _set_monitor_topic(self, _):
        self._delayed_run(self._show_monitor_topic_dialog, False)

    def _show_monitor_topic_dialog(self, enable_after=False):
        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "设置关注描述",
                "描述你想被提醒的内容。\n例如：工作项目的新进展、AI 工具或模型的重要更新、生活安排中的时间提醒。",
                default_text=self.config.get("monitor_topic", ""),
                ok="保存",
                width=460,
            )
            if not clicked:
                return
            topic = text.strip()
            self.config["monitor_topic"] = topic
            if enable_after and topic:
                self.config["monitor_enabled"] = True
                initialize_state_if_needed()
            save_config(self.config)
            self._configure_monitor_timer()
            if topic:
                _notify("关注推送", "关注描述已保存", "已按新描述监控新增消息")
            else:
                _notify("关注推送", "关注描述已清空", "设置描述前不会调用 AI 检查")
            self._rebuild_monitor_menu()
        finally:
            self._release_front()

    def _set_monitor_interval(self, _):
        self._delayed_run(self._show_monitor_interval_dialog)

    def _show_monitor_interval_dialog(self):
        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "设置检查间隔",
                "输入分钟数（1-1440）。建议这个高流量群先用 3 分钟。",
                default_text=str(self.config.get("monitor_interval_minutes", 3)),
                ok="保存",
                width=260,
            )
            if not clicked:
                return
            try:
                minutes = int(text.strip())
            except ValueError:
                _notify("关注推送", "输入错误", "请输入正整数分钟数")
                return
            if minutes < 1 or minutes > 1440:
                _notify("关注推送", "输入错误", "请输入 1-1440 之间的分钟数")
                return
            self.config["monitor_interval_minutes"] = minutes
            save_config(self.config)
            self._configure_monitor_timer()
            _notify("关注推送", "检查间隔已更新", f"每 {minutes} 分钟检查一次")
            self._rebuild_monitor_menu()
        finally:
            self._release_front()

    def _test_monitor_once(self, _):
        if not self.db:
            _notify("关注推送", "还没初始化", "请等微信数据加载完成后再测试")
            return
        if not self.config.get("monitor_topic", "").strip():
            _notify("关注推送", "先设置关注描述", "测试前需要知道你想盯什么")
            self._delayed_run(self._show_monitor_topic_dialog, False)
            return
        threading.Thread(
            target=self._run_monitor_check,
            kwargs={"manual": True, "dry_run": True},
            daemon=True,
        ).start()

    def _run_monitor_check(self, manual=False, dry_run=False):
        if not self._monitor_lock.acquire(blocking=False):
            if manual:
                _notify("关注推送", "正在检查", "上一轮检查还没结束")
            return

        try:
            monitor = TopicMonitor(self.db, self.config)
            result = monitor.check_once(dry_run=dry_run)
            self._handle_monitor_result(result, manual=manual, dry_run=dry_run)
            self._monitor_last_error = ""
        except MonitorConfigError as e:
            self._handle_monitor_error(str(e), manual)
        except Exception as e:
            traceback.print_exc()
            self._handle_monitor_error(f"检查失败: {e}", manual)
        finally:
            self._monitor_lock.release()

    def _handle_monitor_result(self, result, manual=False, dry_run=False):
        status = result.get("status")
        decision = result.get("decision") or {}

        if status == "notified":
            _notify("关注推送", result.get("title", "发现关注内容"),
                    result.get("summary", "有值得关注的新消息"))
            hit_path = result.get("hit_path", "")
            knowledge_path = result.get("knowledge_path", "")
            print(f"[monitor] 命中: {hit_path} 知识库: {knowledge_path}")
            return

        if status == "matched":
            relation = result.get("relation")
            prefix = f"测试命中/{relation}" if relation else "测试命中（不写记录）"
            _notify("关注推送", f"{prefix}: {result.get('title', '发现关注内容')}",
                    result.get("summary", "有值得关注的新消息"))
            return

        if not manual:
            print(f"[monitor] {status}: {result.get('message', '')}")
            return

        messages = {
            "initialized": ("已开始监控", "正式检查会从当前时间之后的新消息开始"),
            "missing_topic": ("还没设置关注描述", "设置后才会调用 AI 检查"),
            "no_messages": ("没有新消息", "这次测试窗口里没有新增内容"),
            "no_match": ("未命中", f"检查了 {result.get('message_count', 0)} 条，没有值得提醒的内容"),
            "cooldown": ("命中但在冷却中", "同一主题短时间内不会重复提醒"),
            "duplicate": ("重复内容，已静默记录", "知识库判断没有新线索，这次不推送"),
        }
        title, message = messages.get(status, ("检查完成", str(result)))
        _notify("关注推送", title, message)

    def _handle_monitor_error(self, message, manual=False):
        print(f"[monitor] {message}")
        if manual or message != self._monitor_last_error:
            _notify("关注推送", "检查失败", message[:180])
        self._monitor_last_error = message

    def _open_monitor_hits_dir(self, _):
        os.makedirs(HITS_DIR, exist_ok=True)
        subprocess.run(["open", HITS_DIR])

    def _open_monitor_knowledge_dir(self, _):
        root = self.config.get("monitor_obsidian_root") or OBSIDIAN_ROOT
        os.makedirs(root, exist_ok=True)
        subprocess.run(["open", root])

    def _set_monitor_obsidian_root(self, _):
        self._delayed_run(self._show_monitor_obsidian_root_dialog)

    def _show_monitor_obsidian_root_dialog(self):
        self._bring_to_front()
        try:
            current = self.config.get("monitor_obsidian_root") or OBSIDIAN_ROOT
            clicked, text = self._input_dialog(
                "设置 Obsidian 仓库位置",
                "粘贴你的 Obsidian 仓库（vault）路径。\n"
                "命中笔记会写到该目录下的「关注推送」子文件夹，\n"
                "在 Obsidian 里就能用 Bases、关系图谱自动整理。\n\n"
                "留空可恢复默认位置。",
                default_text=current,
                ok="保存",
                width=480,
            )
            if not clicked:
                return
            path = os.path.expanduser(text.strip())
            if not path:
                self.config["monitor_obsidian_root"] = OBSIDIAN_ROOT
                save_config(self.config)
                _notify("关注推送", "已恢复默认知识库位置", OBSIDIAN_ROOT)
                self._rebuild_monitor_menu()
                return
            parent = os.path.dirname(path.rstrip("/")) or "/"
            if not os.path.isdir(path) and not os.path.isdir(parent):
                _notify("关注推送", "路径无效", "目录及其上层都不存在，请检查后重试")
                return
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as e:
                _notify("关注推送", "无法创建目录", str(e)[:180])
                return
            self.config["monitor_obsidian_root"] = path
            save_config(self.config)
            _notify("关注推送", "知识库位置已更新", path)
            self._rebuild_monitor_menu()
        finally:
            self._release_front()

    def _knowledge_ready(self):
        if not self.config.get("monitor_knowledge_enabled", False):
            return False
        db = self.config.get("monitor_knowledge_db") or KNOWLEDGE_DB
        return os.path.exists(os.path.expanduser(db))

    def _run_monitor_maintenance(self, _):
        if not self._knowledge_ready():
            _notify("关注推送", "暂无知识库", "命中并记录一些内容后再来整理")
            return
        threading.Thread(target=self._maintenance_scan, daemon=True).start()

    def _maintenance_scan(self):
        if not self._monitor_lock.acquire(blocking=False):
            _notify("关注推送", "正在等待", "当前监控检查还没结束，整理会稍后继续")
            if not self._monitor_lock.acquire(timeout=90):
                _notify("关注推送", "仍在忙", "当前检查耗时较久，稍后再点一次整理")
                return
        try:
            store = KnowledgeStore.from_config(self.config)
            plan = store.run_maintenance(dry_run=True)
        except Exception as e:
            traceback.print_exc()
            self._monitor_lock.release()
            _notify("关注推送", "整理失败", str(e)[:180])
            return
        self._monitor_lock.release()
        self._run_on_main(self._maintenance_confirm, plan)

    def _maintenance_confirm(self, plan):
        confirmed = False
        try:
            groups = plan.get("duplicate_groups", [])
            category_changes = plan.get("category_changes", [])
            total = plan.get("total_topics", 0)
            removed = plan.get("removed_count", 0)
            lines = []
            for g in groups[:8]:
                merged = "、".join(g.get("merged", []))
                lines.append(f"· {merged} → {g.get('primary', '')}")
            if len(groups) > 8:
                lines.append(f"…… 另有 {len(groups) - 8} 组")

            category_lines = []
            for change in category_changes[:10]:
                category_lines.append(
                    f"· {change.get('from', '')} → {change.get('to', '')}：{change.get('title', '')}"
                )
            if len(category_changes) > 10:
                category_lines.append(f"…… 另有 {len(category_changes) - 10} 篇")

            if groups:
                head = (
                    f"发现 {plan['group_count']} 组疑似重复，"
                    f"{plan['merge_note_count']} 篇将合并成 {plan['group_count']} 篇。\n"
                    f"另有 {len(category_changes)} 篇会归并到稳定分类文件夹。\n"
                    f"随后把全部 {total - removed} 篇重新导出到当前 Obsidian 仓库"
                    f"（回填双链 / event_count）。\n\n"
                    + "\n".join(lines)
                    + ("\n\n分类归并：\n" + "\n".join(category_lines) if category_lines else "")
                    + "\n\n这会删除被合并的笔记，并移动归并分类后的笔记，确定整理吗？"
                )
                ok_label = "开始整理"
            elif category_changes:
                head = (
                    "没有发现重复主题。\n"
                    f"发现 {len(category_changes)} 篇笔记可以归并到更少的分类文件夹，"
                    f"随后把全部 {total} 篇重新导出到当前 Obsidian 仓库。\n\n"
                    + "\n".join(category_lines)
                    + "\n\n这会移动这些笔记到归并后的文件夹，确定整理吗？"
                )
                ok_label = "归并并重导出"
            else:
                head = (
                    "没有发现重复主题。\n"
                    f"要把全部 {total} 篇笔记重新导出到当前 Obsidian 仓库吗？\n"
                    "（回填双链 / event_count，也可用于迁移到新仓库）"
                )
                ok_label = "重新导出"

            self._bring_to_front()
            try:
                confirmed = self._confirm_dialog("整理知识库", head, ok=ok_label)
            finally:
                self._release_front()
        except Exception:
            traceback.print_exc()
            confirmed = False

        if not confirmed:
            _notify("关注推送", "已取消整理", "没有任何改动")
            return
        threading.Thread(target=self._maintenance_execute, daemon=True).start()

    def _maintenance_execute(self):
        if not self._monitor_lock.acquire(blocking=False):
            _notify("关注推送", "正在等待", "当前监控检查还没结束，整理会稍后继续")
            if not self._monitor_lock.acquire(timeout=90):
                _notify("关注推送", "仍在忙", "当前检查耗时较久，稍后再点一次整理")
                return
        try:
            store = KnowledgeStore.from_config(self.config)
            result = store.run_maintenance(dry_run=False)
            _notify(
                "关注推送", "整理完成",
                f"合并 {result['removed_count']} 篇重复，"
                f"归并 {result.get('category_change_count', 0)} 篇分类，"
                f"重新导出 {result['reexport_count']} 篇",
            )
        except Exception as e:
            traceback.print_exc()
            _notify("关注推送", "整理失败", str(e)[:180])
        finally:
            self._monitor_lock.release()

    # ── MCP service menu ──────────────────────────────────────

    def _check_mcp_ready(self):
        """Check if MCP Server can start normally, return issue list (empty = ready)."""
        project_dir = os.path.dirname(os.path.abspath(__file__))
        venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
        mcp_server = os.path.join(project_dir, "mcp_server.py")

        issues = []
        if not os.path.isfile(venv_python):
            issues.append("Python 虚拟环境未安装")
        if not os.path.isfile(mcp_server):
            issues.append("mcp_server.py 不存在")
        db_dir = self.config.get("db_dir", "")
        if not db_dir or not os.path.isdir(db_dir):
            issues.append("数据库目录未配置")
        if not get_cached_keys():
            issues.append("数据库密钥未提取")
        return issues

    def _is_mcp_running(self):
        """Detect if mcp_server.py process is running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "mcp_server.py"],
                capture_output=True, text=True,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_mcp_config_snippet(self, client="claude_desktop"):
        """Generate MCP client configuration."""
        import json as _json
        project_dir = os.path.dirname(os.path.abspath(__file__))
        venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
        mcp_server = os.path.join(project_dir, "mcp_server.py")

        if client == "claude_desktop":
            return _json.dumps({
                "mcpServers": {
                    "wechat-summary": {
                        "command": venv_python,
                        "args": [mcp_server],
                    }
                }
            }, indent=2, ensure_ascii=False)
        else:
            return f"claude mcp add wechat-summary {venv_python} {mcp_server}"

    def _build_mcp_menu(self):
        """Build MCP service submenu."""
        mcp = rumps.MenuItem("🔌 MCP 服务")

        # Status
        issues = self._check_mcp_ready()
        if issues:
            status_text = f"❌ {issues[0]}"
        elif self._is_mcp_running():
            status_text = "✅ 运行中"
        else:
            status_text = "✅ 就绪"
        mcp.add(rumps.MenuItem(f"状态: {status_text}"))

        mcp.add(rumps.separator)

        mcp.add(rumps.MenuItem(
            "📋 复制 Claude Desktop 配置",
            callback=self._copy_claude_desktop_config,
        ))
        mcp.add(rumps.MenuItem(
            "📋 复制 Claude Code 命令",
            callback=self._copy_claude_code_config,
        ))

        mcp.add(rumps.separator)

        mcp.add(rumps.MenuItem(
            "🧪 测试 MCP 服务",
            callback=self._test_mcp_server,
        ))

        return mcp

    def _rebuild_mcp_menu(self):
        """Rebuild MCP service menu."""
        if "🔌 MCP 服务" in self.menu:
            del self.menu["🔌 MCP 服务"]
        self.menu.insert_before("⚙️ 设置", self._build_mcp_menu())

    def _copy_claude_desktop_config(self, _):
        snippet = self._get_mcp_config_snippet("claude_desktop")
        process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        process.communicate(snippet.encode("utf-8"))
        _notify("MCP 服务", "已复制到剪贴板",
                "粘贴到 claude_desktop_config.json 即可")

    def _copy_claude_code_config(self, _):
        snippet = self._get_mcp_config_snippet("claude_code")
        process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        process.communicate(snippet.encode("utf-8"))
        _notify("MCP 服务", "已复制到剪贴板",
                "在终端粘贴执行即可添加 MCP 服务")

    def _test_mcp_server(self, _):
        """Test if MCP service can start normally."""
        threading.Thread(target=self._do_mcp_test, daemon=True).start()

    def _do_mcp_test(self):
        project_dir = os.path.dirname(os.path.abspath(__file__))
        venv_python = os.path.join(project_dir, ".venv", "bin", "python3")
        mcp_server = os.path.join(project_dir, "mcp_server.py")

        if not os.path.isfile(venv_python):
            _notify("MCP 服务", "测试失败", "Python 虚拟环境未安装")
            return

        try:
            proc = subprocess.Popen(
                [venv_python, mcp_server],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(2)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                _notify("MCP 服务", "测试通过 ✅", "MCP 服务器启动正常")
            else:
                stderr = proc.stderr.read().decode(errors="replace")
                _notify("MCP 服务", "启动失败 ❌", stderr[:200] or "未知错误")
        except Exception as e:
            _notify("MCP 服务", "测试失败 ❌", str(e)[:200])

    def _toggle_auto_refresh(self, _):
        """Toggle 'auto-refresh on menu open' setting."""
        current = self.config.get("auto_refresh_on_open", False)
        self.config["auto_refresh_on_open"] = not current
        save_config(self.config)
        state = "开启" if not current else "关闭"
        _notify("微信总结", "设置已更新", f"自动刷新已{state}")
        self._rebuild_settings_menu()

    def _toggle_group_nickname(self, _):
        """Toggle 'show group nickname in summary' setting."""
        current = self.config.get("show_group_nickname", True)
        self.config["show_group_nickname"] = not current
        save_config(self.config)
        state = "开启" if not current else "关闭"
        _notify("微信总结", "设置已更新", f"总结中显示群昵称已{state}")
        self._rebuild_settings_menu()

    def _make_batch_limit_callback(self, val):
        def callback(_):
            self.config["batch_msg_limit"] = val
            save_config(self.config)
            _notify("微信总结", "设置已更新", f"小组总结每群条数: {val}")
            self._rebuild_settings_menu()
        return callback

    def _make_hide_inactive_callback(self, months):
        def callback(_):
            self.config["hide_inactive_months"] = months
            save_config(self.config)
            label = f"{months} 个月" if months > 0 else "关闭"
            _notify("微信总结", "设置已更新", f"隐藏不活跃群聊: {label}")
            self._rebuild_settings_menu()
            self._rebuild_chat_menu()
        return callback

    def _make_provider_callback(self, provider_key):
        def callback(sender):
            self.config["ai_provider"] = provider_key
            save_config(self.config)
            self.ai = None  # Recreate on next summary

            provider_name = dict(AI_PROVIDERS).get(provider_key, provider_key)
            _notify("微信总结", "AI 服务已切换", f"当前使用: {provider_name}")
            print(f"[config] AI 切换为: {provider_key}")

            # If provider needs key and none is set, prompt to configure
            if provider_key != "ollama" and not load_key("ai-api-key"):
                self._set_api_key(None)
            else:
                self._rebuild_settings_menu()
        return callback

    def _bring_to_front(self):
        """Bring app to front, temporarily set as Regular app to capture keyboard input."""
        if _HAS_APPKIT:
            try:
                app = NSApplication.sharedApplication()
                # Ensure dialogs and Dock show correct app icon (not Python rocket)
                if os.path.isfile(APP_ICON_PNG):
                    ns_icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                    if ns_icon:
                        app.setApplicationIconImage_(ns_icon)
                app.setActivationPolicy_(0)   # Regular -> get keyboard focus
                app.activateIgnoringOtherApps_(True)
            except Exception:
                pass

    def _input_dialog(self, title, message, default_text="",
                      ok="确定", cancel="取消", width=300):
        """Show input dialog with correct app icon (replaces rumps.Window).

        Returns:
            (clicked: bool, text: str)
        """
        if _HAS_APPKIT:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.addButtonWithTitle_(ok)
            alert.addButtonWithTitle_(cancel)
            field = NSTextField.alloc().initWithFrame_(((0, 0), (width, 24)))
            field.setStringValue_(default_text)
            alert.setAccessoryView_(field)
            alert.window().setInitialFirstResponder_(field)
            result = alert.runModal()
            clicked = (result == 1000)
            text = str(field.stringValue()) if clicked else ""
            return clicked, text
        else:
            window = rumps.Window(
                message=message, title=title, default_text=default_text,
                ok=ok, cancel=cancel, dimensions=(width, 24),
            )
            resp = window.run()
            return bool(resp.clicked), (resp.text if resp.clicked else "")

    def _confirm_dialog(self, title, message, ok="确定", cancel="取消"):
        """Show confirmation dialog with correct app icon (no input field).

        Returns:
            bool: whether OK was clicked
        """
        if _HAS_APPKIT:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.addButtonWithTitle_(ok)
            alert.addButtonWithTitle_(cancel)
            return alert.runModal() == 1000
        else:
            window = rumps.Window(
                message=message, title=title, default_text="",
                ok=ok, cancel=cancel, dimensions=(0, 0),
            )
            return bool(window.run().clicked)

    def _release_front(self):
        """Restore as menu bar app (hide Dock icon)."""
        if _HAS_APPKIT:
            try:
                NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory
            except Exception:
                pass

    def _delayed_run(self, func, *args):
        """Delay execution on main thread, let macOS close menu before showing dialog (NSWindow must be created on main thread)."""
        def _fire(timer):
            timer.stop()
            func(*args)
        t = rumps.Timer(_fire, 0.3)
        t.start()

    def _run_on_main(self, func, *args):
        """Execute on main thread (required for menu modifications from background threads)."""
        self._main_queue.put((func, args))

    def _process_main_queue(self, _):
        """Main thread timer callback: process UI updates submitted by background threads."""
        while not self._main_queue.empty():
            try:
                func, args = self._main_queue.get_nowait()
                func(*args)
            except queue.Empty:
                break
            except Exception:
                traceback.print_exc()

    def _setup_menu_delegate(self, timer):
        """Install menu open detection delegate (runs once)."""
        timer.stop()
        try:
            delegate = _MenuOpenDelegate.alloc().init()
            delegate.app_ref = self
            # Install delegate via rumps Menu wrapper's underlying NSMenu
            ns_menu = self.menu._menu
            if ns_menu:
                ns_menu.setDelegate_(delegate)
                self._menu_delegate = delegate  # prevent GC
                print("[init] ✓ 菜单打开自动刷新已安装")
        except Exception as e:
            print(f"[init] 菜单回调安装失败（不影响使用）: {e}")

    def _do_silent_refresh(self):
        """Silently refresh chat list (auto-triggered on menu open, no notifications)."""
        try:
            if self.db:
                self._run_on_main(self._rebuild_chat_menu)
                self._run_on_main(self._rebuild_mcp_menu)
                print("[auto-refresh] ✓ 群聊列表已刷新")
        except Exception:
            traceback.print_exc()

    def _set_api_key(self, _):
        """Show API Key dialog (delayed execution, let menu close first)."""
        self._delayed_run(self._show_api_key_dialog)

    def _show_api_key_dialog(self):
        provider = self.config.get("ai_provider", "qwen")
        provider_name = dict(AI_PROVIDERS).get(provider, provider)

        hints = {
            "qwen": "通义千问 Key 获取：dashscope.console.aliyun.com",
            "deepseek": "DeepSeek Key 获取：platform.deepseek.com",
            "claude": "Claude Key 获取：console.anthropic.com",
            "openai": "OpenAI Key 获取：platform.openai.com",
        }
        hint = hints.get(provider, "请输入 API Key")

        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "设置 API Key",
                f"当前 AI 服务：{provider_name}\n{hint}\n\nKey 将安全存储在 macOS 钥匙串中",
                ok="保存", width=380,
            )

            if clicked and text.strip():
                key = text.strip()
                if save_key("ai-api-key", key):
                    self.ai = None
                    _notify("微信总结", "API Key 已保存", "密钥已安全存储在 macOS 钥匙串中")
                    self._rebuild_settings_menu()
                else:
                    _notify("微信总结", "保存失败", "无法写入钥匙串")
        finally:
            self._release_front()

    def _reset_bookmarks(self, _):
        """Clear all bookmarks (delayed execution)."""
        self._delayed_run(self._show_reset_bookmarks_dialog)

    def _show_reset_bookmarks_dialog(self):
        self._bring_to_front()
        try:
            confirmed = self._confirm_dialog(
                "重置所有书签",
                "清除后，所有群聊将变为「未总结」状态，\n下次点击总结时会重新读取最近消息。\n\n确定要重置吗？",
                ok="确定重置",
            )
            if confirmed:
                clear_all_bookmarks()
                if self.db:
                    self.db.invalidate_cache()
                    self._rebuild_chat_menu()
                _notify("微信总结", "已重置", "所有书签已清除，所有群聊已恢复为未总结")
                print("[config] 所有书签已清除，缓存已刷新")
        finally:
            self._release_front()

    def open_config_file(self, _):
        if not os.path.exists(CONFIG_FILE):
            save_config(self.config)
        subprocess.run(["open", CONFIG_FILE])

    def _open_summary_dir(self, _):
        subprocess.run(["open", SUMMARY_DIR])

    # ── Initialization ──────────────────────────────────────────

    def _init_background(self):
        print("[init] 开始后台初始化...")
        self._set_status(ICON_LOADING)

        keys = get_cached_keys()
        print(f"[init] 缓存密钥: {'有' if keys else '无'}")
        signed = is_wechat_signed()
        print(f"[init] 微信签名: {'正常' if signed else '需要重新授权'}")

        if not signed and keys:
            _notify("微信总结", "检测到微信签名已失效",
                    f"当前缓存密钥仍可使用；如读不到新消息，{_wechat_signing_message()}")

        if not keys:
            if not is_wechat_running():
                _notify("微信总结", "初始化失败", "请先启动微信并登录")
                self._set_status(ICON_ERROR)
                return
            if not signed:
                _notify("微信总结", "微信需要重新授权", _wechat_signing_message())
                self._set_status(ICON_ERROR)
                return
            if not compile_scanner():
                _notify("微信总结", "编译失败", "需安装 Xcode CLI Tools")
                self._set_status(ICON_ERROR)
                return
            _notify("微信总结", "首次运行", "正在同步数据源...")
            keys = extract_keys()
            if not keys:
                _notify("微信总结", "数据源同步失败", "请确认微信已登录且已重签名")
                self._set_status(ICON_ERROR)
                return

        print(f"[init] db_dir: {self.config.get('db_dir')}")
        if not self.config.get("db_dir") or not os.path.isdir(self.config["db_dir"]):
            _notify("微信总结", "未找到微信数据目录", "请检查配置")
            self._set_status(ICON_ERROR)
            return

        print("[init] 正在加载数据库...")
        self.db = WeChatDB(self.config["db_dir"], keys)

        print("[init] 正在刷新群聊列表...")
        self._run_on_main(self._rebuild_chat_menu)

        # Check if any new encrypted databases are missing keys
        try:
            missing = check_new_databases(self.config["db_dir"], keys)
            if missing:
                names = ", ".join(os.path.basename(m) for m in missing)
                print(f"[init] ⚠ 发现 {len(missing)} 个数据库缺少密钥: {names}")
                _notify("微信总结", f"发现 {len(missing)} 个新数据库",
                        f"建议点击「🔄 刷新数据源」更新\n{names}")
            else:
                print("[init] ✓ 所有数据库密钥完整")
        except Exception as e:
            print(f"[init] 数据库检测出错: {e}")

        self._set_status(ICON_NORMAL)
        print("[init] ✓ 初始化完成！")
        _notify("微信总结", "就绪", "点击菜单栏选择群聊进行总结")

    # ── Chat list + groups (unified dynamic menu management) ────────────────

    def _build_chat_title(self, session):
        """Build menu title for a single group chat."""
        name = session["name"]
        username = session["username"]
        unread = session["unread"]

        last_summary = get_summary_time(username)
        bookmark_ts = get_bookmark(username)

        title = f"📎 {name}"
        has_summarized = bool(last_summary) or bookmark_ts > 0

        if has_summarized:
            display_time = last_summary or datetime.fromtimestamp(bookmark_ts).strftime("%Y-%m-%d %H:%M")
            title += f"  ⏱{display_time}"
            new_count = self.db.count_messages_since(username, bookmark_ts)
            if new_count > 0:
                title += f" · 有{new_count}条更新"
            print(f"[refresh]   {name}: 已总结 ({display_time}), 更新={new_count}")
        else:
            if unread > 0:
                title += f" (未总结 · {unread}条未读)"
            else:
                title += " (未总结)"
            print(f"[refresh]   {name}: 未总结, 微信未读={unread}")

        return title

    def _rebuild_chat_menu(self):
        """Rebuild dynamic menu: ungrouped chats + group submenus."""
        # Clear old dynamic items (📎 ungrouped chats + 📂 groups)
        keys_to_remove = [k for k in self.menu.keys()
                          if isinstance(k, str) and (k.startswith("📎") or k.startswith("📂"))]
        for key in keys_to_remove:
            del self.menu[key]

        if not self.db:
            return

        sessions = self.db.get_recent_sessions(limit=200)
        group_sessions = [s for s in sessions if s["is_group"]]

        # ── Filter inactive chats ──
        hide_months = self.config.get("hide_inactive_months", 1)
        if hide_months > 0:
            import time as _time
            cutoff_ts = _time.time() - hide_months * 30 * 86400
            group_sessions = [s for s in group_sessions if s["timestamp"] >= cutoff_ts]

        # Find chats that are already in groups
        groups = load_groups()
        grouped_usernames = set()
        for grp in groups:
            grouped_usernames.update(grp["chats"])

        # ── Ungrouped chats (reverse insert_after refresh button) ──
        ungrouped = [s for s in group_sessions if s["username"] not in grouped_usernames]

        if ungrouped:
            for session in reversed(ungrouped[:20]):
                title = self._build_chat_title(session)
                item = rumps.MenuItem(title)
                item.add(rumps.MenuItem("📝 总结新消息", callback=self._make_summary_callback(session)))
                item.add(rumps.MenuItem("🔧 自定义总结…", callback=self._make_custom_summary_callback(session)))
                item.add(rumps.MenuItem("📅 按天总结…", callback=self._make_daily_summary_callback(session)))
                self.menu.insert_after("🔍 关键词搜索", item)
        elif not groups:
            self.menu.insert_after("🔍 关键词搜索", rumps.MenuItem("📎 (暂无群聊)"))

        # ── Groups (insert_before in order before recent summaries) ──
        if groups:
            self._load_contacts_if_needed()
            for grp in groups:
                grp_menu = self._build_group_submenu(grp)
                self.menu.insert_before("📋 最近总结", grp_menu)

        # New group button (always at the bottom of group area)
        self.menu.insert_before("📋 最近总结",
                                rumps.MenuItem("📂 ✨ 新建分组…", callback=self._create_group))

    def _make_summary_callback(self, session):
        def callback(sender):
            if self._summarizing:
                _notify("微信总结", "请等待", "正在总结中...")
                return
            self._set_status(ICON_NORMAL)
            threading.Thread(
                target=self._summarize_group, args=(session,), daemon=True
            ).start()
        return callback

    def _make_custom_summary_callback(self, session):
        def callback(_):
            if self._summarizing:
                _notify("微信总结", "请等待", "正在总结中...")
                return
            self._delayed_run(self._show_custom_summary_dialog, session)
        return callback

    def _show_custom_summary_dialog(self, session):
        group_name = session["name"]
        self._bring_to_front()
        try:
            if not _HAS_APPKIT:
                # Fallback: use _input_dialog with single input field
                clicked, text = self._input_dialog(
                    "自定义总结",
                    f"群聊：{group_name}\n\n输入条数（如 50）或分钟数加m（如 30m）",
                    default_text="50", ok="开始总结",
                )
                if clicked and text.strip():
                    text = text.strip()
                    if text.lower().endswith("m"):
                        minutes = int(text[:-1])
                        threading.Thread(
                            target=self._summarize_group,
                            args=(session,),
                            kwargs={"custom_minutes": minutes},
                            daemon=True,
                        ).start()
                    else:
                        count = int(text)
                        threading.Thread(
                            target=self._summarize_group,
                            args=(session,),
                            kwargs={"custom_count": count},
                            daemon=True,
                        ).start()
                return

            # ── PyObjC dual input fields ──
            alert = NSAlert.alloc().init()
            alert.setMessageText_("自定义总结")
            alert.setInformativeText_(
                f"群聊：{group_name}\n以下两项填一项即可（不要都填）"
            )
            # Set dialog icon
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.addButtonWithTitle_("开始总结")
            alert.addButtonWithTitle_("取消")

            view = NSView.alloc().initWithFrame_(((0, 0), (300, 60)))

            label1 = NSTextField.alloc().initWithFrame_(((0, 35), (80, 22)))
            label1.setStringValue_("消息条数：")
            label1.setBezeled_(False)
            label1.setEditable_(False)
            label1.setDrawsBackground_(False)
            view.addSubview_(label1)

            field1 = NSTextField.alloc().initWithFrame_(((80, 35), (210, 22)))
            field1.setPlaceholderString_("如 50")
            view.addSubview_(field1)

            label2 = NSTextField.alloc().initWithFrame_(((0, 5), (80, 22)))
            label2.setStringValue_("最近分钟：")
            label2.setBezeled_(False)
            label2.setEditable_(False)
            label2.setDrawsBackground_(False)
            view.addSubview_(label2)

            field2 = NSTextField.alloc().initWithFrame_(((80, 5), (210, 22)))
            field2.setPlaceholderString_("如 30 = 最近30分钟")
            view.addSubview_(field2)

            alert.setAccessoryView_(view)
            alert.window().setInitialFirstResponder_(field1)

            result = alert.runModal()
            if result != 1000:  # NSAlertFirstButtonReturn
                return

            count_str = str(field1.stringValue()).strip()
            minutes_str = str(field2.stringValue()).strip()

            if count_str and minutes_str:
                _notify("微信总结", "输入错误", "请只填一项，不要两项都填")
                return
            if not count_str and not minutes_str:
                _notify("微信总结", "输入错误", "请至少填写一项")
                return

            if count_str:
                try:
                    count = int(count_str)
                    if count <= 0:
                        raise ValueError
                except ValueError:
                    _notify("微信总结", "输入错误", "消息条数请输入正整数")
                    return
                threading.Thread(
                    target=self._summarize_group,
                    args=(session,),
                    kwargs={"custom_count": count},
                    daemon=True,
                ).start()
            else:
                try:
                    minutes = int(minutes_str)
                    if minutes <= 0:
                        raise ValueError
                except ValueError:
                    _notify("微信总结", "输入错误", "分钟数请输入正整数")
                    return
                threading.Thread(
                    target=self._summarize_group,
                    args=(session,),
                    kwargs={"custom_minutes": minutes},
                    daemon=True,
                ).start()
        except Exception:
            traceback.print_exc()
        finally:
            self._release_front()

    def _make_daily_summary_callback(self, session):
        def callback(_):
            if self._summarizing:
                _notify("微信总结", "请等待", "正在总结中...")
                return
            self._delayed_run(self._show_daily_summary_dialog, session)
        return callback

    def _show_daily_summary_dialog(self, session):
        import calendar
        group_name = session["name"]
        now = datetime.now()
        self._bring_to_front()
        try:
            if not _HAS_APPKIT:
                clicked, text = self._input_dialog(
                    "按天总结",
                    f"群聊：{group_name}\n\n输入日期，格式 YYYY-MM-DD\n例如：2026-05-12",
                    default_text=now.strftime("%Y-%m-%d"), ok="开始总结",
                )
                if clicked and text.strip():
                    try:
                        target_date = datetime.strptime(text.strip(), "%Y-%m-%d")
                    except ValueError:
                        _notify("微信总结", "日期格式错误", "请使用 YYYY-MM-DD 格式")
                        return
                    threading.Thread(
                        target=self._daily_summarize,
                        args=(session, target_date),
                        daemon=True,
                    ).start()
                return

            alert = NSAlert.alloc().init()
            alert.setMessageText_("📅 按天总结")
            alert.setInformativeText_(f"群聊：{group_name}\n选择要总结的日期")
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.addButtonWithTitle_("开始总结")
            alert.addButtonWithTitle_("取消")

            view = NSView.alloc().initWithFrame_(((0, 0), (320, 32)))

            lbl_y = NSTextField.alloc().initWithFrame_(((0, 5), (30, 22)))
            lbl_y.setStringValue_("年")
            lbl_y.setBezeled_(False)
            lbl_y.setEditable_(False)
            lbl_y.setDrawsBackground_(False)
            view.addSubview_(lbl_y)

            years = [str(y) for y in range(now.year - 2, now.year + 1)]
            popup_year = NSPopUpButton.alloc().initWithFrame_pullsDown_(((28, 5), (75, 22)), False)
            popup_year.addItemsWithTitles_(years)
            popup_year.selectItemWithTitle_(str(now.year))
            view.addSubview_(popup_year)

            lbl_m = NSTextField.alloc().initWithFrame_(((110, 5), (30, 22)))
            lbl_m.setStringValue_("月")
            lbl_m.setBezeled_(False)
            lbl_m.setEditable_(False)
            lbl_m.setDrawsBackground_(False)
            view.addSubview_(lbl_m)

            months = [str(m) for m in range(1, 13)]
            popup_month = NSPopUpButton.alloc().initWithFrame_pullsDown_(((138, 5), (55, 22)), False)
            popup_month.addItemsWithTitles_(months)
            popup_month.selectItemWithTitle_(str(now.month))
            view.addSubview_(popup_month)

            lbl_d = NSTextField.alloc().initWithFrame_(((200, 5), (30, 22)))
            lbl_d.setStringValue_("日")
            lbl_d.setBezeled_(False)
            lbl_d.setEditable_(False)
            lbl_d.setDrawsBackground_(False)
            view.addSubview_(lbl_d)

            days = [str(d) for d in range(1, 32)]
            popup_day = NSPopUpButton.alloc().initWithFrame_pullsDown_(((228, 5), (55, 22)), False)
            popup_day.addItemsWithTitles_(days)
            popup_day.selectItemWithTitle_(str(now.day))
            view.addSubview_(popup_day)

            alert.setAccessoryView_(view)

            result = alert.runModal()
            if result != 1000:
                return

            year = int(str(popup_year.titleOfSelectedItem()))
            month = int(str(popup_month.titleOfSelectedItem()))
            day = int(str(popup_day.titleOfSelectedItem()))

            max_day = calendar.monthrange(year, month)[1]
            if day > max_day:
                _notify("微信总结", "日期无效", f"{year}年{month}月只有{max_day}天")
                return

            target_date = datetime(year, month, day)
            if target_date > now:
                _notify("微信总结", "日期无效", "不能选择未来的日期")
                return

            threading.Thread(
                target=self._daily_summarize,
                args=(session, target_date),
                daemon=True,
            ).start()

        except Exception:
            traceback.print_exc()
        finally:
            self._release_front()

    def _daily_summarize(self, session, target_date):
        """Summarize messages for a single day, chunked by 300 messages."""
        from datetime import timedelta

        CHUNK_SIZE = 300

        self._summarizing = True
        self._set_status(ICON_LOADING)

        try:
            username = session["username"]
            group_name = session["name"]
            date_str = target_date.strftime("%Y-%m-%d")

            day_start_ts = target_date.timestamp()
            day_end_ts = (target_date + timedelta(days=1)).timestamp()

            print(f"[daily] {group_name}: 按天总结 {date_str}")

            messages = self.db.get_messages(username, since_ts=day_start_ts, limit=10000)
            messages = [m for m in messages if m["timestamp"] < day_end_ts]

            if not messages:
                _notify("微信总结", group_name, f"{date_str} 没有消息")
                return

            msg_count = len(messages)
            start_time = messages[0]["time_str"]
            end_time = messages[-1]["time_str"]

            if not self.ai:
                try:
                    self.ai = create_provider(self.config)
                except Exception as e:
                    _notify("微信总结", "AI 未配置", str(e))
                    if "Key" in str(e):
                        self._set_api_key(None)
                    return

            chunks = [messages[i:i + CHUNK_SIZE] for i in range(0, len(messages), CHUNK_SIZE)]
            total_chunks = len(chunks)
            print(f"[daily] {group_name}: {date_str} 共 {msg_count} 条消息，分 {total_chunks} 段总结")

            _notify("微信总结", f"📅 {group_name}",
                    f"{date_str} · {msg_count}条消息 · 分{total_chunks}段，开始总结...")

            summaries = []
            for idx, chunk in enumerate(chunks, 1):
                chunk_start = chunk[0]["time_str"]
                chunk_end = chunk[-1]["time_str"]
                chunk_text = self.db.format_messages_for_ai(
                    chunk,
                    show_group_nickname=self.config.get("show_group_nickname", True),
                )

                start_short = chunk_start.split(" ", 1)[-1]
                end_short = chunk_end.split(" ", 1)[-1]
                print(f"[daily]   第 {idx}/{total_chunks} 段: {len(chunk)} 条 ({start_short} ~ {end_short})，调用 AI...")
                _notify("微信总结", f"📅 {group_name}",
                        f"正在总结第 {idx}/{total_chunks} 段（{start_short} ~ {end_short}）...")

                prompt = self.ai.build_prompt(
                    group_name=group_name,
                    messages_text=chunk_text,
                    start_time=chunk_start,
                    end_time=chunk_end,
                    msg_count=len(chunk),
                )
                try:
                    summary = self.ai.summarize(prompt)
                except Exception as e:
                    print(f"[daily]   ✗ 第 {idx} 段失败: {e}")
                    summary = f"（此段总结失败：{e}）"
                summaries.append({
                    "text": summary,
                    "start": chunk_start,
                    "end": chunk_end,
                    "count": len(chunk),
                })

            full_summary = self._combine_daily_summaries(summaries, total_chunks)

            summary_file = self._save_daily_summary(
                group_name, full_summary, msg_count, date_str,
                start_time, end_time, total_chunks,
            )

            self._last_summary = {
                "group": f"📅 {group_name}",
                "text": full_summary,
                "file": summary_file,
                "msg_count": msg_count,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._run_on_main(self._refresh_menu_after_summary)

            _notify("微信总结", f"📅 {group_name}",
                    f"{date_str} · {msg_count}条消息 · {total_chunks}段已总结")
            print(f"[daily] ✓ {group_name} {date_str} 总结完成")

            subprocess.run(["open", summary_file])
            self._set_status(ICON_DONE)

        except Exception as e:
            _notify("微信总结", "按天总结失败", str(e))
            traceback.print_exc()
            self._set_status(ICON_ERROR)
        finally:
            self._summarizing = False

    def _combine_daily_summaries(self, summaries, total_chunks):
        if total_chunks == 1:
            return summaries[0]["text"]
        parts = []
        for s in summaries:
            start_short = s["start"].split(" ", 1)[-1]
            end_short = s["end"].split(" ", 1)[-1]
            header = f"## {start_short} ~ {end_short}（{s['count']}条消息）\n"
            parts.append(header + s["text"])
        return "\n\n".join(parts)

    def _save_daily_summary(self, group_name, summary, msg_count, date_str,
                            start_time, end_time, chunk_count):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in group_name)
        filename = f"daily_{safe_name}_{date_str}_{timestamp}.txt"
        filepath = os.path.join(SUMMARY_DIR, filename)

        chunk_note = f"（分 {chunk_count} 段总结）" if chunk_count > 1 else ""
        header = (
            f"{'='*50}\n"
            f"  📅 按天总结：{group_name}\n"
            f"  日期：{date_str}\n"
            f"  {msg_count} 条消息 · {start_time} ~ {end_time}{chunk_note}\n"
            f"  生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n"
            f"{'='*50}\n\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(header + summary)
        return filepath

    # ── Summary logic ────────────────────────────────────────

    def _summarize_group(self, session, custom_count=None, custom_minutes=None):
        self._summarizing = True
        self._set_status(ICON_LOADING)

        try:
            username = session["username"]
            group_name = session["name"]

            if custom_minutes:
                since_ts = time.time() - custom_minutes * 60
                print(f"[summary] {group_name}: 自定义总结最近 {custom_minutes} 分钟...")
                messages = self.db.get_messages(username, since_ts=since_ts, limit=500)
            elif custom_count:
                print(f"[summary] {group_name}: 自定义总结最近 {custom_count} 条...")
                messages = self.db.get_messages(username, since_ts=0, limit=custom_count)
            else:
                since_ts = get_bookmark(username)
                if since_ts > 0:
                    since_str = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M")
                    print(f"[summary] {group_name}: 读取 {since_str} 之后的新消息...")
                else:
                    print(f"[summary] {group_name}: 首次总结，读取最近消息...")
                messages = self.db.get_messages(username, since_ts=since_ts, limit=500)
            if not messages:
                _notify("微信总结", group_name, "没有新消息")
                return

            messages_text = self.db.format_messages_for_ai(messages, show_group_nickname=self.config.get("show_group_nickname", True))
            start_time = messages[0]["time_str"]
            end_time = messages[-1]["time_str"]
            msg_count = len(messages)

            print(f"[summary] {group_name}: 共 {msg_count} 条消息 ({start_time} ~ {end_time}), 正在调用 AI...")

            if not self.ai:
                try:
                    self.ai = create_provider(self.config)
                except Exception as e:
                    _notify("微信总结", "AI 未配置", str(e))
                    if "Key" in str(e):
                        self._set_api_key(None)
                    return

            prompt = self.ai.build_prompt(
                group_name=group_name,
                messages_text=messages_text,
                start_time=start_time,
                end_time=end_time,
                msg_count=msg_count,
            )

            summary = self.ai.summarize(prompt)

            # Update bookmark
            set_bookmark(username, messages[-1]["timestamp"])

            # Save to file
            summary_file = self._save_summary(group_name, summary, msg_count, start_time, end_time)

            # Update menu
            self._last_summary = {
                "group": group_name,
                "text": summary,
                "file": summary_file,
                "msg_count": msg_count,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._run_on_main(self._refresh_menu_after_summary)

            _notify("微信总结", f"✅ {group_name}", f"{msg_count}条消息已总结")
            print(f"[summary] ✓ {group_name} 总结完成")

            # Auto-open summary file
            subprocess.run(["open", summary_file])

            self._set_status(ICON_DONE)

        except Exception as e:
            _notify("微信总结", "总结失败", str(e))
            traceback.print_exc()
            self._set_status(ICON_ERROR)
        finally:
            self._summarizing = False

    def _save_summary(self, group_name, summary, msg_count, start_time, end_time):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in group_name)
        filename = f"{safe_name}_{timestamp}.txt"
        filepath = os.path.join(SUMMARY_DIR, filename)

        header = (
            f"{'='*50}\n"
            f"  {group_name}\n"
            f"  {msg_count} 条消息 · {start_time} ~ {end_time}\n"
            f"  生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n"
            f"{'='*50}\n\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(header + summary)
        return filepath

    # ── Summary menu display ──────────────────────────────────

    def _refresh_menu_after_summary(self):
        """Refresh all menus after summary completes (must be called on main thread)."""
        self._rebuild_chat_menu()
        self._update_latest_summary()
        self._rebuild_summary_history()

    def _update_latest_summary(self):
        """Update latest summary display (above recent summaries menu)."""
        for key in list(self.menu.keys()):
            if isinstance(key, str) and key.startswith("📝"):
                del self.menu[key]

        s = self._last_summary
        if not s:
            return

        title = f"📝 {s['group']}（{s['msg_count']}条 · {s['time']}）"
        parent = rumps.MenuItem(title)

        # Preview first few lines
        for line in s["text"].strip().split("\n")[:6]:
            line = line.strip()
            if not line:
                continue
            display = line[:45] + "…" if len(line) > 45 else line
            parent.add(rumps.MenuItem(display))

        parent.add(rumps.separator)
        parent.add(rumps.MenuItem("📋 复制到剪贴板", callback=self._copy_summary))
        parent.add(rumps.MenuItem("📄 查看完整内容", callback=self._make_open_file_callback(s["file"])))

        self.menu.insert_before("📋 最近总结", parent)

    def _copy_summary(self, _):
        if not self._last_summary:
            return
        process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        process.communicate(self._last_summary["text"].encode("utf-8"))
        _notify("微信总结", "已复制", "总结内容已复制到剪贴板")

    def _rebuild_summary_history(self):
        """Rebuild recent summaries submenu (excludes the latest one, already shown separately)."""
        if "📋 最近总结" in self.menu:
            del self.menu["📋 最近总结"]

        parent = rumps.MenuItem("📋 最近总结")
        summaries = self._get_recent_summaries(limit=15)

        # Exclude the latest summary already shown separately above
        latest_file = self._last_summary.get("file") if self._last_summary else None

        has_items = False
        for s in summaries:
            if s["path"] == latest_file:
                continue
            item = rumps.MenuItem(s["display"], callback=self._make_open_file_callback(s["path"]))
            parent.add(item)
            has_items = True

        if has_items:
            parent.add(rumps.separator)
        parent.add(rumps.MenuItem("📁 打开总结目录", callback=self._open_summary_dir))

        self.menu.insert_before("⚙️ 设置", parent)

    def _get_recent_summaries(self, limit=15):
        """Read recent summary file list from summary directory."""
        summaries = []
        if not os.path.isdir(SUMMARY_DIR):
            return summaries

        for f in os.listdir(SUMMARY_DIR):
            if not f.endswith(".txt"):
                continue
            path = os.path.join(SUMMARY_DIR, f)
            mtime = os.path.getmtime(path)

            # Read group name from second line of file header
            try:
                with open(path, encoding="utf-8") as fh:
                    fh.readline()  # skip "===="
                    group_name = fh.readline().strip()
            except Exception:
                group_name = f[:-4]

            time_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            display = f"{group_name}（{time_str}）"
            summaries.append({"path": path, "display": display, "mtime": mtime})

        summaries.sort(key=lambda x: x["mtime"], reverse=True)
        return summaries[:limit]

    def _make_open_file_callback(self, filepath):
        def callback(_):
            subprocess.run(["open", filepath])
        return callback

    # ── Group management ────────────────────────────────────────

    def _build_group_submenu(self, grp):
        """Build submenu for a single group."""
        grp_name = grp["name"]
        chat_count = len(grp["chats"])

        grp_summary_time = get_group_summary_time(grp_name)
        if grp_summary_time:
            grp_title = f"📂 {grp_name}（上次总结 {grp_summary_time}）"
        elif chat_count > 0:
            grp_title = f"📂 {grp_name}（{chat_count}个群 · 未总结）"
        else:
            grp_title = f"📂 {grp_name}（空）"

        grp_menu = rumps.MenuItem(grp_title)

        if grp["chats"]:
            for chat_user in grp["chats"]:
                display = self._get_chat_display_name(chat_user)
                bookmark_ts = get_bookmark(chat_user)

                if bookmark_ts > 0:
                    new_count = self.db.count_messages_since(chat_user, bookmark_ts)
                    if new_count > 0:
                        chat_label = f"   {display}（{new_count}条未读）"
                    else:
                        chat_label = f"   {display}（无更新）"
                else:
                    chat_label = f"   {display}（未总结）"

                chat_item = rumps.MenuItem(chat_label)
                chat_session = {"username": chat_user, "name": display, "is_group": True}
                chat_item.add(rumps.MenuItem("📝 总结新消息", callback=self._make_summary_callback(chat_session)))
                chat_item.add(rumps.MenuItem("🔧 自定义总结…", callback=self._make_custom_summary_callback(chat_session)))
                chat_item.add(rumps.MenuItem("📅 按天总结…", callback=self._make_daily_summary_callback(chat_session)))
                chat_item.add(rumps.separator)
                chat_item.add(rumps.MenuItem("❌ 从分组移除", callback=self._make_remove_from_group_callback(grp_name, chat_user)))
                grp_menu.add(chat_item)

            grp_menu.add(rumps.separator)

        grp_menu.add(rumps.MenuItem("➕ 添加群聊…", callback=self._make_add_to_group_callback(grp_name)))
        grp_menu.add(rumps.separator)
        grp_menu.add(rumps.MenuItem(f"🚀 一键总结「{grp_name}」", callback=self._make_batch_summary_callback(grp_name)))
        grp_menu.add(rumps.MenuItem("🗑️ 删除分组", callback=self._make_delete_group_callback(grp_name)))

        return grp_menu

    def _load_contacts_if_needed(self):
        """Ensure contacts are loaded."""
        if self.db:
            self.db._load_contacts()

    def _get_chat_display_name(self, username):
        """Get display name for a group chat."""
        if self.db and self.db._contacts:
            return self.db._contacts.get(username, username)
        return username

    def _create_group(self, _):
        """Create new group (delayed dialog)."""
        self._delayed_run(self._show_create_group_dialog)

    def _show_create_group_dialog(self):
        self._bring_to_front()
        try:
            clicked, text = self._input_dialog(
                "新建分组",
                "请输入分组名称，例如：购物群、工作群、学习群",
                ok="创建",
            )
            if clicked and text.strip():
                name = text.strip()
                if create_group(name):
                    _notify("微信总结", "分组已创建", f"「{name}」，现在可以添加群聊了")
                    self._rebuild_chat_menu()
                else:
                    _notify("微信总结", "创建失败", f"「{name}」已存在")
        finally:
            self._release_front()

    def _make_delete_group_callback(self, group_name):
        def callback(_):
            self._delayed_run(self._show_delete_group_dialog, group_name)
        return callback

    def _show_delete_group_dialog(self, group_name):
        self._bring_to_front()
        try:
            confirmed = self._confirm_dialog(
                "删除分组",
                f"确定要删除分组「{group_name}」吗？\n（不会影响群聊本身，只是移除分组）",
                ok="确定删除",
            )
            if confirmed:
                delete_group(group_name)
                _notify("微信总结", "已删除", f"分组「{group_name}」已移除")
                self._rebuild_chat_menu()
        finally:
            self._release_front()

    def _make_add_to_group_callback(self, group_name):
        def callback(_):
            self._delayed_run(self._show_add_to_group_dialog, group_name)
        return callback

    def _show_add_to_group_dialog(self, group_name):
        if not self.db:
            _notify("微信总结", "未初始化", "请先确保微信已登录")
            return

        # Get all group chats from contact.db (not limited by session count)
        group_sessions = self.db.get_groups()

        if not group_sessions:
            _notify("微信总结", "暂无群聊", "请先刷新群聊列表")
            return

        # Chats already in this group
        existing = set(get_group_chats(group_name))

        # Build selection list (exclude already added)
        available = [s for s in group_sessions if s["username"] not in existing]
        if not available:
            _notify("微信总结", "无可添加群聊", "所有群聊已在该分组中")
            return

        self._bring_to_front()
        try:
            lines = []
            for i, s in enumerate(available, 1):
                lines.append(f"{i}. {s['name']}")
            msg = f"输入要添加到「{group_name}」的群聊序号（多个用逗号分隔）：\n\n" + "\n".join(lines)

            clicked, text = self._input_dialog(
                f"添加群聊到「{group_name}」", msg,
                ok="添加", width=380,
            )
            if clicked and text.strip():
                added = []
                for part in text.strip().replace("，", ",").split(","):
                    try:
                        idx = int(part.strip()) - 1
                        if 0 <= idx < len(available):
                            s = available[idx]
                            add_chat_to_group(group_name, s["username"])
                            added.append(s["name"])
                    except ValueError:
                        pass
                if added:
                    _notify("微信总结", f"已添加到「{group_name}」", "、".join(added))
                    self._rebuild_chat_menu()
        finally:
            self._release_front()

    def _make_remove_from_group_callback(self, group_name, chat_username):
        def callback(_):
            display = self._get_chat_display_name(chat_username)
            remove_chat_from_group(group_name, chat_username)
            _notify("微信总结", "已移除", f"「{display}」已从「{group_name}」移除")
            self._rebuild_chat_menu()
        return callback

    def _make_batch_summary_callback(self, group_name):
        def callback(_):
            if self._summarizing:
                _notify("微信总结", "请等待", "正在总结中...")
                return
            self._set_status(ICON_NORMAL)
            threading.Thread(
                target=self._batch_summarize, args=(group_name,), daemon=True
            ).start()
        return callback

    def _batch_summarize(self, group_name):
        """Batch summarize all chats in a group."""
        self._summarizing = True
        self._set_status(ICON_LOADING)

        try:
            chat_usernames = get_group_chats(group_name)
            if not chat_usernames:
                _notify("微信总结", group_name, "分组中没有群聊")
                return

            if not self.ai:
                try:
                    self.ai = create_provider(self.config)
                except Exception as e:
                    _notify("微信总结", "AI 未配置", str(e))
                    if "Key" in str(e):
                        self._set_api_key(None)
                    return

            print(f"[batch] 开始批量总结分组「{group_name}」，共 {len(chat_usernames)} 个群...")

            groups_data = []
            total_msgs = 0

            for username in chat_usernames:
                chat_name = self._get_chat_display_name(username)
                since_ts = get_bookmark(username)

                batch_limit = self.config.get("batch_msg_limit", 100)
                messages = self.db.get_messages(username, since_ts=since_ts, limit=batch_limit)

                if messages:
                    messages_text = self.db.format_messages_for_ai(messages, show_group_nickname=self.config.get("show_group_nickname", True))
                    start_time = messages[0]["time_str"]
                    end_time = messages[-1]["time_str"]
                    msg_count = len(messages)
                    total_msgs += msg_count

                    groups_data.append({
                        "name": chat_name,
                        "username": username,
                        "messages_text": messages_text,
                        "start_time": start_time,
                        "end_time": end_time,
                        "msg_count": msg_count,
                        "last_msg_ts": messages[-1]["timestamp"],
                    })
                    print(f"[batch]   {chat_name}: {msg_count} 条消息（限 {batch_limit}）")
                else:
                    groups_data.append({
                        "name": chat_name,
                        "username": username,
                        "messages_text": "",
                        "start_time": "",
                        "end_time": "",
                        "msg_count": 0,
                        "last_msg_ts": 0,
                    })
                    print(f"[batch]   {chat_name}: 无新消息")

            if total_msgs == 0:
                _notify("微信总结", group_name, "所有群聊都没有新消息")
                return

            print(f"[batch] 共 {total_msgs} 条消息，正在调用 AI...")

            prompt = self.ai.build_batch_prompt(group_name, groups_data)
            summary = self.ai.summarize(prompt)

            # Update bookmarks for all chats with messages
            for g in groups_data:
                if g["last_msg_ts"] > 0:
                    set_bookmark(g["username"], g["last_msg_ts"])

            # Record group summary time
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            set_group_summary_time(group_name, now_str)

            # Save summary
            summary_file = self._save_batch_summary(group_name, summary, groups_data, total_msgs)

            # Update menu
            self._last_summary = {
                "group": f"📂 {group_name}",
                "text": summary,
                "file": summary_file,
                "msg_count": total_msgs,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._run_on_main(self._refresh_menu_after_summary)

            _notify("微信总结", f"✅ {group_name}", f"{len(groups_data)}个群 · {total_msgs}条消息已总结")
            print(f"[batch] ✓ 分组「{group_name}」总结完成")

            # Auto-open summary file
            subprocess.run(["open", summary_file])

            self._set_status(ICON_DONE)

        except Exception as e:
            _notify("微信总结", "批量总结失败", str(e))
            traceback.print_exc()
            self._set_status(ICON_ERROR)
        finally:
            self._summarizing = False

    def _save_batch_summary(self, group_name, summary, groups_data, total_msgs):
        """Save batch summary."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in group_name)
        filename = f"batch_{safe_name}_{timestamp}.txt"
        filepath = os.path.join(SUMMARY_DIR, filename)

        group_list = ", ".join(g["name"] for g in groups_data)
        header = (
            f"{'='*50}\n"
            f"  📂 分组总结：{group_name}\n"
            f"  包含群聊：{group_list}\n"
            f"  共 {total_msgs} 条消息\n"
            f"  生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n"
            f"{'='*50}\n\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(header + summary)
        return filepath

    # ── Keyword search ──────────────────────────────────────

    def _on_search_click(self, _):
        """Search menu item clicked (delayed dialog, let menu close first)."""
        if not self.db:
            _notify("微信总结", "未初始化", "请先确保微信已登录")
            return
        if self._summarizing:
            _notify("微信总结", "请等待", "正在处理中...")
            return
        self._delayed_run(self._show_search_dialog)

    def _show_search_dialog(self):
        """Show keyword search dialog."""
        if not self.db:
            return

        # Get all group chats from contact.db (not limited by session count)
        group_sessions = self.db.get_groups()

        if not group_sessions:
            _notify("微信总结", "暂无群聊", "请先刷新群聊列表")
            return

        self._bring_to_front()
        try:
            if not _HAS_APPKIT:
                # Fallback: single input field
                self._show_search_dialog_fallback(group_sessions)
                return

            # ── PyObjC multi-input dialog ──
            alert = NSAlert.alloc().init()
            if os.path.isfile(APP_ICON_PNG):
                _icon = NSImage.alloc().initWithContentsOfFile_(APP_ICON_PNG)
                if _icon:
                    alert.setIcon_(_icon)
            alert.setMessageText_("🔍 关键词搜索")
            alert.setInformativeText_("多个关键词用空格分隔（布尔与搜索：必须同时出现）")
            alert.addButtonWithTitle_("开始搜索")
            alert.addButtonWithTitle_("取消")

            # Build group chat list text
            group_lines = []
            for i, s in enumerate(group_sessions, 1):
                group_lines.append(f"{i}. {s['name']}")
            groups_text = "\n".join(group_lines)

            # Custom view: input fields + scrollable chat list
            view = NSView.alloc().initWithFrame_(((0, 0), (380, 310)))

            # Row 4 (y=283): Keywords
            lbl_kw = NSTextField.alloc().initWithFrame_(((0, 283), (80, 22)))
            lbl_kw.setStringValue_("关键词：")
            lbl_kw.setBezeled_(False)
            lbl_kw.setEditable_(False)
            lbl_kw.setDrawsBackground_(False)
            view.addSubview_(lbl_kw)

            field_kw = NSTextField.alloc().initWithFrame_(((80, 283), (290, 22)))
            field_kw.setPlaceholderString_("如 claude api")
            view.addSubview_(field_kw)

            # Row 3 (y=253): Start date
            lbl_start = NSTextField.alloc().initWithFrame_(((0, 253), (80, 22)))
            lbl_start.setStringValue_("开始日期：")
            lbl_start.setBezeled_(False)
            lbl_start.setEditable_(False)
            lbl_start.setDrawsBackground_(False)
            view.addSubview_(lbl_start)

            field_start = NSTextField.alloc().initWithFrame_(((80, 253), (290, 22)))
            field_start.setPlaceholderString_("如 2026-03-01")
            view.addSubview_(field_start)

            # Row 2 (y=223): End date
            lbl_end = NSTextField.alloc().initWithFrame_(((0, 223), (80, 22)))
            lbl_end.setStringValue_("结束日期：")
            lbl_end.setBezeled_(False)
            lbl_end.setEditable_(False)
            lbl_end.setDrawsBackground_(False)
            view.addSubview_(lbl_end)

            field_end = NSTextField.alloc().initWithFrame_(((80, 223), (290, 22)))
            field_end.setPlaceholderString_("留空 = 今天")
            view.addSubview_(field_end)

            # Row 1 (y=193): Chat scope
            lbl_scope = NSTextField.alloc().initWithFrame_(((0, 193), (80, 22)))
            lbl_scope.setStringValue_("群聊范围：")
            lbl_scope.setBezeled_(False)
            lbl_scope.setEditable_(False)
            lbl_scope.setDrawsBackground_(False)
            view.addSubview_(lbl_scope)

            field_scope = NSTextField.alloc().initWithFrame_(((80, 193), (290, 22)))
            field_scope.setPlaceholderString_("全部 或 序号如 1,3,5")
            field_scope.setStringValue_("全部")
            view.addSubview_(field_scope)

            # Row 0 (y=163): AI summary checkbox
            checkbox_ai = NSButton.alloc().initWithFrame_(((80, 163), (290, 22)))
            checkbox_ai.setButtonType_(3)  # NSSwitchButton (checkbox)
            checkbox_ai.setTitle_("用 AI 总结搜索结果")
            checkbox_ai.setState_(0)  # Default unchecked
            view.addSubview_(checkbox_ai)

            # Scrollable chat list (fixed height, won't fill the screen)
            lbl_groups = NSTextField.alloc().initWithFrame_(((0, 133), (380, 22)))
            lbl_groups.setStringValue_(f"可选群聊（共 {len(group_sessions)} 个）：")
            lbl_groups.setBezeled_(False)
            lbl_groups.setEditable_(False)
            lbl_groups.setDrawsBackground_(False)
            view.addSubview_(lbl_groups)

            scroll = NSScrollView.alloc().initWithFrame_(((0, 0), (380, 130)))
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(NSBezelBorder)
            text_view = NSTextView.alloc().initWithFrame_(((0, 0), (360, 130)))
            text_view.setEditable_(False)
            text_view.setString_(groups_text)
            text_view.setFont_(NSFont.systemFontOfSize_(11))
            scroll.setDocumentView_(text_view)
            view.addSubview_(scroll)

            alert.setAccessoryView_(view)
            alert.window().setInitialFirstResponder_(field_kw)

            result = alert.runModal()
            if result != 1000:  # NSAlertFirstButtonReturn
                return

            # ── Read input ──
            kw_str = str(field_kw.stringValue()).strip()
            start_str = str(field_start.stringValue()).strip()
            end_str = str(field_end.stringValue()).strip()
            scope_str = str(field_scope.stringValue()).strip()
            use_ai = checkbox_ai.state() == 1

            # ── Validate input ──
            if not kw_str:
                _notify("微信总结", "输入错误", "请输入搜索关键词")
                return

            keywords = kw_str.split()

            # Parse start date
            if not start_str:
                _notify("微信总结", "输入错误", "请输入开始日期")
                return
            try:
                start_ts = datetime.strptime(start_str, "%Y-%m-%d").timestamp()
            except ValueError:
                _notify("微信总结", "日期格式错误", "请使用 YYYY-MM-DD 格式，如 2026-03-01")
                return

            # Parse end date
            if end_str:
                try:
                    # Set end date to 23:59:59 of the day
                    end_ts = datetime.strptime(end_str, "%Y-%m-%d").timestamp() + 86399
                except ValueError:
                    _notify("微信总结", "日期格式错误", "请使用 YYYY-MM-DD 格式，如 2026-03-09")
                    return
            else:
                end_ts = time.time()  # Empty = current time

            if start_ts > end_ts:
                _notify("微信总结", "日期错误", "开始日期不能晚于结束日期")
                return

            # Parse chat scope
            if not scope_str or scope_str == "全部":
                search_usernames = [s["username"] for s in group_sessions]
            else:
                search_usernames = []
                for part in scope_str.replace("，", ",").split(","):
                    try:
                        idx = int(part.strip()) - 1
                        if 0 <= idx < len(group_sessions):
                            search_usernames.append(group_sessions[idx]["username"])
                    except ValueError:
                        pass
                if not search_usernames:
                    _notify("微信总结", "输入错误", "未选择有效群聊，请输入「全部」或群聊序号")
                    return

            # ── Start background search ──
            print(f"[搜索] 关键词={keywords}, 群聊数={len(search_usernames)}, "
                  f"时间={start_str}~{end_str or '今天'}, AI={use_ai}")
            threading.Thread(
                target=self._do_search,
                args=(keywords, kw_str, search_usernames, start_ts, end_ts, use_ai),
                daemon=True,
            ).start()

        except Exception as e:
            print(f"[搜索] ❌ 异常：{e}")
            traceback.print_exc()
        finally:
            self._release_front()

    def _show_search_dialog_fallback(self, group_sessions):
        """Fallback search dialog when AppKit is unavailable."""
        clicked, text = self._input_dialog(
            "🔍 关键词搜索",
            "格式：关键词|开始日期|结束日期\n"
            "例如：claude api|2026-03-01|2026-03-09\n\n"
            "多个关键词用空格分隔（必须同时出现）\n"
            "结束日期留空则为今天\n"
            "将搜索所有群聊，不使用 AI 总结",
            ok="搜索", width=380,
        )
        if not clicked or not text.strip():
            return

        parts = text.strip().split("|")
        if len(parts) < 2:
            _notify("微信总结", "格式错误", "请用 | 分隔关键词和日期")
            return

        kw_str = parts[0].strip()
        keywords = kw_str.split()
        if not keywords:
            _notify("微信总结", "输入错误", "请输入关键词")
            return

        try:
            start_ts = datetime.strptime(parts[1].strip(), "%Y-%m-%d").timestamp()
        except ValueError:
            _notify("微信总结", "日期格式错误", "请使用 YYYY-MM-DD 格式")
            return

        if len(parts) >= 3 and parts[2].strip():
            try:
                end_ts = datetime.strptime(parts[2].strip(), "%Y-%m-%d").timestamp() + 86399
            except ValueError:
                end_ts = time.time()
        else:
            end_ts = time.time()

        search_usernames = [s["username"] for s in group_sessions]

        threading.Thread(
            target=self._do_search,
            args=(keywords, kw_str, search_usernames, start_ts, end_ts, False),
            daemon=True,
        ).start()

    def _do_search(self, keywords, kw_str, usernames, start_ts, end_ts, use_ai):
        """Execute keyword search in background (read-only, does not modify any bookmarks or data)."""
        self._summarizing = True
        self._set_status(ICON_LOADING)

        try:
            start_display = datetime.fromtimestamp(start_ts).strftime("%m-%d")
            end_display = datetime.fromtimestamp(end_ts).strftime("%m-%d")

            print(f"[搜索] 搜索关键词：{kw_str}，范围：{start_display}~{end_display}，"
                  f"群聊数：{len(usernames)}，AI总结：{use_ai}")

            # Get data coverage range (inform user which chats have how much data)
            coverage = self.db.get_fts_coverage(usernames)
            if coverage:
                for uname in usernames:
                    cov = coverage.get(uname)
                    if cov:
                        e = datetime.fromtimestamp(cov["earliest"]).strftime("%Y-%m-%d")
                        l = datetime.fromtimestamp(cov["latest"]).strftime("%Y-%m-%d")
                        gname = self.db._contacts.get(uname, uname) if self.db._contacts else uname
                        print(f"[搜索]   {gname}: 数据范围 {e} ~ {l} ({cov['count']}条)")

            # Execute search (prefer FTS full-text index, covers all historical data)
            results = self.db.search_messages(keywords, usernames, start_ts, end_ts)

            total_count = sum(len(msgs) for msgs in results.values())

            if total_count == 0:
                # Build data coverage description
                coverage_note = ""
                if coverage:
                    lines = []
                    for uname in usernames:
                        cov = coverage.get(uname)
                        gname = self.db._contacts.get(uname, uname) if self.db._contacts else uname
                        if cov:
                            e = datetime.fromtimestamp(cov["earliest"]).strftime("%Y-%m-%d")
                            lines.append(f"  {gname}: 数据从 {e} 起")
                        else:
                            lines.append(f"  {gname}: 无数据")
                    coverage_note = "\n数据覆盖：\n" + "\n".join(lines)

                print(f"[搜索] ⚠ 搜索完成，未找到包含 {keywords} 的消息")
                _notify("微信总结", "搜索完成 · 0 条结果",
                        f"未找到包含「{kw_str}」的消息")
                self._set_status(ICON_NORMAL)
                return

            print(f"[搜索] 命中 {total_count} 条消息，涉及 {len(results)} 个群")

            if use_ai:
                # AI summary mode
                if not self.ai:
                    try:
                        self.ai = create_provider(self.config)
                    except Exception as e:
                        _notify("微信总结", "AI 未配置", str(e))
                        return

                prompt = self.ai.build_search_prompt(kw_str, results, start_display, end_display)
                print(f"[search] 正在调用 AI 总结...")
                summary = self.ai.summarize(prompt)

                filepath = self._save_search_result(
                    kw_str, results, total_count, start_display, end_display,
                    ai_summary=summary
                )
            else:
                # Raw text mode
                filepath = self._save_search_result(
                    kw_str, results, total_count, start_display, end_display,
                    ai_summary=None
                )

            # Update latest summary display
            self._last_summary = {
                "group": f"🔍 搜索：{kw_str}",
                "text": summary if use_ai else f"搜索「{kw_str}」命中 {total_count} 条消息",
                "file": filepath,
                "msg_count": total_count,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._run_on_main(self._refresh_menu_after_summary)

            _notify("微信总结", f"🔍 搜索完成", f"「{kw_str}」命中 {total_count} 条消息")
            print(f"[搜索] ✓ 搜索完成，结果已保存")

            subprocess.run(["open", filepath])
            self._set_status(ICON_DONE)

        except Exception as e:
            _notify("微信总结", "搜索失败", str(e))
            traceback.print_exc()
            self._set_status(ICON_ERROR)
        finally:
            self._summarizing = False
            # Safety net: ensure icon doesn't get stuck on ⏳
            if self.title == ICON_LOADING:
                self._set_status(ICON_NORMAL)

    def _save_search_result(self, kw_str, results, total_count, start_display, end_display, ai_summary=None):
        """Save search results to file (does not modify any bookmarks)."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_kw = "".join(c if c.isalnum() or c in "._-" else "_" for c in kw_str)

        if ai_summary:
            filename = f"search_ai_{safe_kw}_{timestamp}.txt"
        else:
            filename = f"search_{safe_kw}_{timestamp}.txt"

        filepath = os.path.join(SUMMARY_DIR, filename)

        group_count = len(results)
        mode_label = "AI总结" if ai_summary else "原文"

        # Calculate actual data range for each chat
        actual_ranges = []
        for username, messages in results.items():
            if messages:
                group_name = messages[0]["group_name"]
                earliest = min(m["timestamp"] for m in messages)
                latest = max(m["timestamp"] for m in messages)
                e = datetime.fromtimestamp(earliest).strftime("%m-%d")
                l = datetime.fromtimestamp(latest).strftime("%m-%d")
                actual_ranges.append(f"    {group_name}: {e} ~ {l} ({len(messages)}条)")

        header = (
            f"{'='*50}\n"
            f"  🔍 关键词搜索（{mode_label}）：{kw_str}\n"
            f"  时间范围：{start_display} ~ {end_display}\n"
            f"  搜索群聊：{group_count} 个群\n"
            f"  命中消息：{total_count} 条\n"
            f"  生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n"
        )
        if actual_ranges:
            header += "  各群命中范围：\n" + "\n".join(actual_ranges) + "\n"
        header += f"{'='*50}\n\n"

        if ai_summary:
            content = header + ai_summary
        else:
            # Raw text mode: display grouped by chat
            parts = []
            for username, messages in results.items():
                group_name = messages[0]["group_name"] if messages else username
                count = len(messages)
                lines = [f"--- 📌 {group_name}（{count}条命中）---\n"]
                for msg in messages:
                    if msg["sender"]:
                        lines.append(f"[{msg['time_str']}] {msg['sender']}: {msg['text']}")
                    else:
                        lines.append(f"[{msg['time_str']}] {msg['text']}")
                parts.append("\n".join(lines))
            content = header + "\n\n".join(parts) + "\n"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return filepath

    # ── Menu bar buttons ─────────────────────────────────────

    @rumps.clicked("刷新群聊列表")
    def refresh_groups(self, _):
        if not self.db:
            _notify("微信总结", "未初始化", "请先确保微信已登录")
            return
        self._set_status(ICON_LOADING)
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        try:
            self._run_on_main(self._rebuild_chat_menu)
            _notify("微信总结", "刷新完成", "群聊列表已更新")
        except Exception as e:
            _notify("微信总结", "刷新失败", str(e))
        finally:
            self._set_status(ICON_NORMAL)

    @rumps.clicked("🔄 刷新数据源")
    def reextract_keys(self, _):
        print("[keys] 点击🔄 刷新数据源")
        if not is_wechat_running():
            print("[keys] ✗ 微信未运行")
            _notify("微信总结", "微信未运行", "请先启动微信并登录")
            return
        if not is_wechat_signed():
            print("[keys] ✗ 微信未签名")
            _notify("微信总结", "微信需要重新授权", _wechat_signing_message())
            return
        print("[keys] 开始刷新数据源...")
        threading.Thread(target=self._do_reextract, daemon=True).start()

    def _do_reextract(self):
        self._set_status(ICON_LOADING)
        _notify("微信总结", "正在刷新数据源", "需要管理员权限...")
        try:
            keys = extract_keys()
            print(f"[keys] extract_keys 返回: {len(keys) if keys else 0} 个密钥")
            if keys:
                self.db = WeChatDB(self.config["db_dir"], keys)
                self._run_on_main(self._rebuild_chat_menu)
                _notify("微信总结", "数据源刷新成功", f"已同步 {len(keys)} 个数据库")
            else:
                _notify("微信总结", "刷新失败", _wechat_signing_message())
        except Exception as e:
            print(f"[keys] ✗ 刷新异常: {e}")
            traceback.print_exc()
            _notify("微信总结", "刷新失败", str(e))
        self._set_status(ICON_NORMAL)


if __name__ == "__main__":
    print("微信总结 启动中...")
    WeChatSummaryApp().run()
