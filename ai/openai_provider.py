"""OpenAI-compatible API provider (supports OpenAI, DeepSeek, Qwen, etc.)."""
from .base import AIProvider
from core.api_errors import normalize_ai_error


class OpenAIProvider(AIProvider):
    def __init__(self, api_key, model="gpt-4o-mini", base_url=None):
        from openai import OpenAI
        kwargs = {"api_key": api_key, "timeout": 120.0}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model

    def summarize(self, prompt: str) -> str:
        print(f"[ai] 调用 {self.model}, prompt 长度: {len(prompt)} 字符...")
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
            )
            result = response.choices[0].message.content
            print(f"[ai] 返回 {len(result)} 字符")
            return result
        except Exception as e:
            raise RuntimeError(normalize_ai_error(e, "AI")) from None
