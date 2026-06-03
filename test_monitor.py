import os
import tempfile
import unittest

from core.monitor import TopicMonitor, load_state, save_state
from core.knowledge import KnowledgeStore


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


if __name__ == "__main__":
    unittest.main()
