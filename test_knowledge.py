import json
import os
import sqlite3
import tempfile
import unittest

from core.knowledge import (
    OBSIDIAN_CATEGORY_INDEX_FILENAME,
    KnowledgeStore,
    ensure_obsidian_vault,
    safe_path_part,
)


def msg(ts, sender, text):
    return {
        "timestamp": ts,
        "time_str": f"2026-05-29 03:{ts:02d}",
        "sender": sender,
        "text": text,
    }


def candidate(**overrides):
    data = {
        "title": "Claude 4.8 发布传闻",
        "summary": "1. 【03:16】群里提到 Claude 4.8 可能今天发布。",
        "topic_key": "claude-4.8-release-rumor",
        "category": "AI模型",
        "entities": ["Claude", "Opus"],
        "key_facts": ["群里认为 Claude 4.8 可能今天发布"],
        "links": ["https://example.com/claude48"],
        "event_type": "rumor",
        "status_hint": "rumor",
    }
    data.update(overrides)
    return data


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "knowledge.db")
        self.obsidian_root = os.path.join(self.tmp.name, "obsidian")
        self.config = {"monitor_chat_display_name": "Claude恋爱技术群"}
        self.messages = [
            msg(16, "蛋", "X 上都在传 Claude 4.8 要发了"),
            msg(17, "Ruller", "感觉今天概率很高"),
        ]
        self.store = KnowledgeStore(
            self.db_path,
            self.obsidian_root,
            now_func=lambda: 1000,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, table):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        finally:
            conn.close()

    def test_new_topic_creates_sqlite_topic_and_markdown(self):
        result = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})

        topics = self.rows("topics")
        events = self.rows("events")

        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["title"], "Claude 4.8 发布传闻")
        self.assertEqual(json.loads(topics[0]["entities_json"]), ["Claude", "Opus"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["relation"], "new")
        self.assertTrue(os.path.exists(result["knowledge_path"]))
        with open(result["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        basename = os.path.basename(result["knowledge_path"])
        self.assertTrue(basename.startswith("2026-05-29 03-16 "))
        self.assertIn("category: \"AI模型\"", md)
        self.assertIn("# 2026-05-29 03:16 · Claude 4.8 发布传闻", md)
        self.assertIn("## 当前摘要", md)
        self.assertIn("## 时间线", md)
        self.assertIn("Claude 4.8 可能今天发布", md)

    def test_ensure_obsidian_vault_seeds_default_files_without_overwrite(self):
        seeded = ensure_obsidian_vault(self.obsidian_root, include_app_config=True)

        self.assertIn(".obsidian/app.json", seeded["created"])
        self.assertTrue(os.path.exists(os.path.join(self.obsidian_root, ".obsidian", "app.json")))
        self.assertTrue(os.path.exists(os.path.join(self.obsidian_root, "首页.md")))
        self.assertTrue(os.path.exists(
            os.path.join(self.obsidian_root, "关注推送", "AI模型", OBSIDIAN_CATEGORY_INDEX_FILENAME)
        ))

        home = os.path.join(self.obsidian_root, "首页.md")
        with open(home, "w", encoding="utf-8") as f:
            f.write("# custom\n")

        seeded_again = ensure_obsidian_vault(self.obsidian_root, include_app_config=True)

        self.assertEqual(seeded_again["created"], [])
        with open(home, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# custom\n")

    def test_ensure_obsidian_vault_removes_generated_legacy_category_index_only(self):
        monitor_root = os.path.join(self.obsidian_root, "关注推送")
        os.makedirs(monitor_root, exist_ok=True)
        generated_legacy = os.path.join(monitor_root, "工具更新.md")
        custom_legacy = os.path.join(monitor_root, "AI模型.md")
        with open(generated_legacy, "w", encoding="utf-8") as f:
            f.write('# 工具更新\n\n```query\npath:"关注推送/工具更新"\n```\n')
        with open(custom_legacy, "w", encoding="utf-8") as f:
            f.write("# AI模型\n\n我自己写的内容\n")

        ensure_obsidian_vault(self.obsidian_root, include_app_config=True)

        self.assertFalse(os.path.exists(generated_legacy))
        self.assertTrue(os.path.exists(custom_legacy))
        self.assertTrue(os.path.exists(
            os.path.join(self.obsidian_root, "关注推送", "工具更新", OBSIDIAN_CATEGORY_INDEX_FILENAME)
        ))

    def test_ensure_obsidian_vault_migrates_generated_home_links(self):
        os.makedirs(self.obsidian_root, exist_ok=True)
        home = os.path.join(self.obsidian_root, "首页.md")
        with open(home, "w", encoding="utf-8") as f:
            f.write("# 微信关注推送知识库\n\n- [[关注推送/AI模型]]\n- [[关注推送/工具更新]]\n")

        ensure_obsidian_vault(self.obsidian_root, include_app_config=True)

        with open(home, encoding="utf-8") as f:
            md = f.read()
        self.assertIn("[[关注推送/AI模型/目录|AI模型]]", md)
        self.assertIn("[[关注推送/工具更新/目录|工具更新]]", md)
        self.assertNotIn("[[关注推送/AI模型]]", md)

    def test_custom_obsidian_vault_only_creates_monitor_subdir_by_default(self):
        custom_root = os.path.join(self.tmp.name, "custom-vault")

        ensure_obsidian_vault(custom_root)

        self.assertTrue(os.path.isdir(os.path.join(custom_root, "关注推送")))
        self.assertFalse(os.path.exists(os.path.join(custom_root, ".obsidian", "app.json")))
        self.assertFalse(os.path.exists(os.path.join(custom_root, "首页.md")))

    def test_duplicate_event_only_records_event(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_id = first["topic_id"]

        self.store.apply_event(
            candidate(summary="1. 【03:18】大家又重复讨论 4.8 传闻。"),
            self.messages,
            self.config,
            {"relation": "duplicate", "target_topic_id": topic_id},
        )

        topics = self.rows("topics")
        events = self.rows("events")

        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["event_count"], 1)
        self.assertEqual([e["relation"] for e in events], ["new", "duplicate"])

    def test_update_appends_timeline_and_writes_updates_relation(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_id = first["topic_id"]

        updated = self.store.apply_event(
            candidate(
                summary="1. 【03:21】有人贴出性能曝光链接，传闻有了新来源。",
                key_facts=["有人贴出 Claude 4.8 性能曝光链接"],
                links=["https://example.com/claude48", "https://example.com/benchmark"],
            ),
            self.messages,
            self.config,
            {"relation": "update", "target_topic_id": topic_id, "reason": "新增链接"},
        )

        topic = self.rows("topics")[0]
        relations = self.rows("relations")
        self.assertEqual(topic["event_count"], 2)
        self.assertIn("性能曝光链接", "\n".join(json.loads(topic["key_facts_json"])))
        self.assertEqual(relations[0]["relation"], "updates")
        with open(updated["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn("新线索", md)
        self.assertIn("https://example.com/benchmark", md)

    def test_contradiction_marks_topic_disputed(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_id = first["topic_id"]

        self.store.apply_event(
            candidate(
                title="Claude 4.8 发布图被辟谣",
                summary="1. 【03:30】夏希指出流传图片是网友假想，不是官方图。",
                key_facts=["流传图片被指出是网友假想"],
                status_hint="disputed",
            ),
            self.messages,
            self.config,
            {"relation": "contradiction", "target_topic_id": topic_id, "reason": "图片被辟谣"},
        )

        topic = self.rows("topics")[0]
        relations = self.rows("relations")
        self.assertEqual(topic["status"], "disputed")
        self.assertEqual(relations[0]["relation"], "contradicts")
        self.assertIn("群里认为 Claude 4.8 可能今天发布", json.loads(topic["key_facts_json"]))

    def test_new_topic_links_related_existing_topics(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_a = first["topic_id"]

        second = self.store.apply_event(
            candidate(
                title="Claude 4.8 上下文长度讨论",
                topic_key="claude-4.8-context-length",
                summary="1. 【03:40】群里讨论 Claude 4.8 的上下文长度。",
            ),
            self.messages,
            self.config,
            {"relation": "new", "related_topic_ids": [topic_a]},
        )

        related = [r for r in self.rows("relations") if r["relation"] == "related"]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["source_topic_id"], second["topic_id"])
        self.assertEqual(related[0]["target_topic_id"], topic_a)

        with open(second["knowledge_path"], encoding="utf-8") as f:
            md = f.read()
        self.assertIn("## 相关主题", md)
        self.assertIn(
            "[[关注推送/AI模型/2026-05-29 03-16 Claude 4.8 发布传闻|Claude 4.8 发布传闻]]",
            md,
        )
        self.assertIn("event_count:", md)

    def test_link_related_skips_missing_and_self_ids(self):
        first = self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        topic_a = first["topic_id"]

        second = self.store.apply_event(
            candidate(title="无关主题", topic_key="unrelated"),
            self.messages,
            self.config,
            {"relation": "new", "related_topic_ids": [999999, topic_a]},
        )

        related = [
            r for r in self.rows("relations")
            if r["relation"] == "related" and r["source_topic_id"] == second["topic_id"]
        ]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["target_topic_id"], topic_a)

    def test_find_candidates_prefers_recent_same_chat_continuation(self):
        self.store.apply_event(
            candidate(
                title="Claude 4.8 大版本体验讨论",
                topic_key="claude-4.8-broad",
                summary="1. 【03:05】群里泛聊 Claude 4.8 的模型体验。",
                entities=["Claude", "4.8", "局外猫"],
                key_facts=["Claude 4.8 的模型体验被多次讨论"],
                links=[],
            ),
            [msg(5, "局外猫", "4.8 体验挺乱的")],
            self.config,
            {"relation": "new"},
        )
        recent = self.store.apply_event(
            candidate(
                title="Claude缓存与工具调用优化讨论",
                topic_key="claude-cache-tool-impact",
                summary="1. 【03:19】群友提到工具调用会破坏 Claude API 缓存，建议放在断点后。",
                entities=["Claude", "Taiyaki", "兔蛙旋转"],
                key_facts=[
                    "调用MCP工具会降低Claude API缓存命中率",
                    "将工具说明放在断点后可避免缓存破坏",
                ],
                links=[],
            ),
            [
                msg(19, "Taiyaki", "工具说明好多"),
                msg(20, "兔蛙旋转", "放在断点后面也不行吗"),
            ],
            self.config,
            {"relation": "new"},
        )

        candidates = self.store.find_candidates(
            candidate(
                title="群友讨论Claude显式断点与4.8缓存优化",
                topic_key="explicit-breakpoint-cache",
                summary="1. 【03:22】兔蛙旋转补充变化 block 放在断点后，role 需要是 user。",
                entities=["Claude", "4.8", "Taiyaki", "兔蛙旋转"],
                key_facts=[
                    "显式断点可以放在变化block前，保持前缀稳定",
                    "变化block放在断点后且role需为user",
                ],
                links=[],
                source_chat="Claude恋爱技术群",
                window_start="2026-05-29 03:22",
            ),
            limit=3,
        )

        self.assertEqual(candidates[0]["topic_id"], recent["topic_id"])

    def test_run_maintenance_merges_duplicate_topics(self):
        self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        self.store.apply_event(
            candidate(
                title="Claude 4.8 今天发布?",
                summary="1. 【03:25】又有人说 4.8 今天发。",
                key_facts=["有人称今天发布"],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        self.assertEqual(len(self.rows("topics")), 2)

        result = self.store.run_maintenance()

        self.assertEqual(result["group_count"], 1)
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["reexport_count"], 1)

        topics = self.rows("topics")
        events = self.rows("events")
        self.assertEqual(len(topics), 1)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["topic_id"] == topics[0]["topic_id"] for e in events))
        facts = json.loads(topics[0]["key_facts_json"])
        self.assertIn("有人称今天发布", facts)

    def test_run_maintenance_dry_run_reports_without_changing(self):
        self.store.apply_event(candidate(), self.messages, self.config, {"relation": "new"})
        self.store.apply_event(
            candidate(title="Claude 4.8 今天发布?", summary="重复"),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        result = self.store.run_maintenance(dry_run=True)

        self.assertEqual(result["group_count"], 1)
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(len(self.rows("topics")), 2)

    def test_run_maintenance_merges_category_folders(self):
        technique = self.store.apply_event(
            candidate(
                title="Claude 4.8 思考链提取技巧",
                topic_key="claude-48-thinking-tips",
                category="AI产品技巧",
                summary="1. 【03:16】群里分享 Claude 4.8 思考链提取技巧。",
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        tool = self.store.apply_event(
            candidate(
                title="自建 app 新功能讨论",
                topic_key="self-app-feature",
                category="自建app新功能",
                summary="1. 【03:20】群里讨论自建 app 的新功能。",
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        def move_to_legacy_folder(result, legacy_category):
            current_path = result["knowledge_path"]
            legacy_rel = os.path.join(
                "关注推送", legacy_category, os.path.basename(current_path),
            )
            legacy_path = os.path.join(self.obsidian_root, legacy_rel)
            os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
            os.rename(current_path, legacy_path)
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "UPDATE topics SET category = ?, obsidian_path = ? WHERE topic_id = ?",
                    (legacy_category, legacy_rel, result["topic_id"]),
                )
                conn.commit()
            finally:
                conn.close()
            return legacy_path

        old_technique_path = move_to_legacy_folder(technique, "AI产品技巧")
        old_tool_path = move_to_legacy_folder(tool, "自建app新功能")
        plan = self.store.run_maintenance(dry_run=True)
        self.assertEqual(plan["category_change_count"], 2)
        self.assertTrue(os.path.exists(old_technique_path))
        self.assertTrue(os.path.exists(old_tool_path))

        result = self.store.run_maintenance()

        self.assertEqual(result["category_change_count"], 2)
        self.assertFalse(os.path.exists(old_technique_path))
        self.assertFalse(os.path.exists(old_tool_path))
        topics = {t["topic_key"]: t for t in self.store.list_topics()}
        self.assertEqual(topics["claude-48-thinking-tips"]["category"], "技术方法")
        self.assertEqual(topics["self-app-feature"]["category"], "自建app")
        self.assertIn(os.path.join("关注推送", "技术方法"), topics["claude-48-thinking-tips"]["obsidian_path"])
        self.assertIn(os.path.join("关注推送", "自建app"), topics["self-app-feature"]["obsidian_path"])
        self.assertIn("2026-05-29 03-16", topics["claude-48-thinking-tips"]["obsidian_path"])
        self.assertIn("2026-05-29 03-16", topics["self-app-feature"]["obsidian_path"])
        self.assertEqual(result["removed_empty_dirs"], 2)

    def test_maintenance_does_not_merge_broadly_related_ai_topics(self):
        self.store.apply_event(
            candidate(
                title="Claude 4.8 思考链提取技巧讨论",
                topic_key="claude-48-thinking-chain",
                summary="1. 【03:16】群里讨论 Claude 4.8 的思考链提取方法。",
                entities=["Claude", "4.8"],
                key_facts=["群里讨论 Claude 4.8 思考链提取方法"],
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )
        self.store.apply_event(
            candidate(
                title="Claude 4.6 vs 4.8 实际体验讨论",
                topic_key="claude-46-vs-48-experience",
                summary="1. 【03:20】群里比较 Claude 4.6 和 4.8 的实际体验。",
                entities=["Claude", "4.8"],
                key_facts=["群里比较 Claude 4.6 和 4.8 的实际体验"],
                links=[],
            ),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        result = self.store.run_maintenance(dry_run=True)

        self.assertEqual(result["group_count"], 0)
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["reexport_count"], 2)

    def test_safe_filename_handles_chinese_emoji_slash_and_long_title(self):
        unsafe = "Claude/Opus: 4.8 🚀 " + "很长" * 60
        result = self.store.apply_event(
            candidate(title=unsafe, category="AI/模型🚀"),
            self.messages,
            self.config,
            {"relation": "new"},
        )

        basename = os.path.basename(result["knowledge_path"])
        self.assertNotIn("/", basename)
        self.assertNotIn(":", basename)
        self.assertNotIn("🚀", basename)
        self.assertLessEqual(len(safe_path_part(unsafe, max_len=90)), 90)
        self.assertTrue(os.path.exists(result["knowledge_path"]))


if __name__ == "__main__":
    unittest.main()
