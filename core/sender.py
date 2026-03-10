"""
微信消息发送 - 通过 macOS AppleScript UI 自动化

原理：通过 System Events 控制微信桌面端界面
  1. 激活微信窗口
  2. 用搜索框找到目标聊天
  3. 在输入框粘贴消息并发送

前提条件：
  - 微信桌面版已登录
  - 运行此代码的 app（Terminal、Claude.app 等）需要辅助功能权限
    系统设置 → 隐私与安全性 → 辅助功能
"""
import subprocess
import time


def _run_osascript(script: str, timeout: int = 10) -> tuple[bool, str]:
    """执行 AppleScript，返回 (是否成功, 输出/错误信息)"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "AppleScript 执行超时"
    except Exception as e:
        return False, str(e)


def activate_wechat() -> tuple[bool, str]:
    """将微信窗口置于前台"""
    return _run_osascript("""
        tell application "WeChat"
            activate
            reopen
        end tell
        delay 0.5
    """)


def select_chat(chat_name: str) -> tuple[bool, str]:
    """通过搜索框切换到指定聊天

    使用 Cmd+F 打开搜索，输入名称，快速回车（在网络搜索结果加载前
    抢先选中联系人），然后 Escape 关闭搜索面板。
    """
    ok, msg = activate_wechat()
    if not ok:
        return False, f"无法激活微信: {msg}"

    # 转义特殊字符
    escaped = chat_name.replace("\\", "\\\\").replace('"', '\\"')

    script = f"""
        set the clipboard to "{escaped}"
        tell application "System Events"
            tell process "WeChat"
                set frontmost to true
                delay 0.3

                -- Cmd+F 打开搜索框
                keystroke "f" using command down
                delay 0.5

                -- 清空搜索框并粘贴聊天名
                keystroke "a" using command down
                delay 0.1
                keystroke "v" using command down
                delay 0.1

                -- 立刻回车！在网络搜索结果加载之前，联系人还在第一位
                -- 0.1s：本地结果已渲染，网络结果还没加载（实测最佳值）
                key code 36
                delay 0.8

                -- Escape 关闭搜索面板
                key code 53
                delay 0.5
            end tell
        end tell
        return "ok"
    """
    return _run_osascript(script, timeout=20)


def send_to_current_chat(text: str) -> tuple[bool, str]:
    """向当前打开的聊天发送消息

    假设微信已在前台且已选中目标聊天。
    点击右侧聊天面板的输入框区域，粘贴消息并发送。
    """
    # 转义特殊字符
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')

    script = f"""
        set the clipboard to "{escaped}"
        tell application "System Events"
            tell process "WeChat"
                set frontmost to true
                delay 0.3

                -- 获取窗口位置和尺寸
                set winPos to position of window 1
                set winSize to size of window 1

                -- 点击右侧聊天面板的输入框（70% 宽度处，底部上方 50px）
                -- 微信左侧是会话列表/搜索面板，右侧是聊天区域
                set clickX to (item 1 of winPos) + (item 1 of winSize) * 0.7
                set clickY to (item 2 of winPos) + (item 2 of winSize) - 50

                click at {{clickX, clickY}}
                delay 0.3

                -- 粘贴消息
                keystroke "v" using command down
                delay 0.3

                -- 回车发送
                key code 36
                delay 0.3
            end tell
        end tell
        return "ok"
    """
    return _run_osascript(script, timeout=10)


def send_message(text: str, chat_name: str | None = None) -> tuple[bool, str]:
    """发送微信消息

    Args:
        text: 要发送的消息文本
        chat_name: 目标聊天名称（群名或联系人名）。
                   如果为 None，发送到当前打开的聊天。

    Returns:
        (是否成功, 结果说明)
    """
    if not text.strip():
        return False, "消息内容不能为空"

    # 如果指定了聊天名，先切换
    if chat_name:
        ok, msg = select_chat(chat_name)
        if not ok:
            return False, f"无法切换到聊天「{chat_name}」: {msg}"
        time.sleep(0.3)

    # 发送消息
    ok, msg = send_to_current_chat(text)
    if ok:
        target = chat_name or "当前聊天"
        return True, f"✅ 已发送到「{target}」"
    return False, f"发送失败: {msg}"
