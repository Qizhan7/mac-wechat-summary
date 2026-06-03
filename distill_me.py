#!/usr/bin/env python3
"""
蒸馏「我」的说话风格 —— 参考 immortal-skill 的四维提取框架。

从 conversations.txt（微信提取）和/或 QQ 导出的 txt 文件中，
分批让 AI 提取互动风格 + 性格特征，最后组装成可用的 system prompt。

用法：
    python distill_me.py                          # 用默认微信提取数据
    python distill_me.py --qq ~/qq_export.txt     # 额外加入 QQ 聊天记录
    python distill_me.py --model qwen-plus        # 指定模型（默认 qwen-plus）
    python distill_me.py --name 盏                # 指定名字
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import load_config
from core.keychain import load_key

OUTPUT_DIR = os.path.expanduser("~/.wechat-summary/distilled")
EXTRACTED_DIR = os.path.expanduser("~/.wechat-summary/extracted")

# ── 提取 Prompt（参考 immortal-skill） ──

INTERACTION_PROMPT = """你是一个专业的人格分析师。请从以下聊天记录中提取「{name}」的**互动风格**。

## 提取维度

### 1. 默认沟通方式
- 消息长度偏好（长文/短句/表情）
- 回复速度习惯
- 常用的句式结构

### 2. 口头禅与表达习惯
- 高频词汇、语气词（哈哈、嗯嗯、好的、啊啊啊 等）
- 特有的表达方式、比喻、缩写
- 标点符号使用习惯（省略号、感叹号、波浪号等）
- 表情/emoji 使用偏好

### 3. 回应模式
- 对不同话题的参与度（什么话题会接、什么会忽略）
- 赞同/反对时的表达方式
- 提问的方式和频率

### 4. 情绪表达
- 开心/兴奋时的说话方式
- 无聊/累了时的说话方式
- 不高兴/生气时的说话方式

### 5. 社交风格
- 在群里的角色（活跃/潜水/吐槽手/捧哏等）
- 对不同人的说话方式差异
- 幽默风格（冷笑话/自嘲/吐槽/谐音梗等）

## 规则
- 只基于聊天记录中的**实际证据**，不要猜测
- 尽量引用原话作为证据
- 材料不足的维度写「材料不足」
- 「{name}」在记录中标记为【我】

## 聊天记录
{messages}"""

PERSONALITY_PROMPT = """你是一个专业的人格分析师。请从以下聊天记录中提取「{name}」的**性格与价值观特征**。

## 提取维度

### 1. 核心价值观
- 在对话中反复强调或体现的信念
- 面对选择时什么因素排在第一位
- 明确表达过在乎什么、反感什么

### 2. 情绪模式
- 什么场景触发积极情绪？表现是什么？
- 什么场景触发消极情绪？表现是什么？
- 压力下的行为变化

### 3. 兴趣与关注点
- 主动聊起的话题
- 投入最多精力讨论的领域

### 4. 决策风格
- 做决定的方式（果断/纠结/随缘/问别人）
- 给建议时的倾向

### 5. 社交偏好
- 对社交频率和深度的偏好
- 在群体中的角色定位

## 规则
- 基于**行为证据**而非心理学标签
- 允许并记录矛盾面——人本身就是复杂的
- 引用原话作为证据
- 材料不足的维度写「材料不足」
- 「{name}」在记录中标记为【我】

## 聊天记录
{messages}"""

ASSEMBLER_PROMPT = """你是一个 system prompt 工程师。根据以下提取出的互动风格和性格分析，
生成一个可以让 AI 模仿「{name}」在微信群里说话的 system prompt。

## 互动风格分析结果
{interaction_results}

## 性格与价值观分析结果
{personality_results}

## 要求
1. 生成的 prompt 要让 AI 能在群聊中自然地模仿 {name} 的说话方式
2. 包含具体的语言特征（口头禅、句式、标点习惯、表情使用等）
3. 包含回应规则（什么话题要接、什么要忽略、什么时候长回复什么时候短回复）
4. 包含性格设定（但要自然，不要像在念说明书）
5. prompt 要简洁实用，不超过 800 字
6. 不要包含任何伦理声明或免责声明
7. 用第二人称写（"你是..."）
8. 最后附上 10 个典型回复示例，涵盖不同场景

