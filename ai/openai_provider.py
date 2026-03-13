"""OpenAI-compatible API provider (supports OpenAI, DeepSeek, Qwen, etc.)."""
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
            err = str(e)
            if "401" in err or "auth" in err.lower() or "invalid" in err.lower() and "key" in err.lower():
                raise RuntimeError("API Key 无效或已过期，请在设置中重新配置") from e
            if "429" in err or "rate" in err.lower():
                raise RuntimeError("API 请求频率超限，请稍后再试") from e
            if "timeout" in err.lower() or "timed out" in err.lower():
                raise RuntimeError("API 请求超时，请检查网络连接后重试") from e
            if "connect" in err.lower():
                raise RuntimeError("无法连接 API 服务器，请检查网络连接") from e
            raise RuntimeError(f"AI 调用失败: {err}") from e
