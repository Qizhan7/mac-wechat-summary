"""OpenAI 兼容 API 提供者（支持 OpenAI、DeepSeek、通义千问等兼容接口）"""
from .base import AIProvider


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
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
        )
        result = response.choices[0].message.content
        print(f"[ai] 返回 {len(result)} 字符")
        return result
