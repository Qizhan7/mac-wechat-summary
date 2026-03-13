"""macOS Keychain storage - securely store API keys in system keychain."""
import subprocess
from typing import Optional

SERVICE_NAME = "wechat-summary"


def save_key(account: str, password: str) -> bool:
    """Save a key to macOS Keychain.

    Args:
        account: Account identifier (e.g. "ai-api-key")
        password: Key/password to store
    """
    try:
        # -U: update if already exists
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
    """Load a key from macOS Keychain."""
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
    """Delete a key from Keychain."""
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
