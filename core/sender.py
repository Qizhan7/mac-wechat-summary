"""
WeChat message sending via macOS AppleScript UI automation.

Mechanism: Uses System Events to control the WeChat desktop app.
  1. Activate the WeChat window
  2. Use the search box to find the target chat
  3. Paste the message into the input field and send

Prerequisites:
  - WeChat desktop app is logged in
  - The app running this code (Terminal, Claude.app, etc.) must have
    Accessibility permissions: System Settings -> Privacy & Security -> Accessibility
"""
import subprocess
import time


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

    # Escape special characters
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
                -- 0.1s: local results rendered, web results not yet loaded (optimal)
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

    Assumes WeChat is in the foreground with the target chat selected.
    After select_chat closes the search panel, the input field already
    has focus — so we just paste and press Enter. No coordinate clicking
    needed, which avoids issues with varying window sizes.
    """
    # Escape special characters
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')

    script = f"""
        set the clipboard to "{escaped}"
        tell application "System Events"
            tell process "WeChat"
                set frontmost to true
                delay 0.3

                -- Paste message (input field already focused after search)
                keystroke "v" using command down
                delay 0.3

                -- Press Enter to send
                key code 36
                delay 0.3
            end tell
        end tell
        return "ok"
    """
    return _run_osascript(script, timeout=10)


def send_message(text: str, chat_name: str | None = None) -> tuple[bool, str]:
    """Send a WeChat message.

    Uses a single combined AppleScript call to avoid focus/timing issues
    between separate subprocess invocations.

    Args:
        text: Message text to send.
        chat_name: Target chat name (group or contact).
                   If None, sends to the currently open chat.

    Returns:
        (success, result description)
    """
    if not text.strip():
        return False, "消息内容不能为空"

    escaped_text = text.replace("\\", "\\\\").replace('"', '\\"')

    if chat_name:
        # Combined: activate + search chat + send message in ONE AppleScript
        escaped_name = chat_name.replace("\\", "\\\\").replace('"', '\\"')
        script = f"""
            tell application "WeChat"
                activate
                reopen
            end tell
            delay 0.5

            set the clipboard to "{escaped_name}"
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

                    -- Press Enter before web results load
                    key code 36
                    delay 0.8

                    -- Escape: close search panel, input field gets focus
                    key code 53
                    delay 0.5
                end tell
            end tell

            -- Now paste and send the message
            set the clipboard to "{escaped_text}"
            tell application "System Events"
                tell process "WeChat"
                    keystroke "v" using command down
                    delay 0.3

                    -- Press Enter to send
                    key code 36
                    delay 0.3
                end tell
            end tell
            return "ok"
        """
        ok, msg = _run_osascript(script, timeout=30)
    else:
        # No chat switch needed, just send to current chat
        ok, msg = send_to_current_chat(text)

    if ok:
        target = chat_name or "当前聊天"
        return True, f"✅ 已发送到「{target}」"
    return False, f"发送失败: {msg}"
