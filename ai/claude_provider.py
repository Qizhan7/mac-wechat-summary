"""Claude API 提供者"""
from .base import AIProvider


class ClaudeProvider(AIProvider):
    def __init__(self, api_key, model="claude-sonnet-4-20250514"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
        self.model = model

    def summarize(self, prompt: str) -> str:
        print(f"[ai] 调用 {self.model}, prompt 长度: {len(prompt)} 字符...")
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        print(f"[ai] 返回 {len(result)} 字符")
        return result
