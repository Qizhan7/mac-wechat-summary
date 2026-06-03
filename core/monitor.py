"""Topic monitoring for WeChat chats.

The monitor is deliberately read-only: it reads decrypted DB cache data,
calls an AI model to classify new messages, and optionally writes local hit
records. It never activates WeChat or sends messages.
"""
import json
import os
import re
import time
from datetime import datetime

from .config import DATA_DIR
from .knowledge import (
    KnowledgeStore,
    RELATION_NOTIFY,
    normalize_candidate,
    normalize_relation,
)
from .keychain import load_key

STATE_FILE = os.path.join(DATA_DIR, "monitor_state.json")
HITS_DIR = os.path.join(DATA_DIR, "monitor_hits")
URL_RE = re.compile(r"https?://[^\s<>'\"，。；：！？、（）()【】\\[\\]{}]+")


class MonitorConfigError(RuntimeError):
    """Raised when the monitor is missing required user configuration."""


def load_state(path=STATE_FILE):
    """Load monitor state."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state, path=STATE_FILE):
    """Persist monitor state."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def initialize_state_if_needed(path=STATE_FILE, now_func=time.time):
    """Set the first monitor checkpoint to now, avoiding historical floods."""
    state = load_state(path)
    if not state.get("last_checked_ts"):
        state["last_checked_ts"] = now_func()
        save_state(state, path)
        return True
    return False


def get_deepseek_api_key():
    """Read the DeepSeek API key from env, then the app Keychain."""
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return env_key
    return (load_key("deepseek-api-key") or load_key("ai-api-key") or "").strip()


