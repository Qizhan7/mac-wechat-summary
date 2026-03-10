"""Ollama 本地模型提供者"""
import requests

from .base import AIProvider


class OllamaProvider(AIProvider):
    def __init__(self, model="qwen3:8b", base_url="http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def summarize(self, prompt: str) -> str:
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_ctx": 8192},
                "think": False,  # 关闭 thinking 模式，直接输出总结
            },
            timeout=180,
        )
        response.raise_for_status()
        return response.json()["message"]["content"]
