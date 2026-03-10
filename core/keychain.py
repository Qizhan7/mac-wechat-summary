"""
macOS Keychain 安全存储 - 用系统钥匙串保存 API Key
"""
import subprocess
from typing import Optional

SERVICE_NAME = "wechat-summary"


def save_key(account: str, password: str) -> bool:
    """保存密钥到 macOS 钥匙串

    Args:
        account: 账户标识 (如 "ai-api-key")
        password: 要保存的密钥
    """
    try:
        # -U: 如果已存在则更新
        subprocess.run(
            [
                "security", "add-generic-password",
                "-a", account,
                "-s", SERVICE_NAME,
                "-w", password,
                "-U",
            ],
            capture_output=True, text=True, check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def load_key(account: str) -> Optional[str]:
    """从 macOS 钥匙串读取密钥"""
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-a", account,
                "-s", SERVICE_NAME,
                "-w",
            ],
            capture_output=True, text=True, check=True,
        )
        key = result.stdout.strip()
        return key if key else None
    except subprocess.CalledProcessError:
        return None


def delete_key(account: str) -> bool:
    """从钥匙串删除密钥"""
    try:
        subprocess.run(
            [
                "security", "delete-generic-password",
                "-a", account,
                "-s", SERVICE_NAME,
            ],
            capture_output=True, text=True, check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
