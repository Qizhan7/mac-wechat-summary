#!/usr/bin/env python3
"""
从微信聊天记录中提取「盏的说话风格」训练数据。
保留完整对话上下文，标记哪些是自己的发言。

用法：
    python extract_my_style.py                    # 交互式选群
    python extract_my_style.py --all              # 所有群聊
    python extract_my_style.py --list             # 列出所有群聊
    python extract_my_style.py --group "群名"     # 指定群聊（可多次使用）
    python extract_my_style.py --limit 2000       # 每个群最多提取条数（默认1000）
    python extract_my_style.py --private "联系人"  # 提取私聊
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import load_config
from core.wechat_db import WeChatDB

OUTPUT_DIR = os.path.expanduser("~/.wechat-summary/extracted")


def get_db():
    cfg = load_config()
    db_dir = cfg.get("db_dir", "")
    keys_file = cfg.get("keys_file", os.path.expanduser("~/.wechat-summary/all_keys.json"))
    if not db_dir or not os.path.isdir(db_dir):
        print("❌ 数据库目录未配置，请先运行主程序配置")
        sys.exit(1)
    with open(keys_file) as f:
        keys = json.load(f)
    return WeChatDB(db_dir, keys)


def format_conversation(messages, chat_name):
    """把消息列表格式化为带上下文的对话训练数据。"""
    lines = []
    lines.append(f"=== 群聊: {chat_name} ===\n")

    for msg in messages:
        sender = msg["sender"]
        text = msg["text"]
        time_str = msg["time_str"]

        # "您" 是群聊中自己的消息，"我" 是私聊中自己的消息
        is_me = sender in ("您", "我")

        if is_me:
            lines.append(f"[{time_str}] 【我】: {text}")
        else:
            lines.append(f"[{time_str}] {sender}: {text}")

    return "\n".join(lines)


def extract_style_samples(messages):
    """提取「上下文 → 我的回复」配对，用于 few-shot / fine-tune。"""
    samples = []
    context_window = 5  # 我的每条回复前保留几条上下文

    for i, msg in enumerate(messages):
        is_me = msg["sender"] in ("您", "我")
        if not is_me:
            continue
        if msg["text"] in ("[图片]", "[表情]", "[语音]", "[视频]"):
            continue

        # 收集前面的上下文
        start = max(0, i - context_window)
        context = []
        for j in range(start, i):
            prev = messages[j]
            prev_is_me = prev["sender"] in ("您", "我")
            prefix = "【我】" if prev_is_me else prev["sender"]
            context.append(f"{prefix}: {prev['text']}")

        samples.append({
            "context": context,
            "response": msg["text"],
            "time": msg["time_str"],
        })

    return samples


def main():
    parser = argparse.ArgumentParser(description="提取微信聊天风格数据")
    parser.add_argument("--list", action="store_true", help="列出所有群聊")
    parser.add_argument("--all", action="store_true", help="提取所有群聊")
    parser.add_argument("--group", action="append", help="指定群名（可多次）")
    parser.add_argument("--private", action="append", help="指定私聊联系人（可多次）")
    parser.add_argument("--limit", type=int, default=1000, help="每个聊天最多条数")
    args = parser.parse_args()

    db = get_db()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 列出群聊
    if args.list:
        groups = db.get_groups(include_unnamed=True)
        for i, g in enumerate(groups):
            print(f"  {i+1:3d}. {g['name']}  ({g['username']})")
        print(f"\n共 {len(groups)} 个群聊")
        return

    # 确定要提取的群聊
    groups = db.get_groups(include_unnamed=True)
    targets = []

    if args.all:
        targets = groups
    elif args.group:
        for name in args.group:
            matched = [g for g in groups if name in g["name"]]
            if matched:
                targets.extend(matched)
            else:
                print(f"⚠️  未找到包含「{name}」的群聊")
    else:
        # 交互式选择
        print("选择要提取的群聊（输入序号，逗号分隔，或 all）：\n")
        for i, g in enumerate(groups):
            print(f"  {i+1:3d}. {g['name']}")
        print()
        choice = input(">>> ").strip()
        if choice.lower() == "all":
            targets = groups
        else:
            for idx_str in choice.split(","):
                try:
                    idx = int(idx_str.strip()) - 1
                    if 0 <= idx < len(groups):
                        targets.append(groups[idx])
                except ValueError:
                    pass

    if not targets and not args.private:
        print("没有选择任何聊天")
        return

    all_samples = []
    all_conversations = []

    # 提取群聊
    for g in targets:
        print(f"📥 提取群聊: {g['name']} ...", end=" ", flush=True)
        messages = db.get_messages(g["username"], limit=args.limit)
        if not messages:
            print("(无消息)")
            continue

        my_count = sum(1 for m in messages if m["sender"] in ("您", "我"))
        print(f"共 {len(messages)} 条，其中我的 {my_count} 条")

        conv = format_conversation(messages, g["name"])
        all_conversations.append(conv)

        samples = extract_style_samples(messages)
        all_samples.extend(samples)

    # 提取私聊
    if args.private:
        db._load_contacts()
        for name in args.private:
            # 从联系人中找
            found_username = None
            for uname, display in db._contacts.items():
                if name in display and "@chatroom" not in uname:
                    found_username = uname
                    break
            if not found_username:
                print(f"⚠️  未找到联系人「{name}」")
                continue

            print(f"📥 提取私聊: {name} ...", end=" ", flush=True)
            messages = db.get_messages(found_username, limit=args.limit)
            if not messages:
                print("(无消息)")
                continue

            my_count = sum(1 for m in messages if m["sender"] == "我")
            print(f"共 {len(messages)} 条，其中我的 {my_count} 条")

            conv = format_conversation(messages, f"私聊-{name}")
            all_conversations.append(conv)

            samples = extract_style_samples(messages)
            all_samples.extend(samples)

    if not all_samples:
        print("没有提取到任何数据")
        return

    # 保存完整对话（给人看的）
    conv_file = os.path.join(OUTPUT_DIR, "conversations.txt")
    with open(conv_file, "w", encoding="utf-8") as f:
        f.write("\n\n".join(all_conversations))

    # 保存结构化样本（给模型用的 JSONL）
    jsonl_file = os.path.join(OUTPUT_DIR, "style_samples.jsonl")
    with open(jsonl_file, "w", encoding="utf-8") as f:
        for s in all_samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # 生成 system prompt 素材
    prompt_file = os.path.join(OUTPUT_DIR, "system_prompt_draft.md")
    _generate_prompt_draft(all_samples, prompt_file)

    print(f"\n✅ 提取完成！")
    print(f"   对话原文: {conv_file}")
    print(f"   训练样本: {jsonl_file} ({len(all_samples)} 条)")
    print(f"   Prompt草稿: {prompt_file}")


def _generate_prompt_draft(samples, output_path):
    """生成一个 system prompt 草稿，里面塞了典型回复示例。"""
    # 按回复长度分桶，取多样化的样本
    short = [s for s in samples if len(s["response"]) <= 10]
    medium = [s for s in samples if 10 < len(s["response"]) <= 50]
    long = [s for s in samples if len(s["response"]) > 50]

    # 每种取一些做 few-shot 示例
    import random
    random.seed(42)
    examples = []
    for bucket, n in [(short, 8), (medium, 6), (long, 4)]:
        if bucket:
            examples.extend(random.sample(bucket, min(n, len(bucket))))

    lines = [
        "# 角色：模仿我的微信群聊风格\n",
        "你需要模仿以下说话风格来回复群聊消息。\n",
        "## 风格特征\n",
        "（请根据下面的示例总结你的风格特征，然后填在这里）\n",
        "## 示例对话\n",
    ]

    for i, ex in enumerate(examples):
        lines.append(f"### 示例 {i+1}")
        if ex["context"]:
            lines.append("上下文：")
            for c in ex["context"]:
                lines.append(f"  {c}")
        lines.append(f"我的回复：{ex['response']}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