## 直接输出 system prompt，不要加额外说明。"""


def create_client(config, model):
    """创建 OpenAI 兼容客户端。"""
    from openai import OpenAI

    api_key = load_key("ai-api-key") or config.get("ai_api_key", "")
    if not api_key:
        print("❌ 未找到 API Key，请先在微信总结 app 中配置")
        sys.exit(1)

    provider = config.get("ai_provider", "qwen")
    base_urls = {
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "deepseek": "https://api.deepseek.com",
    }
    base_url = base_urls.get(provider)
    if provider == "custom":
        base_url = config.get("ai_base_url", "")

    kwargs = {"api_key": api_key, "timeout": 600.0}
    if base_url:
        kwargs["base_url"] = base_url

    return OpenAI(**kwargs), model


def call_ai(client, model, prompt, retries=2):
    """调用 AI，带重试。"""
    for attempt in range(retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
            )
            return response.choices[0].message.content
        except Exception as e:
            err_str = str(e)
            # 内容审核拦截，跳过这批
            if "data_inspection_failed" in err_str or "inappropriate" in err_str:
                print(f"⚠️  内容审核拦截，跳过此批")
                return None
            if attempt < retries and ("429" in err_str or "timeout" in err_str.lower()):
                wait = 10 * (attempt + 1)
                print(f"  ⏳ 请求失败，{wait}s 后重试... ({e})")
                time.sleep(wait)
            else:
                raise


def load_conversations(wechat_path, qq_paths=None):
    """加载所有聊天记录，返回文本。"""
    texts = []
    if wechat_path and os.path.exists(wechat_path):
        with open(wechat_path, encoding="utf-8") as f:
            texts.append(f.read())
        print(f"📄 加载微信记录: {wechat_path} ({len(texts[-1])} 字符)")

    for qq in (qq_paths or []):
        if os.path.exists(qq):
            with open(qq, encoding="utf-8") as f:
                content = f.read()
            texts.append(f"\n=== QQ聊天记录 ===\n{content}")
            print(f"📄 加载QQ记录: {qq} ({len(content)} 字符)")
        else:
            print(f"⚠️  QQ文件不存在: {qq}")

    return "\n\n".join(texts)


def split_into_batches(text, max_chars=12000):
    """按段落分批，每批不超过 max_chars。"""
    paragraphs = text.split("\n")
    batches = []
    current = []
    current_len = 0

    for line in paragraphs:
        line_len = len(line) + 1
        if current_len + line_len > max_chars and current:
            batches.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        batches.append("\n".join(current))

    return batches


