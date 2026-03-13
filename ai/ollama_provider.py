"""Ollama local model provider."""
import requests

from .base import AIProvider


class OllamaProvider(AIProvider):
    def __init__(self, model="qwen3:8b", base_url="http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def summarize(self, prompt: str) -> str:
        try:
            response = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"num_ctx": 8192},
                    "think": False,  # Disable thinking mode, output summary directly
                },
                timeout=180,
            )
            response.raise_for_status()
            data = response.json()
            if "message" not in data or "content" not in data.get("message", {}):
                raise RuntimeError(f"Ollama 返回了意外的响应格式")
            return data["message"]["content"]
        except requests.ConnectionError:
            raise RuntimeError(
                "无法连接 Ollama 服务，请确认 Ollama 已启动（运行 ollama serve）"
            )
        except requests.Timeout:
            raise RuntimeError("Ollama 请求超时，模型可能正在加载，请稍后重试")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise RuntimeError(
                    f"Ollama 找不到模型 {self.model}，请运行 ollama pull {self.model}"
                ) from e
            raise RuntimeError(f"Ollama 请求失败: {e}") from e
