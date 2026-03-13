"""Key extraction - compile and run C scanner to extract DB keys from WeChat process memory."""
import json
import os
import shlex
import subprocess
import sys

from .config import APP_DIR, DATA_DIR, load_config


C_SOURCE = os.path.join(APP_DIR, "c_src", "find_keys_macos.c")
C_BINARY = os.path.join(DATA_DIR, "find_keys_macos")
KEYS_FILE = os.path.join(DATA_DIR, "all_keys.json")
EXTRACT_LOG = os.path.join(DATA_DIR, "extract_keys.log")
DEFAULT_WECHAT_APP = "/Applications/WeChat.app"
WECHAT_PROCESS_NAMES = ("WeChat", "WeChatAppEx", "微信")
WECHAT_PROCESS_PATTERNS = (
    r"/WeChat\.app/Contents/MacOS/WeChat($| )",
    r"/WeChatAppEx\.app/Contents/MacOS/WeChatAppEx($| )",
)


def _first_pid(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None
    except Exception:
        return None


def get_wechat_pid():
    """Get WeChat main process PID for key scanning."""
    for name in WECHAT_PROCESS_NAMES:
        pid = _first_pid(["pgrep", "-x", name])
        if pid:
            return pid

    for pattern in WECHAT_PROCESS_PATTERNS:
        pid = _first_pid(["pgrep", "-f", pattern])
        if pid:
            return pid

    return None


def is_wechat_running():
    """Check if WeChat is running."""
    return get_wechat_pid() is not None


def get_wechat_app_path():
    """Get WeChat.app path, preferring system-installed location."""
    try:
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (path to application "WeChat")'],
            capture_output=True,
            text=True,
        )
        path = result.stdout.strip()
        if result.returncode == 0 and path and os.path.isdir(path):
            return path.rstrip("/")
    except Exception:
        pass

    if os.path.isdir(DEFAULT_WECHAT_APP):
        return DEFAULT_WECHAT_APP
    return None


def is_wechat_signed():
    """Check if WeChat has been re-signed (hardened runtime removed)."""
    app_path = get_wechat_app_path()
    if not app_path:
        return False

    try:
        result2 = subprocess.run(
            ["codesign", "-dvv", app_path],
            capture_output=True, text=True,
        )
        if result2.returncode != 0:
            return False
        flags = result2.stderr
        # Hardened runtime shows "runtime" in flags
        return "runtime" not in flags.lower()
    except Exception:
        return False


