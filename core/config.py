"""Configuration management - app config and WeChat data path detection."""
import json
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.expanduser("~/.wechat-summary")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
LEGACY_CONFIG_FILE = os.path.join(APP_DIR, "config.json")

DEFAULT_CONFIG = {
    "db_dir": "",
    "keys_file": os.path.join(DATA_DIR, "all_keys.json"),
    "decrypted_dir": os.path.join(DATA_DIR, "decrypted"),
    "ai_provider": "qwen",  # Options: qwen, ollama, deepseek, claude, openai, custom
    "ai_model": "",          # Empty uses default model; API key stored in macOS Keychain
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen3:8b",
    "auto_refresh_on_open": False,
    "ai_base_url": "",
    "show_group_nickname": True,
    "batch_msg_limit": 100,
    "hide_inactive_months": 1,
    "monitor_enabled": False,
    "monitor_interval_minutes": 3,
    "monitor_chat_username": "45716626449@chatroom",
    "monitor_chat_display_name": "Claude恋爱技术群",
    "monitor_topic": "",
    "monitor_max_messages_per_run": 200,
    "monitor_cooldown_minutes": 15,
    "monitor_ai_provider": "deepseek",
    "monitor_ai_model": "deepseek-v4-flash",
    "monitor_knowledge_enabled": True,
    "monitor_knowledge_db": os.path.join(DATA_DIR, "monitor_knowledge.db"),
    "monitor_obsidian_root": os.path.join(DATA_DIR, "obsidian_knowledge"),
}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _sanitize_config(saved):
    cfg = dict(DEFAULT_CONFIG)
    if not isinstance(saved, dict):
        return cfg

    for key in (
        "ai_provider", "ai_model", "ollama_url", "ollama_model",
        "ai_base_url", "monitor_chat_username", "monitor_chat_display_name",
        "monitor_topic", "monitor_ai_provider", "monitor_ai_model",
        "monitor_knowledge_db", "monitor_obsidian_root",
    ):
        value = saved.get(key)
        if isinstance(value, str):
            cfg[key] = value

    for key in (
        "auto_refresh_on_open", "show_group_nickname", "monitor_enabled",
        "monitor_knowledge_enabled",
    ):
        value = saved.get(key)
        if isinstance(value, bool):
            cfg[key] = value

    int_ranges = {
        "batch_msg_limit": (1, 5000),
        "hide_inactive_months": (0, 60),
        "monitor_interval_minutes": (1, 1440),
        "monitor_max_messages_per_run": (1, 1000),
        "monitor_cooldown_minutes": (0, 1440),
    }
    for key, (min_value, max_value) in int_ranges.items():
        value = saved.get(key)
        if isinstance(value, int) and min_value <= value <= max_value:
            cfg[key] = value

    db_dir = saved.get("db_dir")
    if isinstance(db_dir, str):
        cfg["db_dir"] = db_dir

    keys_file = saved.get("keys_file")
    if isinstance(keys_file, str) and keys_file:
        cfg["keys_file"] = keys_file

    decrypted_dir = saved.get("decrypted_dir")
    if isinstance(decrypted_dir, str) and decrypted_dir:
        cfg["decrypted_dir"] = decrypted_dir

    return cfg


def _load_saved_config():
    saved = _read_json(CONFIG_FILE)
    if saved is not None:
        return _sanitize_config(saved)

    legacy = _read_json(LEGACY_CONFIG_FILE)
    if legacy is None:
        return dict(DEFAULT_CONFIG)

    cfg = _sanitize_config(legacy)
    save_config(cfg)
    return cfg


def auto_detect_db_dir():
    """Auto-detect macOS WeChat database path."""
    bases = [
        os.path.expanduser(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
        ),
        os.path.expanduser(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Documents"
        ),
        os.path.expanduser(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support"
        ),
    ]

    candidates = []
    seen = set()
    for base in bases:
        if not os.path.isdir(base):
            continue

        for root, dirs, _files in os.walk(base):
            for dirname in dirs:
                if dirname != "db_storage":
                    continue
                storage = os.path.join(root, dirname)
                if storage in seen:
                    continue
                seen.add(storage)
                candidates.append(storage)

    if not candidates:
        return None

    preferred = []
    for path in candidates:
        score = 0
        if "/xwechat_files/" in path.replace("\\", "/"):
            score += 2
        if os.path.isfile(os.path.join(path, "contact", "contact.db")):
            score += 2
        if os.path.isfile(os.path.join(path, "session", "session.db")):
            score += 2
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        preferred.append((score, mtime, path))

    preferred.sort(reverse=True)
    return preferred[0][2]


def load_config():
    """Load config, auto-detect on first run."""
    os.makedirs(DATA_DIR, exist_ok=True)

    cfg = _load_saved_config()

    # Auto-detect db_dir
    if not cfg["db_dir"] or not os.path.isdir(cfg["db_dir"]):
        detected = auto_detect_db_dir()
        if detected:
            cfg["db_dir"] = detected
            save_config(cfg)

    return cfg


def save_config(cfg):
    """Save config."""
    os.makedirs(DATA_DIR, exist_ok=True)
    normalized = _sanitize_config(cfg)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=4, ensure_ascii=False)
