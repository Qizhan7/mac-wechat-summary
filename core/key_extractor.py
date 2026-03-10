"""
密钥提取 - 编译并运行 C 扫描器从微信进程内存提取数据库密钥
"""
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
    """获取可用于扫描密钥的微信主进程 PID"""
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
    """检查微信是否在运行"""
    return get_wechat_pid() is not None


def get_wechat_app_path():
    """获取 WeChat.app 路径，优先系统已安装的位置"""
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
    """检查微信是否已重签名（去掉 hardened runtime）"""
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
        # hardened runtime 会在 flags 中出现 "runtime"
        return "runtime" not in flags.lower()
    except Exception:
        return False


def compile_scanner():
    """编译 C 密钥扫描器"""
    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(C_BINARY):
        # 检查是否需要重新编译
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
    """运行 C 扫描器提取密钥，需要 sudo 权限

    Returns:
        dict: {db_rel_path: {"enc_key": hex_string}, ...} 或 None
    """
    if not compile_scanner():
        return None

    pid = get_wechat_pid()
    if not pid:
        return None
    home_dir = os.path.expanduser("~")
    db_dir = load_config().get("db_dir", "")

    # C 扫描器输出到当前目录的 all_keys.json，我们 cd 到 DATA_DIR
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
            # 尝试带交互的 sudo（通过 osascript 弹窗）
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

    # 读取输出的 keys 文件
    keys_path = os.path.join(DATA_DIR, "all_keys.json")
    if not os.path.exists(keys_path):
        return None

    try:
        with open(keys_path) as f:
            keys = json.load(f)
        # 过滤掉元数据字段
        keys = {k: v for k, v in keys.items() if not k.startswith("_")}
    except (json.JSONDecodeError, OSError):
        return None

    # ── 如果 C 扫描器因权限问题没能匹配到 DB，在 Python 端重新匹配 ──
    # Python 以当前用户身份运行，可以正常读取 sandbox 文件
    if not keys and db_dir and os.path.isdir(db_dir):
        keys = _rematch_keys_from_log(db_dir)

    return keys if keys else None


def _parse_raw_keys_from_log(log_path=EXTRACT_LOG):
    """从 extract_keys.log 解析 C 扫描器找到的所有 key+salt 对"""
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
    """用 Python（用户权限）读取 db 文件头，与扫描器日志中的 key+salt 匹配

    解决 root 无法读取 macOS sandbox 文件的问题。
    """
    raw_keys = _parse_raw_keys_from_log()
    if not raw_keys:
        return {}

    print(f"[key_extractor] 从日志解析到 {len(raw_keys)} 个 key+salt 对，用 Python 重新匹配...")

    # 建立 salt → key_hex 索引
    salt_to_key = {}
    for key_hex, salt_hex in raw_keys:
        salt_to_key[salt_hex] = key_hex

    # 遍历 db_dir 下所有 .db 文件，读取 salt
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
                # 未加密 SQLite，跳过
                if header[:15] == b"SQLite format 3":
                    continue
                file_salt = header.hex().lower()
                if file_salt in salt_to_key:
                    matched[rel] = {"enc_key": salt_to_key[file_salt]}
                    print(f"  ✓ 匹配: {rel}")
            except OSError:
                continue

    if matched:
        # 保存到 all_keys.json
        try:
            with open(KEYS_FILE, "w") as f:
                json.dump(matched, f, indent=2)
            print(f"[key_extractor] Python 重新匹配成功: {len(matched)} 个数据库")
        except OSError:
            pass

    return matched


def get_cached_keys():
    """获取缓存的密钥（不重新提取）"""
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
    """检测 db_dir 下是否有新的加密数据库缺少密钥

    扫描 db_storage 目录所有 .db 文件，读头 16 字节判断是否加密，
    与已有 keys 对比，返回缺少密钥的列表。

    Args:
        db_dir: db_storage 目录路径
        keys: 当前已有的密钥字典 {rel_path: {"enc_key": ...}}

    Returns:
        list[str]: 缺少密钥的数据库相对路径
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
                continue  # 已有密钥
            # 读头 16 字节判断是否加密
            try:
                with open(full, "rb") as f:
                    header = f.read(16)
                if len(header) < 16 or header[:15] == b"SQLite format 3":
                    continue  # 太小或未加密，跳过
                missing.append(rel)
            except OSError:
                continue
    return sorted(missing)
