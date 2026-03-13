"""
WeChat message sending via macOS UI automation.

Mechanism:
  1. Activate the WeChat window (AppleScript)
  2. If chat_name given: use search box to find the target chat (AppleScript)
  3. Click the input field, paste and send the message (CGEvent)

The send sequence (click input → Cmd+V paste → Enter) uses Quartz CGEvent
exclusively, avoiding additional AppleScript subprocess calls that can steal
focus or lose the input-box cursor.

Prerequisites:
  - WeChat desktop app is logged in
  - The app running this code (Terminal, Claude.app, etc.) must have
    Accessibility permissions: System Settings -> Privacy & Security -> Accessibility
"""

import subprocess
import time

import Quartz


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run_osascript(script: str, timeout: int = 10) -> tuple[bool, str]:
    """Run AppleScript, return (success, output/error)."""
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


def _cg_click(x: float, y: float):
    """Click at screen coordinates (x, y) using CGEvent."""
    point = (x, y)
    down = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.05)
    up = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _cg_key(keycode: int, flags: int = 0):
    """Press and release a key using CGEvent.

    Args:
        keycode: macOS virtual keycode (e.g. 36=Return, 9='v').
        flags:   Modifier flags (e.g. kCGEventFlagMaskCommand for Cmd).

    IMPORTANT: flags are ALWAYS set explicitly (even 0 = no modifiers)
    to prevent inheriting stale modifier state from previous events.
    Without this, a bare Enter after Cmd+V could become Cmd+Enter
    (which inserts a newline in WeChat instead of sending).
    """
    down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    Quartz.CGEventSetFlags(down, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.05)
    up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
    Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


# ---------------------------------------------------------------------------
# WeChat window helpers
# ---------------------------------------------------------------------------

def _get_window_rect() -> tuple[float, float, float, float] | None:
    """Get WeChat window (x, y, width, height) via AppleScript."""
    ok, result = _run_osascript("""
        tell application "System Events"
            tell process "WeChat"
                set winPos to position of window 1
                set winSize to size of window 1
                set wx to (item 1 of winPos) as text
                set wy to (item 2 of winPos) as text
                set ww to (item 1 of winSize) as text
                set wh to (item 2 of winSize) as text
                return wx & "," & wy & "," & ww & "," & wh
            end tell
        end tell
    """)
    if not ok:
        return None
    try:
        parts = [float(v) for v in result.split(",")]
        if len(parts) == 4:
            return (parts[0], parts[1], parts[2], parts[3])
    except (ValueError, IndexError):
        pass
    return None


def _click_input_box() -> bool:
    """Click the WeChat message input box using CGEvent.

    Position is calculated from the WeChat window geometry:
      - Horizontal: 70 % of window width (safely past the ~280-320 px sidebar,
        landing in the middle of the chat panel).
      - Vertical:   55 px from the window bottom (inside the input text area,
        which occupies roughly the bottom 100 px).

    Returns True if the click was performed, False on failure.
    """
    rect = _get_window_rect()
    if not rect:
        return False
    wx, wy, ww, wh = rect
    x = wx + ww * 0.7
    y = wy + wh - 55
    _cg_click(x, y)
    return True


def _paste_and_send(text: str):
    """Set clipboard → Cmd+V → Enter, all via CGEvent / pbcopy.

    No AppleScript subprocess, so WeChat keeps keyboard focus throughout.
    """
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5)
    time.sleep(0.1)

    # Cmd+V  (keycode 9 = 'v')
    _cg_key(9, Quartz.kCGEventFlagMaskCommand)
    time.sleep(0.5)

    # Return  (keycode 36) — flags=0 explicitly clears Cmd modifier
    _cg_key(36, 0)
    time.sleep(0.3)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def activate_wechat() -> tuple[bool, str]:
    """Bring WeChat window to foreground."""
    return _run_osascript("""
        tell application "WeChat"
            activate
            reopen
        end tell
        delay 0.5
    """)


def select_chat(chat_name: str) -> tuple[bool, str]:
    """Switch to a specific chat via the search box.

    Opens search with Cmd+F, types the name, presses Enter quickly
    (before web search results load, so the contact stays at the top),
    then presses Escape to close the search panel.
    """
    ok, msg = activate_wechat()
    if not ok:
        return False, f"无法激活微信: {msg}"

    escaped = chat_name.replace("\\", "\\\\").replace('"', '\\"')

    script = f"""
        set the clipboard to "{escaped}"
        tell application "System Events"
            tell process "WeChat"
                set frontmost to true
                delay 0.3

                -- Cmd+F: open search
                keystroke "f" using command down
                delay 0.5

                -- Clear search and paste chat name
                keystroke "a" using command down
                delay 0.1
                keystroke "v" using command down
                delay 0.1

                -- Press Enter immediately before web results load
                key code 36
                delay 0.8

                -- Escape: close search panel
                key code 53
                delay 0.5
            end tell
        end tell
        return "ok"
    """
    return _run_osascript(script, timeout=20)


def send_to_current_chat(text: str) -> tuple[bool, str]:
    """Send a message to the currently open chat.

    Activates WeChat, clicks the input box, pastes and sends.
    Works regardless of whether the input field already has focus.
    """
    ok, msg = activate_wechat()
    if not ok:
        return False, f"无法激活微信: {msg}"

    if not _click_input_box():
        return False, "无法定位微信输入框"
    time.sleep(0.3)

    _paste_and_send(text)
    return True, "ok"


def send_message(text: str, chat_name: str | None = None) -> tuple[bool, str]:
    """Send a WeChat message.

    Strategy:
      - AppleScript is used **only** for activating WeChat and navigating
        to the target chat (search → Enter → Escape).
      - Everything after that (click input box → paste → Enter) uses CGEvent,
        which runs in-process and cannot lose window focus.

    Args:
        text:      Message text to send.
        chat_name: Target chat name (group or contact).
                   If None, sends to the currently open chat.

    Returns:
        (success, result description)
    """
    if not text.strip():
        return False, "消息内容不能为空"

    if chat_name:
        # Step 1: AppleScript — activate + search + select chat + Escape
        ok, msg = select_chat(chat_name)
        if not ok:
            return False, f"选择聊天失败: {msg}"

        # Step 2: CGEvent — click input box (Escape 后焦点不确定)
        if not _click_input_box():
            return False, "无法定位微信输入框"
        time.sleep(0.3)

        # Step 3: CGEvent — paste + Enter (in-process, no focus loss)
        _paste_and_send(text)
        ok = True
    else:
        # Already in target chat — activate, click, paste+send
        ok, msg = send_to_current_chat(text)

    if ok:
        target = chat_name or "当前聊天"
        return True, f"✅ 已发送到「{target}」"
    return False, f"发送失败: {msg}"
