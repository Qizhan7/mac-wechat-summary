"""AI summary interface - abstract base class and prompt templates."""
from abc import ABC, abstractmethod


SUMMARY_PROMPT = """你是一个专业的群聊消息总结助手。请对以下群聊消息进行高质量总结。

## 群聊信息
- 群名：{group_name}
- 时间范围：{start_time} ~ {end_time}
- 消息数：{msg_count} 条

## 输出格式要求（严格遵循）

第一行：一句话概述本时间段内群聊的整体氛围和涉及的主要方向。

然后是按话题归纳的详细总结，每个话题格式如下：

**1. [话题标题，用一句话概括本话题核心内容]**
· 时间：[该话题的起止时间]
· 群成员：[参与该话题讨论的主要成员名字，用顿号分隔]
· 总结：[详细总结该话题的讨论内容。必须包含具体发言人的名字，说明谁提出了什么观点、谁回应了什么、大家的共识是什么。要有细节和逻辑，不要泛泛而谈。]

**2. [下一个话题标题]**
...（同上格式）

最后，**仅当**消息中出现以下明确的通知信号时，才添加「相关提醒」栏目：
- @所有人 / @全体成员
- 明确标注为"群公告"、"通知"、"注意"、"重要"的内容
- 明确的时间约定（如集合时间、截止日期、会议安排）

如果没有上述信号，不要添加「相关提醒」。大家复制同一句话刷屏、接龙玩梗等不算提醒。

**相关提醒**
· [提醒内容，包含谁说的、什么事]

## 要求
1. 话题按时间顺序排列
2. 每个话题的总结必须提到具体发言人的名字，说明谁说了什么
3. **禁止**将发言人归纳为"其他成员"、"其他人"、"等人"等模糊称呼。始终使用消息记录中的原始发言人名字，即使名字看起来像 ID 也照常使用
4. 忽略纯表情包、拍一拍、无意义的水消息
5. 用自然流畅的中文书写，不要用列表罗列每条消息
6. 话题标题要具体生动，不要太笼统
7. 如果消息中有 [图片]、[视频]、[链接] 等非文字内容，简要提及即可
8. 不需要加"该总结由AI生成"等声明

## 消息记录
{messages}"""


BATCH_SUMMARY_PROMPT = """你是一个专业的群聊消息总结助手。现在需要你对 **多个群聊** 的消息进行精简总结。

## 分组名称：{group_category}
## 包含群聊：{group_list}
## 总时间范围：{start_time} ~ {end_time}

## 输出格式要求（严格遵循）

第一行：一句话概述这组群聊在本时间段内的整体活跃情况。

然后 **按群聊分别列出** 每个群的精简总结。格式如下：

---
### 📌 {{群名}}（{{消息数}}条 · {{时间范围}}）

**1. [话题标题]**
· 群成员：[参与成员]
· 总结：[精简总结，保留关键信息和发言人名字，省略冗长的讨论过程]

**2. [话题标题]**
...

---
### 📌 {{下一个群名}}（...）
...

最后，**仅当**消息中出现 @所有人/@全体成员、群公告、通知、注意、重要 等明确通知信号，或有具体的时间约定时，才添加以下栏目（复制刷屏、接龙玩梗不算）：

**⚠️ 需要关注**
· [提醒内容]

## 要求
1. 每个群聊单独一个区块，不要把不同群的消息混在一起
2. 比单群总结更精简：省略讨论过程中的反复讨论和细节，只保留结论和关键信息
3. 但仍然要提到具体发言人的名字
4. **禁止**将发言人归纳为"其他成员"、"其他人"、"等人"等模糊称呼。始终使用消息记录中的原始发言人名字，即使名字看起来像 ID 也照常使用
5. 忽略纯表情包、拍一拍、无意义的水消息
6. 如果某个群在该时间段没有新消息，直接写"暂无新消息"
7. 话题标题要具体，不要太笼统
8. 不需要加"该总结由AI生成"等声明

## 各群消息记录

{messages}"""