def main():
    parser = argparse.ArgumentParser(description="蒸馏我的说话风格")
    parser.add_argument("--name", default="我", help="你的名字（默认: 我）")
    parser.add_argument("--qq", action="append", help="QQ 导出的 txt 文件路径（可多个）")
    parser.add_argument("--input", help="自定义输入文件（替代默认的 conversations.txt）")
    parser.add_argument("--model", default="qwen-plus", help="模型（默认 qwen-plus）")
    parser.add_argument("--batch-size", type=int, default=12000, help="每批最大字符数")
    args = parser.parse_args()

    config = load_config()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 加载数据
    wechat_file = args.input or os.path.join(EXTRACTED_DIR, "conversations.txt")
    all_text = load_conversations(wechat_file, args.qq)
    if not all_text.strip():
        print("❌ 没有找到任何聊天数据")
        return

    # 分批
    batches = split_into_batches(all_text, args.batch_size)
    print(f"\n📊 总数据: {len(all_text)} 字符，分为 {len(batches)} 批\n")

    # 创建客户端
    client, model = create_client(config, args.model)
    print(f"🤖 使用模型: {model}\n")

    # ── 第一轮：互动风格提取 ──
    print("=" * 50)
    print("📝 第一轮：提取互动风格")
    print("=" * 50)

    interaction_results = []
    for i, batch in enumerate(batches):
        cache_file = os.path.join(OUTPUT_DIR, f"interaction_batch_{i+1}.md")
        if os.path.exists(cache_file):
            with open(cache_file, encoding="utf-8") as f:
                cached = f.read()
            if cached.strip():
                interaction_results.append(cached)
                print(f"\n  [{i+1}/{len(batches)}] 已有缓存，跳过 ✓")
                continue
        print(f"\n  [{i+1}/{len(batches)}] 分析中...", end=" ", flush=True)
        prompt = INTERACTION_PROMPT.format(name=args.name, messages=batch)
        result = call_ai(client, model, prompt)
        if result is None:
            continue
        interaction_results.append(result)
        print(f"✓ ({len(result)} 字)")
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write(result)

    # ── 第二轮：性格提取 ──
    print("\n" + "=" * 50)
    print("📝 第二轮：提取性格与价值观")
    print("=" * 50)

    personality_results = []
    for i, batch in enumerate(batches):
        cache_file = os.path.join(OUTPUT_DIR, f"personality_batch_{i+1}.md")
        if os.path.exists(cache_file):
            with open(cache_file, encoding="utf-8") as f:
                cached = f.read()
            if cached.strip():
                personality_results.append(cached)
                print(f"\n  [{i+1}/{len(batches)}] 已有缓存，跳过 ✓")
                continue
        print(f"\n  [{i+1}/{len(batches)}] 分析中...", end=" ", flush=True)
        prompt = PERSONALITY_PROMPT.format(name=args.name, messages=batch)
        result = call_ai(client, model, prompt)
        if result is None:
            continue
        personality_results.append(result)
        print(f"✓ ({len(result)} 字)")
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write(result)

    # ── 第三轮：合并 + 组装最终 prompt ──
    print("\n" + "=" * 50)
    print("🔧 第三轮：组装最终 system prompt")
    print("=" * 50)

    merged_interaction = "\n\n---\n\n".join(interaction_results)
    merged_personality = "\n\n---\n\n".join(personality_results)

    # 保存合并结果
    with open(os.path.join(OUTPUT_DIR, "interaction_merged.md"), "w", encoding="utf-8") as f:
        f.write(merged_interaction)
    with open(os.path.join(OUTPUT_DIR, "personality_merged.md"), "w", encoding="utf-8") as f:
        f.write(merged_personality)

    # 如果合并结果太长，先让 AI 各自浓缩
    if len(merged_interaction) + len(merged_personality) > 20000:
        print("\n  📦 数据较多，先浓缩各维度...")

        condense_prompt = "请将以下多批次分析结果合并浓缩为一份，去重、合并相同发现、保留所有独特特征和原话证据。\n\n{content}"

        print("  浓缩互动风格...", end=" ", flush=True)
        merged_interaction = call_ai(client, model, condense_prompt.format(content=merged_interaction))
        print(f"✓ ({len(merged_interaction)} 字)")

        print("  浓缩性格分析...", end=" ", flush=True)
        merged_personality = call_ai(client, model, condense_prompt.format(content=merged_personality))
        print(f"✓ ({len(merged_personality)} 字)")

    # 组装最终 prompt
    print("\n  🎯 生成最终 system prompt...", end=" ", flush=True)
    assembler = ASSEMBLER_PROMPT.format(
        name=args.name,
        interaction_results=merged_interaction,
        personality_results=merged_personality,
    )
    final_prompt = call_ai(client, model, assembler)
    print(f"✓ ({len(final_prompt)} 字)")

    # 保存
    output_file = os.path.join(OUTPUT_DIR, "system_prompt.md")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_prompt)

    print("\n" + "=" * 50)
    print(f"✅ 蒸馏完成！")
    print(f"   最终 prompt: {output_file}")
    print(f"   互动风格: {os.path.join(OUTPUT_DIR, 'interaction_merged.md')}")
    print(f"   性格分析: {os.path.join(OUTPUT_DIR, 'personality_merged.md')}")
    print(f"\n💡 下一步：把 system_prompt.md 的内容喂给千问，然后跟「假的你」聊天试试")


if __name__ == "__main__":
    main()