class TopicMonitor:
    """Check one chat for user-interesting topics."""

    def __init__(
        self,
        db,
        config,
        state_file=STATE_FILE,
        hits_dir=HITS_DIR,
        ai_evaluator=None,
        relation_evaluator=None,
        knowledge_store=None,
        now_func=time.time,
    ):
        self.db = db
        self.config = config
        self.state_file = state_file
        self.hits_dir = hits_dir
        self.ai_evaluator = ai_evaluator
        self.relation_evaluator = relation_evaluator
        self.knowledge_store = knowledge_store
        self.now_func = now_func

    def check_once(self, dry_run=False):
        """Run a single monitor check.

        Args:
            dry_run: When True, do not update state or save hit files.
        """
        topic = self.config.get("monitor_topic", "").strip()
        if not topic:
            return {"status": "missing_topic", "message": "请先设置关注描述"}

        username = self.config.get("monitor_chat_username", "").strip()
        if not username:
            raise MonitorConfigError("监控群聊未配置")

        state = load_state(self.state_file)
        if not dry_run and not state.get("last_checked_ts"):
            state["last_checked_ts"] = self.now_func()
            save_state(state, self.state_file)
            return {"status": "initialized", "message": "已从当前时间开始监控"}

        since_ts = self._get_since_ts(state, dry_run)
        max_messages = self.config.get("monitor_max_messages_per_run", 200)

        messages = self.db.get_messages(username, since_ts=since_ts, limit=max_messages)
        if not messages:
            return {"status": "no_messages", "message": "没有新消息"}

        messages_text = self.db.format_messages_for_ai(
            messages,
            show_group_nickname=self.config.get("show_group_nickname", True),
        )
        decision = self._evaluate(messages, messages_text, topic)
        normalized = self._normalize_decision(decision, messages)

        last_msg_ts = messages[-1]["timestamp"]
        result = {
            "status": "no_match",
            "message_count": len(messages),
            "last_msg_ts": last_msg_ts,
            "decision": normalized,
        }

        if not dry_run:
            state["last_checked_ts"] = last_msg_ts

        if not normalized["match"] or normalized["score"] < 70:
            if not dry_run:
                save_state(state, self.state_file)
            return result

        if self._knowledge_enabled():
            knowledge_result = self._process_with_knowledge(
                normalized,
                messages,
                messages_text,
                dry_run=dry_run,
            )
            result.update(knowledge_result)
            if dry_run:
                result["status"] = "matched"
                return result

            state["last_topic_key"] = normalized["topic_key"]
            if result.get("status") == "duplicate":
                save_state(state, self.state_file)
                return result

            if result.get("relation") in RELATION_NOTIFY:
                state["last_notified_ts"] = self.now_func()
                hit_path = self._save_hit(messages, normalized)
                result["hit_path"] = hit_path
            save_state(state, self.state_file)
            return result

        if self._is_in_cooldown(state, normalized):
            result["status"] = "cooldown"
            if not dry_run:
                save_state(state, self.state_file)
            return result

        result.update({
            "status": "matched" if dry_run else "notified",
            "title": normalized["title"],
            "summary": normalized["summary"],
            "topic_key": normalized["topic_key"],
        })

        if not dry_run:
            hit_path = self._save_hit(messages, normalized)
            state["last_topic_key"] = normalized["topic_key"]
            state["last_notified_ts"] = self.now_func()
            save_state(state, self.state_file)
            result["hit_path"] = hit_path

        return result

    def _get_since_ts(self, state, dry_run):
        if dry_run:
            interval = self.config.get("monitor_interval_minutes", 3)
            return self.now_func() - interval * 60
        if state.get("last_checked_ts"):
            return float(state["last_checked_ts"])
        return 0

    def _knowledge_enabled(self):
        return bool(self.config.get("monitor_knowledge_enabled", False))

    def _get_knowledge_store(self, dry_run=False):
        if self.knowledge_store is not None:
            return self.knowledge_store
        return KnowledgeStore.from_config(self.config, now_func=self.now_func, read_only=dry_run)

    def _process_with_knowledge(self, decision, messages, messages_text, dry_run=False):
        candidate = normalize_candidate(decision)
        store = self._get_knowledge_store(dry_run=dry_run)
        candidates = store.find_candidates(candidate)
        relation_decision = self._classify_knowledge_relation(candidate, candidates, messages_text)
        relation = normalize_relation(relation_decision.get("relation"))

        if dry_run:
            return {
                "relation": relation,
                "relation_reason": relation_decision.get("reason", ""),
                "title": self._notification_title(decision, relation),
                "summary": decision["summary"],
                "topic_key": decision["topic_key"],
                "knowledge_candidates": candidates,
                "knowledge_candidate": candidate,
            }

        if relation != "new" and not relation_decision.get("target_topic_id"):
            if candidates:
                relation_decision["target_topic_id"] = candidates[0]["topic_id"]
            else:
                relation_decision["relation"] = "new"
                relation = "new"

        if relation == "new":
            related_ids = [c["topic_id"] for c in candidates if c.get("score", 0) >= 25][:3]
            if related_ids:
                relation_decision["related_topic_ids"] = related_ids

        knowledge = store.apply_event(candidate, messages, self.config, relation_decision)
        status = "duplicate" if relation == "duplicate" else "notified"
        return {
            "status": status,
            "relation": relation,
            "relation_reason": relation_decision.get("reason", ""),
            "title": self._notification_title(decision, relation),
            "summary": decision["summary"],
            "topic_key": decision["topic_key"],
            "knowledge_topic_id": knowledge.get("topic_id"),
            "knowledge_event_id": knowledge.get("event_id"),
            "knowledge_path": knowledge.get("knowledge_path", ""),
            "obsidian_path": knowledge.get("obsidian_path", ""),
            "knowledge_candidates": candidates,
        }

    def _classify_knowledge_relation(self, candidate, candidates, messages_text):
        if not candidates:
            return {"relation": "new", "reason": "没有相似旧主题"}

        prompt = self._build_relation_prompt(candidate, candidates, messages_text)
        try:
            if self.relation_evaluator:
                raw = self.relation_evaluator(prompt, self.config)
            else:
                raw = self._call_deepseek(prompt)
            decision = self._parse_json(raw) if isinstance(raw, str) else raw
        except Exception as e:
            return {
                "relation": "update",
                "target_topic_id": candidates[0]["topic_id"],
                "reason": f"关系判定失败，按新线索保守提醒: {e}",
            }

        if not isinstance(decision, dict):
            decision = {}

        target_topic_id = decision.get("target_topic_id")
        candidate_ids = {c["topic_id"] for c in candidates}
        try:
            target_topic_id = int(target_topic_id)
        except (TypeError, ValueError):
            target_topic_id = None
        if target_topic_id not in candidate_ids:
            target_topic_id = candidates[0]["topic_id"] if normalize_relation(decision.get("relation")) != "new" else None

        return {
            "relation": normalize_relation(decision.get("relation")),
            "target_topic_id": target_topic_id,
            "reason": str(decision.get("reason") or "").strip(),
        }

    def _build_relation_prompt(self, candidate, candidates, messages_text):
        candidate_text = json.dumps(candidate, ensure_ascii=False, indent=2)
        candidates_text = json.dumps(candidates, ensure_ascii=False, indent=2)
        return f"""你是微信群关注推送的本地知识库判重助手。请判断这次新命中的候选内容与已有主题的关系。

关系只能是：
- duplicate：只是旧主题的重复说法，没有新增事实、链接、结论、反转或重要人物回应。
- update：属于已有主题的新线索、新链接、新事实、新测评、新讨论进展，值得提醒。
- contradiction：对已有主题形成辟谣、反转、纠错、冲突证据，值得提醒。
- new：不是这些旧主题，应该新建主题。

判断标准要适配微信群消息：
- 同一个传闻/发布/链接/模型测评，短时间内可能多次刷屏；没有新信息就是 duplicate。
- 同主题里出现新链接、更多人确认、关键说法变化、官方/半官方来源、实际测评、发布时间线推进，就是 update。
- 旧传闻被否认、图片被指出是假的、结论相反，就是 contradiction。
- 只按语义和事实判断，不要因为标题不同就判 new。

<new_candidate>
{candidate_text}
</new_candidate>

<existing_topics>
{candidates_text}
</existing_topics>

<source_messages>
{messages_text}
</source_messages>

只输出严格 JSON：
{{
  "relation": "duplicate|update|new|contradiction",
  "target_topic_id": 123,
  "reason": "一句话解释"
}}"""

    @staticmethod
    def _notification_title(decision, relation):
        if relation == "update":
            return f"新线索: {decision['title']}"
        if relation == "contradiction":
            return f"反转/辟谣: {decision['title']}"
        return decision["title"]

    def _evaluate(self, messages, messages_text, topic):
        prompt = self._build_prompt(messages, messages_text, topic)
        if self.ai_evaluator:
            return self.ai_evaluator(prompt, self.config)
        return self._call_deepseek(prompt)

    def _build_prompt(self, messages, messages_text, topic):
        group_name = self.config.get("monitor_chat_display_name", "监控群聊")
        start_time = messages[0].get("time_str", "")
        end_time = messages[-1].get("time_str", "")
        return f"""你是一个专业的微信群消息关注与总结助手。请根据用户自己设定的关注描述，判断新增消息中是否有值得提醒用户查看的内容；如果值得提醒，就生成一段简洁、有用、按话题整理的小摘要。

不要把关注描述当作关键词搜索。先理解用户真正想捕捉的信息类型，再结合群聊上下文判断是否语义相关、是否有具体内容、是否值得现在提醒。用户的关注点可能来自工作、AI、家人、朋友、生活安排、兴趣娱乐或任何其他场景；判断标准以用户写下的关注描述为准。

<user_interest>
{topic}
</user_interest>

<chat_context>
群聊：{group_name}
时间：{start_time} ~ {end_time}
消息数：{len(messages)}
</chat_context>

<decision_policy>
1. 先理解用户关注描述背后的真实意图，包括用户想知道的对象、事件、变化、机会、风险、情绪或提醒。
2. 从新增消息中聚合 0-3 个候选话题；不要逐条消息机械判断，也不要只按关键词判断。
3. 对每个候选话题按以下维度估分：
   - semantic_relevance：是否真的符合用户兴趣，而不是只出现相似词。
   - usefulness：是否满足用户描述的价值类型；可能是事实信息、提醒、风险、决策价值、情绪价值、趣味性、关系动态或启发。
   - novelty：是否像新增信息，而不是重复闲聊或已知内容。
   - urgency：是否值得现在通知用户，而不是等之后总结也行。
4. 如果用户关注的是轻松内容、情绪动态、家庭消息、朋友近况、趣事或八卦，不要用“工作价值/行动价值”过滤它；改用“是否符合用户想看的东西、是否有看点、是否有人回应、是否可能影响用户”来判断。
5. 如果用户关注的是宽口径主题（例如新鲜事、新想法、重要更新、家人近况、好玩的事），请从新增消息中提炼亮点，而不是等待明确公告。
6. 只有综合判断“符合用户关注描述且值得提醒”时 match=true。
7. 宁可漏掉无看点的水聊，也不要频繁误报。
8. 不要被闲聊比例稀释：即使 100 条里只有 1-2 条有价值，只要那 1-2 条明确符合用户关注描述，也要作为候选判断。
9. 如果用户关注描述包含新功能、产品更新、AI 工具、链接、教程、实验报告、具体做法、自建 app 或 agent 设计，那么“明确对象 + 明确变化/功能/做法/链接/结论”的单条消息也可以通知；不要因为只有单条就降到 70 分以下。
</decision_policy>

<negative_rules>
以下情况不要通知：
- 只是提到相关词，但没有实质内容。
- 与用户兴趣无关的玩笑、表情、复读、寒暄、跑题闲聊。
- 和兴趣点只弱相关，用户之后看总结也不迟。
- 没有明确证据消息支撑。
- 只有单句暧昧暗示，缺少上下文，无法判断价值。
注意：如果单句里已经包含明确产品/项目/模型/工具名，加上新功能、更新、链接、教程、实验结果、修复方案或可执行做法，它不是“无上下文的新消息”，可以通知。
注意：如果用户关注描述本身就是轻松、情绪、关系、家庭或生活类内容，相关的玩笑、趣事、近况、反应和情绪变化可以通知；不要因为它不是严肃信息就判为无价值。
</negative_rules>

<scoring>
score 是 0-100：
- 0-39：无关或只有字面擦边。
- 40-59：有点相关，但不值得通知。
- 60-69：可能相关，但证据不足或价值一般，不通知。
- 70-84：明确相关，且有信息价值/启发价值/行动价值/情绪价值之一，可以通知。
- 85-100：高度相关、新颖、有明显看点或多人回应，值得立刻看。
对于宽口径兴趣：只要候选话题有明确上下文、有人回应、且符合用户关注描述中的价值类型，score 可以达到 70 以上。
对于新功能/产品更新/链接资源/实验报告/具体做法：只要有明确对象和可复查的信息，哪怕消息很短，也应给到 70-84。
低于 70 必须 match=false。
</scoring>

<output_rules>
只输出严格 JSON，不要 Markdown，不要解释 JSON 外的文字。
score 只用于程序内部判断，不要把评分写进 title 或 digest。
digest 是给用户看的最终内容，不要粘贴原文证据，不要输出“评分/证据/原文”栏目。
digest 使用群聊总结风格，按时间顺序列出 1-5 条，格式类似：
1. 【时间】A 提到了某个有用网站/链接：说明它能做什么，并保留网址
2. 【时间】B 提到某个新消息/安排/变化，C、D 有附和或补充
3. 【时间】C 提出某个观点，D 反驳了什么，最后大致形成什么结论
每条都要写清谁说了什么、为什么值得看；有链接、时间、决定、结论时必须保留。
如果只有一个话题，也写成 1 条；如果没有值得提醒的内容，digest 为空字符串。
topic_key 用稳定短语，便于同一主题冷却去重。
category 用简短中文分类，例如 AI模型、工具更新、发布传闻、教程资源、群内八卦、生活安排、待确认信息、已辟谣、未分类。
entities 是涉及的人、产品、模型、公司、项目或群友昵称。
key_facts 是可复用到知识库的关键事实，不要写空泛评价。
links 必须提取消息中出现的 URL；没有则空数组。
event_type 用简短短语描述事件类型，例如 rumor、release、benchmark、resource、debunk、discussion。
status_hint 可为 tracking、rumor、confirmed、disputed、resolved；不确定用 tracking。
</output_rules>

<output_json>
{{
  "match": false,
  "score": 0,
  "reason": "一句话说明为什么通知或不通知",
  "title": "短标题",
  "digest": "1. 【时间】谁提到了什么，为什么值得看\\n2. 【时间】谁补充/反驳/附和了什么，结论是什么",
  "topic_key": "短主题标识",
  "category": "分类",
  "entities": ["Claude", "OpenAI"],
  "key_facts": ["可沉淀的事实或线索"],
  "links": ["https://example.com"],
  "event_type": "rumor",
  "status_hint": "tracking"
}}
</output_json>

<messages>
{messages_text}
</messages>"""

    def _call_deepseek(self, prompt):
        api_key = get_deepseek_api_key()
        if not api_key:
            raise MonitorConfigError("请先设置 DeepSeek API Key")

        from openai import OpenAI

        model = self.config.get("monitor_ai_model", "deepseek-v4-flash")
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=60.0)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content

    def _normalize_decision(self, decision, messages=None):
        if isinstance(decision, str):
            decision = self._parse_json(decision)
        if not isinstance(decision, dict):
            decision = {}

        title = str(decision.get("title") or "发现关注内容").strip()
        summary = self._normalize_digest(decision)
        topic_key = str(decision.get("topic_key") or title).strip()

        return {
            "match": bool(decision.get("match")),
            "score": self._clamp_score(decision.get("score", 0)),
            "title": title[:60] or "发现关注内容",
            "summary": summary[:1200],
            "topic_key": self._clean_topic_key(topic_key),
            "category": self._clean_short_text(decision.get("category"), "未分类", 40),
            "entities": self._normalize_list(decision.get("entities"), 16),
            "key_facts": self._normalize_list(decision.get("key_facts"), 20),
            "links": self._normalize_links(decision, summary, messages),
            "event_type": self._clean_short_text(decision.get("event_type"), "", 80),
            "status_hint": self._clean_short_text(decision.get("status_hint"), "tracking", 80),
        }

    def _normalize_digest(self, decision):
        digest = decision.get("digest")
        if isinstance(digest, str) and digest.strip():
            return digest.strip()

        items = decision.get("items")
        if isinstance(items, list):
            lines = []
            for idx, item in enumerate(items, 1):
                if isinstance(item, dict):
                    time_text = str(item.get("time") or "").strip()
                    text = str(item.get("summary") or item.get("text") or "").strip()
                    if text:
                        prefix = f"{idx}. "
                        if time_text:
                            prefix += f"【{time_text}】"
                        lines.append(prefix + text)
                else:
                    text = str(item).strip()
                    if text:
                        lines.append(f"{idx}. {text}")
            if lines:
                return "\n".join(lines)

        return str(decision.get("summary") or "").strip()

    def _parse_json(self, text):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                return {}
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}

    @staticmethod
    def _clamp_score(value):
        try:
            score = int(value)
        except (TypeError, ValueError):
            score = 0
        return max(0, min(100, score))

    @staticmethod
    def _clean_topic_key(value):
        value = value.strip()[:80]
        return value or "关注内容"

    @staticmethod
    def _clean_short_text(value, default="", limit=80):
        text = str(value or "").strip()
        return text[:limit] if text else default

    @staticmethod
    def _normalize_list(value, limit=12):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        result = []
        seen = set()
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(text[:180])
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _extract_links_from_text(text):
        links = []
        for match in URL_RE.findall(str(text or "")):
            url = match.rstrip(".,;:!?，。；：！？、")
            if url:
                links.append(url)
        return links

    def _normalize_links(self, decision, summary, messages=None):
        links = []
        links.extend(self._normalize_list(decision.get("links"), 20))
        links.extend(self._extract_links_from_text(summary))
        for msg in messages or []:
            links.extend(self._extract_links_from_text(msg.get("text", "")))
        return self._normalize_list(links, 20)

    def _is_in_cooldown(self, state, decision):
        cooldown_min = self.config.get("monitor_cooldown_minutes", 15)
        if cooldown_min <= 0:
            return False
        if state.get("last_topic_key") != decision["topic_key"]:
            return False
        last_notified = state.get("last_notified_ts", 0)
        return self.now_func() - float(last_notified or 0) < cooldown_min * 60

    def _save_hit(self, messages, decision):
        os.makedirs(self.hits_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_key = re.sub(r"[^0-9A-Za-z._-]+", "_", decision["topic_key"])[:40] or "hit"
        path = os.path.join(self.hits_dir, f"{timestamp}_{safe_key}.txt")

        lines = [
            "关注推送命中",
            "=" * 40,
            f"群聊: {self.config.get('monitor_chat_display_name', '')}",
            f"时间: {messages[0].get('time_str', '')} ~ {messages[-1].get('time_str', '')}",
            f"主题: {decision['topic_key']}",
            "",
            decision["summary"],
        ]

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