SEARCH_SUMMARY_PROMPT = """你是一个专业的群聊消息分析助手。用户搜索了关键词「{keywords}」，以下是在多个群聊中搜索到的相关消息。请对这些消息进行归纳总结。

## 搜索信息
- 搜索关键词：{keywords}
- 时间范围：{start_time} ~ {end_time}
- 涉及群聊：{group_list}
- 命中消息数：{total_count} 条

## 输出格式要求（严格遵循）

按群聊分别列出每个群中与关键词相关的讨论：

---
### 📌 {{群名}}

**1. [相关话题标题]**
· 时间：[该话题讨论的时间段]
· 发言人：[参与讨论的人]
· 经过：[简述讨论过程，可以简写]
· 结果：[重点！最终结论、决定、结果是什么]

**2. [下一个话题]**
...

---
### 📌 {{下一个群名}}
...

最后，总结一段：

**📋 综合结论**
· 关于「{keywords}」，各群讨论的核心结果和要点

## 要求
1. 按群聊分别列出，每个群内按时间顺序
2. **重点突出结果**，讨论经过可以简写，但结果必须详细
3. 必须提到具体发言人的名字，说明谁提出了什么、谁做了什么决定
4. 如果同一个群中有多次讨论同一话题，按时间合并为一个条目
5. 忽略与搜索关键词明显无关的内容
6. 用自然流畅的中文书写
7. 不需要加"该总结由AI生成"等声明

## 搜索到的消息记录

{messages}"""


class AIProvider(ABC):
    """AI provider abstract base class."""

    @abstractmethod
    def summarize(self, prompt: str) -> str:
        """Send prompt to AI and return summary result."""
        pass

    def build_prompt(self, group_name, messages_text, start_time, end_time, msg_count):
        """Build single-chat summary prompt."""
        return SUMMARY_PROMPT.format(
            group_name=group_name,
            start_time=start_time,
            end_time=end_time,
            msg_count=msg_count,
            messages=messages_text,
        )

    def build_search_prompt(self, keywords_str, search_results, start_time, end_time):
        """Build search summary prompt.

        Args:
            keywords_str: Raw keyword string (e.g. "claude api").
            search_results: {username: [messages]} grouped by chat.
            start_time: Display string for search start time.
            end_time: Display string for search end time.
        """
        group_names = []
        parts = []
        total_count = 0

        for username, messages in search_results.items():
            if not messages:
                continue
            group_name = messages[0]["group_name"]
            group_names.append(group_name)
            count = len(messages)
            total_count += count

            lines = []
            for msg in messages:
                if msg["sender"]:
                    lines.append(f"[{msg['time_str']}] {msg['sender']}: {msg['text']}")
                else:
                    lines.append(f"[{msg['time_str']}] {msg['text']}")

            parts.append(
                f"======== {group_name}（{count}条命中）========\n"
                + "\n".join(lines)
            )

        messages_text = "\n\n".join(parts)
        group_list = "、".join(group_names)

        return SEARCH_SUMMARY_PROMPT.format(
            keywords=keywords_str,
            start_time=start_time,
            end_time=end_time,
            group_list=group_list,
            total_count=total_count,
            messages=messages_text,
        )

    def build_batch_prompt(self, group_category, groups_data):
        """Build batch summary prompt.

        Args:
            group_category: Group category name.
            groups_data: List of dicts with keys: name, messages_text,
                start_time, end_time, msg_count.
        """
        group_list = "、".join(g["name"] for g in groups_data)

        # Overall time range
        all_starts = [g["start_time"] for g in groups_data if g["msg_count"] > 0]
        all_ends = [g["end_time"] for g in groups_data if g["msg_count"] > 0]
        start_time = min(all_starts) if all_starts else "N/A"
        end_time = max(all_ends) if all_ends else "N/A"

        # Concatenate messages from all groups
        parts = []
        for g in groups_data:
            if g["msg_count"] > 0:
                parts.append(
                    f"======== {g['name']}（{g['msg_count']}条 · "
                    f"{g['start_time']} ~ {g['end_time']}）========\n"
                    f"{g['messages_text']}"
                )
            else:
                parts.append(f"======== {g['name']} ========\n（暂无新消息）")

        messages = "\n\n".join(parts)

        return BATCH_SUMMARY_PROMPT.format(
            group_category=group_category,
            group_list=group_list,
            start_time=start_time,
            end_time=end_time,
            messages=messages,
        )