def compile_scanner():
    """Compile C key scanner."""
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(C_BINARY):
        # Check if recompilation needed
        if os.path.getmtime(C_BINARY) >= os.path.getmtime(C_SOURCE):
            return True

    try:
        result = subprocess.run(
            ["cc", "-O2", "-o", C_BINARY, C_SOURCE, "-framework", "Foundation"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"编译失败: {result.stderr}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"编译失败: {e}", file=sys.stderr)
        return False


def extract_keys():
    """Run C scanner to extract keys, requires sudo.

    Returns:
        dict: {db_rel_path: {"enc_key": hex_string}, ...} or None.
    """
    if not compile_scanner():
        return None

    pid = get_wechat_pid()
    if not pid:
        return None
    home_dir = os.path.expanduser("~")
    db_dir = load_config().get("db_dir", "")

    # C scanner outputs all_keys.json to cwd, so cd to DATA_DIR
    try:
        result = subprocess.run(
            ["sudo", "-n", C_BINARY, str(pid), home_dir, db_dir],
            capture_output=True, text=True,
            cwd=DATA_DIR,
            timeout=60,
        )
        with open(EXTRACT_LOG, "w", encoding="utf-8") as f:
            f.write("=== sudo -n stdout ===\n")
            f.write(result.stdout)
            f.write("\n=== sudo -n stderr ===\n")
            f.write(result.stderr)

        if result.returncode != 0:
            # Try interactive sudo via osascript dialog
            shell_command = (
                f"cd {shlex.quote(DATA_DIR)} && "
                f"{shlex.quote(C_BINARY)} {pid} {shlex.quote(home_dir)} {shlex.quote(db_dir)}"
            )
            result = subprocess.run(
                ["osascript", "-e",
                 f"do shell script {json.dumps(shell_command)} with administrator privileges"],
                capture_output=True, text=True,
                timeout=60,
            )
            with open(EXTRACT_LOG, "a", encoding="utf-8") as f:
                f.write("\n=== osascript stdout ===\n")
                f.write(result.stdout)
                f.write("\n=== osascript stderr ===\n")
                f.write(result.stderr)
            if result.returncode != 0:
                return None

    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

    # Read output keys file
    keys_path = os.path.join(DATA_DIR, "all_keys.json")
    if not os.path.exists(keys_path):
        return None

    try:
        with open(keys_path) as f:
            keys = json.load(f)
        # Filter out metadata fields
        keys = {k: v for k, v in keys.items() if not k.startswith("_")}
    except (json.JSONDecodeError, OSError):
        return None

    # If C scanner couldn't match DBs due to permission issues, re-match in Python
    # Python runs as current user and can read sandbox files
    if not keys and db_dir and os.path.isdir(db_dir):
        keys = _rematch_keys_from_log(db_dir)

    return keys if keys else None


def _parse_raw_keys_from_log(log_path=EXTRACT_LOG):
    """Parse all key+salt pairs found by C scanner from extract_keys.log."""
    raw_keys = []  # [(key_hex, salt_hex), ...]
    if not os.path.exists(log_path):
        return raw_keys
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                # 格式: "(unknown)  <key_hex 64>  <salt_hex 32>"
                # 或:   "db_name   <key_hex 64>  <salt_hex 32>"
                parts = line.split()
                if len(parts) < 3:
                    continue
                key_hex = parts[-2]
                salt_hex = parts[-1]
                if len(key_hex) == 64 and len(salt_hex) == 32:
                    try:
                        bytes.fromhex(key_hex)
                        bytes.fromhex(salt_hex)
                        raw_keys.append((key_hex.lower(), salt_hex.lower()))
                    except ValueError:
                        continue
    except OSError:
        pass
    return raw_keys


def _rematch_keys_from_log(db_dir):
    """Read db file headers with Python (user privileges) and match against key+salt from scanner log.

    Solves the issue where root cannot read macOS sandbox files.
    """
    raw_keys = _parse_raw_keys_from_log()
    if not raw_keys:
        return {}

    print(f"[key_extractor] 从日志解析到 {len(raw_keys)} 个 key+salt 对，用 Python 重新匹配...")

    # Build salt -> key_hex index
    salt_to_key = {}
    for key_hex, salt_hex in raw_keys:
        salt_to_key[salt_hex] = key_hex

    # Walk all .db files under db_dir, read salt
    matched = {}
    for root, _dirs, files in os.walk(db_dir):
        for fname in files:
            if not fname.endswith(".db"):
                continue
            full_path = os.path.join(root, fname)
            rel = os.path.relpath(full_path, db_dir).replace("\\", "/")
            try:
                with open(full_path, "rb") as f:
                    header = f.read(16)
                if len(header) < 16:
                    continue
                # Unencrypted SQLite, skip
                if header[:15] == b"SQLite format 3":
                    continue
                file_salt = header.hex().lower()
                if file_salt in salt_to_key:
                    matched[rel] = {"enc_key": salt_to_key[file_salt]}
                    print(f"  ✓ 匹配: {rel}")
            except OSError:
                continue

    if matched:
        # Save to all_keys.json
        try:
            with open(KEYS_FILE, "w") as f:
                json.dump(matched, f, indent=2)
            print(f"[key_extractor] Python 重新匹配成功: {len(matched)} 个数据库")
        except OSError:
            pass

    return matched


def get_cached_keys():
    """Get cached keys (without re-extraction)."""
    if not os.path.exists(KEYS_FILE):
        return None
    try:
        with open(KEYS_FILE) as f:
            keys = json.load(f)
        keys = {k: v for k, v in keys.items() if not k.startswith("_")}
        return keys if keys else None
    except (json.JSONDecodeError, OSError):
        return None


def check_new_databases(db_dir, keys):
    """Detect new encrypted databases under db_dir that are missing keys.

    Scans db_storage directory for all .db files, reads first 16 bytes to
    check if encrypted, compares against existing keys, returns list of
    databases missing keys.

    Args:
        db_dir: db_storage directory path.
        keys: Current key dict {rel_path: {"enc_key": ...}}.

    Returns:
        list[str]: Relative paths of databases missing keys.
    """
    missing = []
    normalized_keys = {k.replace("\\", "/") for k in keys}
    for root, _dirs, files in os.walk(db_dir):
        for fname in files:
            if not fname.endswith(".db"):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, db_dir).replace("\\", "/")
            if rel in normalized_keys:
                continue  # Already has key
            # Read first 16 bytes to check if encrypted
            try:
                with open(full, "rb") as f:
                    header = f.read(16)
                if len(header) < 16 or header[:15] == b"SQLite format 3":
                    continue  # Too small or unencrypted, skip
                missing.append(rel)
            except OSError:
                continue
    return sorted(missing)
