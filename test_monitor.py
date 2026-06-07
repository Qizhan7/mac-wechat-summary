import os
import tempfile
import unittest

from core.monitor import TopicMonitor, load_state, save_state
from core.knowledge import KnowledgeStore
from core.link_preview import fetch_link_preview
from core.api_errors import is_retryable_ai_error, normalize_ai_error
from core.wechat_db import _clean_msg_text


class FakeDB:
    def __init__(self, messages):
        self.messages = messages
        self.calls = 0

    def get_messages(self, username, since_ts=0, limit=500):
        self.calls += 1
        messages = [m for m in self.messages if m["timestamp"] > since_ts]
        return messages[-limit:]

    def format_messages_for_ai(self, messages, show_group_nickname=False):
        return "\n".join(
            f"[{m['time_str']}] {m.get('sender', '')}: {m['text']}"
            for m in messages
        )


def msg(ts, text):
    return {
        "timestamp": ts,
        "time_str": f"2026-05-29 00:{ts:02d}",
        "sender": "成员",
        "text": text,
        "type": 1,
    }


class TopicMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp.name, "state.json")
        self.hits_dir = os.path.join(self.tmp.name, "hits")
        self.config = {
            "monitor_topic": "Claude Code 新功能",
            "monitor_chat_username": "chatroom",
            "monitor_chat_display_name": "Claude恋爱技术群",
            "monitor_interval_minutes": 3,
            "monitor_max_messages_per_run": 200,
            "monitor_cooldown_minutes": 15,
            "show_group_nickname": True,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_ai_502_html_error_is_user_friendly_and_retryable(self):
        error = """<html>
<head><title>502 Bad Gateway</title></head>
<body><center><h1>502 Bad Gateway</h1></center></body>
</html>"""

        self.assertTrue(is_retryable_ai_error(error))
        self.assertEqual(
            normalize_ai_error(error, "DeepSeek"),
            "DeepSeek API 服务临时不可用，请稍后再试",
        )

    def monitor(self, db, evaluator, now=1000, relation_evaluator=None, knowledge_store=None):
        return TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=evaluator,
            relation_evaluator=relation_evaluator,
            knowledge_store=knowledge_store,
            now_func=lambda: now,
        )

    def test_no_messages_does_not_call_ai(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        called = []
        db = FakeDB([])

        result = self.monitor(db, lambda *_: called.append(True)).check_once()

        self.assertEqual(result["status"], "no_messages")
        self.assertEqual(called, [])

    def test_no_match_updates_bookmark(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "普通闲聊")])

        result = self.monitor(db, lambda *_: {"match": False, "score": 20}).check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 11)

    def test_match_writes_hit_and_state(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 发布新功能")])
        decision = {
            "match": True,
            "score": 92,
            "title": "Claude Code 新功能",
            "digest": "1. 【00:11】成员提到 Claude Code 发布新功能，值得看。",
            "summary": "1. 【00:11】成员提到 Claude Code 发布新功能，值得看。",
            "topic_key": "claude-code-new-feature",
        }

        result = self.monitor(db, lambda *_: decision).check_once()
        state = load_state(self.state_file)

        self.assertEqual(result["status"], "notified")
        self.assertTrue(os.path.exists(result["hit_path"]))
        with open(result["hit_path"], encoding="utf-8") as f:
            hit_text = f.read()
        self.assertIn("1. 【00:11】成员提到 Claude Code 发布新功能", hit_text)
        self.assertNotIn("评分", hit_text)
        self.assertNotIn("证据", hit_text)
        self.assertNotIn("新增消息", hit_text)
        self.assertEqual(state["last_checked_ts"], 11)
        self.assertEqual(state["last_topic_key"], "claude-code-new-feature")

    def test_cooldown_suppresses_duplicate_topic(self):
        save_state({
            "last_checked_ts": 10,
            "last_topic_key": "same-topic",
            "last_notified_ts": 950,
        }, self.state_file)
        db = FakeDB([msg(11, "重复讨论")])
        decision = {
            "match": True,
            "score": 90,
            "title": "重复主题",
            "summary": "重复",
            "topic_key": "same-topic",
        }

        result = self.monitor(db, lambda *_: decision, now=1000).check_once()

        self.assertEqual(result["status"], "cooldown")
        self.assertFalse(os.path.isdir(self.hits_dir))
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 11)

    def test_message_limit_uses_latest_messages(self):
        self.config["monitor_max_messages_per_run"] = 200
        self.config["monitor_interval_minutes"] = 999
        save_state({"last_checked_ts": 0.1}, self.state_file)
        messages = [msg(i, f"消息{i}") for i in range(1, 251)]
        db = FakeDB(messages)
        seen_prompt = []

        monitor = self.monitor(db, lambda prompt, *_: seen_prompt.append(prompt) or {"match": False})
        result = monitor.check_once(dry_run=True)

        self.assertEqual(result["message_count"], 200)
        self.assertIn("消息51", seen_prompt[0])
        self.assertNotIn("消息50", seen_prompt[0])

    def test_recent_context_is_included_without_counting_as_new(self):
        save_state({"last_checked_ts": 100}, self.state_file)
        db = FakeDB([
            msg(95, "把会变化的 block 放在断点后面"),
            msg(101, "role 需要是 user，不然会破坏缓存"),
        ])
        seen_prompt = []

        result = self.monitor(
            db,
            lambda prompt, *_: seen_prompt.append(prompt) or {"match": False, "score": 20},
        ).check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["message_count"], 1)
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 101)
        self.assertIn("<recent_context>", seen_prompt[0])
        self.assertIn("把会变化的 block 放在断点后面", seen_prompt[0])
        self.assertIn("role 需要是 user", seen_prompt[0])

    def test_only_recent_context_does_not_call_ai(self):
        save_state({"last_checked_ts": 100}, self.state_file)
        called = []
        db = FakeDB([msg(95, "把会变化的 block 放在断点后面")])

        result = self.monitor(db, lambda *_: called.append(True)).check_once()

        self.assertEqual(result["status"], "no_messages")
        self.assertEqual(called, [])

    def test_dry_run_does_not_update_state(self):
        self.config["monitor_interval_minutes"] = 999
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 发布新功能")])

        result = self.monitor(
            db,
            lambda *_: {"match": True, "score": 95, "topic_key": "dry"},
        ).check_once(dry_run=True)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 10)
        self.assertFalse(os.path.isdir(self.hits_dir))

    def test_knowledge_duplicate_suppresses_notification_and_hit_file(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        save_state({"last_checked_ts": 10}, self.state_file)
        store = KnowledgeStore(
            self.config["monitor_knowledge_db"],
            self.config["monitor_obsidian_root"],
            now_func=lambda: 900,
        )
        first = store.apply_event(
            self._knowledge_decision(),
            [msg(10, "Claude Code 发布新功能")],
            self.config,
            {"relation": "new"},
        )
        db = FakeDB([msg(11, "Claude Code 发布新功能")])

        result = self.monitor(
            db,
            lambda *_: self._knowledge_decision(summary="1. 【00:11】成员重复提到 Claude Code 发布新功能。"),
            relation_evaluator=lambda *_: {
                "relation": "duplicate",
                "target_topic_id": first["topic_id"],
            },
        ).check_once()

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["relation"], "duplicate")
        self.assertFalse(os.path.isdir(self.hits_dir))
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 11)

    def test_knowledge_new_update_and_contradiction_notify(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        save_state({"last_checked_ts": 10}, self.state_file)

        new_result = self.monitor(
            FakeDB([msg(11, "Claude Code 发布新功能")]),
            lambda *_: self._knowledge_decision(),
        ).check_once()
        self.assertEqual(new_result["status"], "notified")
        self.assertEqual(new_result["relation"], "new")
        self.assertTrue(os.path.exists(new_result["hit_path"]))
        self.assertTrue(os.path.exists(new_result["knowledge_path"]))

        save_state({"last_checked_ts": 11}, self.state_file)
        update_result = self.monitor(
            FakeDB([msg(12, "Claude Code 新功能补了链接")]),
            lambda *_: self._knowledge_decision(
                summary="1. 【00:12】成员补充了 Claude Code 新功能链接。",
                key_facts=["成员补充了 Claude Code 新功能链接"],
                links=["https://example.com/codex"],
            ),
            relation_evaluator=lambda *_: {"relation": "update"},
        ).check_once()
        self.assertEqual(update_result["status"], "notified")
        self.assertEqual(update_result["relation"], "update")
        self.assertTrue(update_result["title"].startswith("新线索:"))

        save_state({"last_checked_ts": 12}, self.state_file)
        contradiction_result = self.monitor(
            FakeDB([msg(13, "刚才那个 Claude Code 新功能截图是假的")]),
            lambda *_: self._knowledge_decision(
                title="Claude Code 新功能截图被辟谣",
                summary="1. 【00:13】成员指出刚才的新功能截图是假的。",
                key_facts=["新功能截图被指出是假的"],
                status_hint="disputed",
            ),
            relation_evaluator=lambda *_: {"relation": "contradiction"},
        ).check_once()
        self.assertEqual(contradiction_result["status"], "notified")
        self.assertEqual(contradiction_result["relation"], "contradiction")
        self.assertTrue(contradiction_result["title"].startswith("反转/辟谣:"))

    def test_knowledge_dry_run_does_not_write_db_or_markdown(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        self.config["monitor_interval_minutes"] = 999
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 发布新功能")])

        result = self.monitor(
            db,
            lambda *_: self._knowledge_decision(),
            knowledge_store=KnowledgeStore(
                self.config["monitor_knowledge_db"],
                self.config["monitor_obsidian_root"],
                read_only=True,
            ),
        ).check_once(dry_run=True)

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["relation"], "new")
        self.assertFalse(os.path.exists(self.config["monitor_knowledge_db"]))
        self.assertFalse(os.path.exists(self.config["monitor_obsidian_root"]))
        self.assertEqual(load_state(self.state_file)["last_checked_ts"], 10)

    def test_raw_message_links_are_saved_when_model_omits_links(self):
        self.config["monitor_knowledge_enabled"] = True
        self.config["monitor_knowledge_db"] = os.path.join(self.tmp.name, "knowledge.db")
        self.config["monitor_obsidian_root"] = os.path.join(self.tmp.name, "obsidian")
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 新功能来了 https://example.com/codex?from=group。")])

        result = self.monitor(
            db,
            lambda *_: self._knowledge_decision(
                summary="1. 【00:11】成员提到 Claude Code 新功能，值得看。",
                links=[],
            ),
        ).check_once()

        self.assertEqual(result["status"], "notified")
        with open(result["knowledge_path"], encoding="utf-8") as f:
            markdown = f.read()
        self.assertIn("https://example.com/codex?from=group", markdown)

    def test_prompt_keeps_ai_interaction_and_multiple_candidate_guidance(self):
        monitor = self.monitor(FakeDB([]), lambda *_: {"match": False})
        messages = [msg(11, "4.8 做互动问卷时加载了相关技能，并给出了偏好测试结果")]
        prompt = monitor._build_prompt(
            messages,
            "2026-05-29 00:11 成员: 4.8 做互动问卷时加载了相关技能，并给出了偏好测试结果",
            self.config["monitor_topic"],
        )

        self.assertIn("多个候选都达到通知门槛", prompt)
        self.assertIn("AI/agent/模型互动实验", prompt)
        self.assertIn("模型行为边界或偏好反馈", prompt)
        self.assertIn("不要按单个敏感词字面过滤或命中", prompt)

    def test_link_preview_context_is_included_in_monitor_prompt(self):
        save_state({"last_checked_ts": 10}, self.state_file)
        db = FakeDB([msg(11, "Claude Code 新功能介绍 https://example.com/codex")])
        seen_prompt = []

        monitor = TopicMonitor(
            db,
            self.config,
            state_file=self.state_file,
            hits_dir=self.hits_dir,
            ai_evaluator=lambda prompt, *_: seen_prompt.append(prompt) or {"match": False},
            link_preview_fetcher=lambda url: {
                "url": url,
                "status": "ok",
                "title": "Claude Code 新功能说明",
                "summary": "介绍了一个可以启发实际项目的功能更新。",
            },
            now_func=lambda: 1000,
        )

        result = monitor.check_once()

        self.assertEqual(result["status"], "no_match")
        self.assertIn("<link_context>", seen_prompt[0])
        self.assertIn("Claude Code 新功能说明", seen_prompt[0])
        self.assertIn("介绍了一个可以启发实际项目的功能更新", seen_prompt[0])

    def test_wechat_record_link_is_marked_unavailable_without_network_guessing(self):
        preview = fetch_link_preview(
            "https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate"
            "?t=page/favorite_record__w_unsupport&from=singlemessage"
        )

        self.assertEqual(preview["status"], "unavailable")
        self.assertIn("无法读取被转发的聊天记录正文", preview["summary"])

    def _knowledge_decision(self, **overrides):
        data = {
            "match": True,
            "score": 92,
            "title": "Claude Code 新功能",
            "digest": "1. 【00:11】成员提到 Claude Code 发布新功能，值得看。",
            "topic_key": "claude-code-new-feature",
            "category": "工具更新",
            "entities": ["Claude Code"],
            "key_facts": ["Claude Code 发布新功能"],
            "links": [],
            "event_type": "release",
            "status_hint": "tracking",
        }
        if "summary" in overrides:
            summary = overrides["summary"]
            overrides["digest"] = summary
        data.update(overrides)
        return data


class WeChatMessageCleanTests(unittest.TestCase):
    def test_appmsg_link_keeps_url(self):
        raw = (
            "<msg><appmsg><type>5</type>"
            "<title><![CDATA[Claude Code 新功能说明]]></title>"
            "<url><![CDATA[https://example.com/codex?x=1&y=2]]></url>"
            "</appmsg></msg>"
        )

        cleaned = _clean_msg_text(raw)

        self.assertEqual(
            cleaned,
            "[链接] Claude Code 新功能说明 https://example.com/codex?x=1&y=2",
        )

    def test_appmsg_link_keeps_escaped_url(self):
        raw = (
            "<msg><appmsg><type>5</type>"
            "<title>Claude Code 新功能说明</title>"
            "<url>https://example.com/codex?x=1&amp;y=2</url>"
            "</appmsg></msg>"
        )

        cleaned = _clean_msg_text(raw)

        self.assertIn("https://example.com/codex?x=1&y=2", cleaned)

    def test_forwarded_chat_record_extracts_embedded_items(self):
        raw = """<msg><appmsg>
<title>群聊的聊天记录</title>
<des>盏:[文件] bdsmtest_long.zip</des>
<type>19</type>
<url>https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate?t=page/favorite_record__w_unsupport</url>
<recorditem><![CDATA[<recordinfo>
<title>群聊的聊天记录</title>
<datalist count="3">
<dataitem datatype="8"><sourcename>盏</sourcename><sourcetime>2026-6-3 凌晨5:36</sourcetime><datatitle>bdsmtest_long.zip</datatitle><datafmt>zip</datafmt></dataitem>
<dataitem datatype="1"><sourcename>盏</sourcename><sourcetime>2026-6-3 凌晨5:37</sourcetime><datadesc>机做的</datadesc></dataitem>
<dataitem datatype="1"><sourcename>盏</sourcename><sourcetime>2026-6-3 凌晨5:37</sourcetime><datadesc>https://bdsmtest.org/questions</datadesc></dataitem>
</datalist>
</recordinfo>]]></recorditem>
</appmsg></msg>"""

        cleaned = _clean_msg_text(raw)

        self.assertIn("[聊天记录] 群聊的聊天记录", cleaned)
        self.assertIn("[文件] bdsmtest_long.zip", cleaned)
        self.assertIn("机做的", cleaned)
        self.assertIn("https://bdsmtest.org/questions", cleaned)
        self.assertNotIn("favorite_record__w_unsupport", cleaned)

    def test_quoted_forwarded_chat_record_extracts_refermsg_record(self):
        raw = """<msg><appmsg>
<title>这里是测试</title>
<type>57</type>
<refermsg><content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;盏的聊天记录&lt;/title&gt;&lt;type&gt;19&lt;/type&gt;&lt;recorditem&gt;&lt;![CDATA[&lt;recordinfo&gt;&lt;desc&gt;盏: [文件] test.zip
盏: https://example.com/questions&lt;/desc&gt;&lt;datalist count="0"/&gt;&lt;/recordinfo&gt;]]&gt;&lt;/recorditem&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg>
</appmsg></msg>"""

        cleaned = _clean_msg_text(raw)

        self.assertIn("[回复] 这里是测试", cleaned)
        self.assertIn("[聊天记录] 聊天记录", cleaned)
        self.assertIn("盏: [文件] test.zip", cleaned)
        self.assertIn("https://example.com/questions", cleaned)


if __name__ == "__main__":
    unittest.main()
