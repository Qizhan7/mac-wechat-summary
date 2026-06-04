"""Local knowledge base for monitor hits and Obsidian-friendly Markdown."""
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime

from .config import DATA_DIR

KNOWLEDGE_DB = os.path.join(DATA_DIR, "monitor_knowledge.db")
OBSIDIAN_ROOT = os.path.join(DATA_DIR, "obsidian_knowledge")
OBSIDIAN_SUBDIR = "关注推送"

RELATION_NOTIFY = {"new", "update", "contradiction"}
RELATION_LABELS = {
    "new": "新主题",
    "duplicate": "重复出现",
    "update": "新线索",
    "contradiction": "反转/辟谣",
}

CATEGORY_ALIASES = (
    ("群内八卦", ("八卦", "搞笑", "吃瓜", "瓜", "轶事")),
    ("自建app", ("自建app", "自建 app", "app新功能", "app 新功能")),
    ("设计讨论", ("设计", "自主权", "自主性", "agent")),
    ("技术方法", ("教程", "技巧", "方法", "配置", "资源", "实践")),
    ("工具更新", ("工具", "产品", "应用", "新功能", "更新")),
    ("AI实验", ("实验", "报告", "测评", "修复")),
    ("AI模型", ("模型", "安全", "发布传闻")),
)

OBSIDIAN_INDEX_CATEGORIES = (
    "AI模型",
    "工具更新",
    "技术方法",
    "AI实验",
    "自建app",
    "设计讨论",
    "群内八卦",
)
OBSIDIAN_CATEGORY_INDEX_FILENAME = "目录.md"
OBSIDIAN_HOME_LINKS = tuple(
    (
        f"[[{OBSIDIAN_SUBDIR}/{category}]]",
        f"[[{OBSIDIAN_SUBDIR}/{category}/目录|{category}]]",
    )
    for category in OBSIDIAN_INDEX_CATEGORIES
)

OBSIDIAN_APP_CONFIG = {
    "alwaysUpdateLinks": True,
    "attachmentFolderPath": "附件",
    "newFileLocation": "current",
    "promptDelete": False,
    "showInlineTitle": True,
    "spellcheck": False,
    "livePreview": True,
}

OBSIDIAN_CORE_PLUGINS = {
    "file-explorer": True,
    "global-search": True,
    "switcher": True,
    "graph": True,
    "backlink": True,
    "outgoing-link": True,
    "tag-pane": True,
    "page-preview": True,
    "daily-notes": False,
    "templates": False,
    "note-composer": False,
    "command-palette": True,
    "slash-command": False,
    "editor-status": False,
    "markdown-importer": False,
    "zk-prefixer": False,
    "random-note": False,
    "outline": True,
    "word-count": False,
    "slides": False,
    "audio-recorder": False,
    "workspaces": False,
    "file-recovery": True,
    "publish": False,
    "sync": False,
    "canvas": True,
    "footnotes": False,
    "properties": True,
    "bookmarks": True,
    "bases": True,
    "webviewer": False,
}

OBSIDIAN_APPEARANCE_CONFIG = {
    "accentColor": "#2563eb",
    "baseFontSize": 15,
    "nativeMenus": True,
    "showViewHeader": True,
}

OBSIDIAN_HOME_NOTE = """# 微信关注推送知识库

这里是微信关注推送自动沉淀的 Obsidian vault。新推送会继续写入 `关注推送/`，文件名和标题都带日期时间。

## 快速入口

- [[关注推送/AI模型/目录|AI模型]]
- [[关注推送/工具更新/目录|工具更新]]
- [[关注推送/技术方法/目录|技术方法]]
- [[关注推送/AI实验/目录|AI实验]]
- [[关注推送/自建app/目录|自建app]]
- [[关注推送/设计讨论/目录|设计讨论]]
- [[关注推送/群内八卦/目录|群内八卦]]

## 常用搜索

```query
path:"关注推送" link
```

```query
path:"关注推送" 新功能 OR 更新 OR 教程 OR 实验 OR 修复
```

## 用法

- 左侧文件夹：按分类看每条推送。
- 顶部搜索：搜产品名、模型名、链接域名或关键词。
- 右侧反向链接/出链：看这条内容和哪些条目有关。
- Graph：点左侧网络图标，看话题之间的连接。
"""


def _json_dumps(value):
    return json.dumps(value or [], ensure_ascii=False)


def _json_loads(value, default=None):
    try:
        data = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return [] if default is None else default
    return data if data is not None else ([] if default is None else default)


def _normalize_list(value, limit=12):
    if value is None:
        return []
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


def safe_path_part(value, fallback="未分类", max_len=80):
    """Make a filesystem-safe but readable path part."""
    text = str(value or "").strip()
    chars = []
    for ch in text:
        if ch in '<>:"/\\|?*':
            chars.append(" ")
            continue
        category = unicodedata.category(ch)
        if category[0] in {"L", "N"} or ch in {" ", "-", "_", ".", "·", "（", "）", "(", ")"}:
            chars.append(ch)
        else:
            chars.append(" ")

    cleaned = re.sub(r"\s+", " ", "".join(chars)).strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len].rstrip(" .") or fallback


def _frontmatter_scalar(value):
    text = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _frontmatter_list(name, values):
    values = _normalize_list(values)
    if not values:
        return f"{name}: []"
    lines = [f"{name}:"]
    lines.extend(f"  - {_frontmatter_scalar(v)}" for v in values)
    return "\n".join(lines)


def _truncate(value, limit):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _note_time(value):
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", text)
    if match:
        return match.group(0)
    return text[:16]


def _path_time(value):
    return _note_time(value).replace(":", "-")


def _note_heading(topic):
    when = _note_time(topic.get("first_seen", ""))
    title = topic["title"]
    return f"{when} · {title}" if when else title


def _obsidian_link(obsidian_path, title):
    if not obsidian_path:
        return f"[[{title}]]"
    target = os.path.splitext(obsidian_path)[0]
    return f"[[{target}|{title}]]"


def _is_default_obsidian_root(path):
    return os.path.abspath(os.path.expanduser(path or "")) == os.path.abspath(OBSIDIAN_ROOT)


def _write_json_if_missing(path, data):
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return True


def _write_text_if_missing(path, text):
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")
    return True


def _write_or_migrate_home_note(path):
    if not os.path.exists(path):
        return _write_text_if_missing(path, OBSIDIAN_HOME_NOTE)

    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        return False

    if not current.startswith("# 微信关注推送知识库"):
        return False

    updated = current
    for old_link, new_link in OBSIDIAN_HOME_LINKS:
        updated = updated.replace(f"- {old_link}", f"- {new_link}")
    if updated == current:
        return False

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated)
        return True
    except OSError:
        return False


def _category_index_text(category):
    return f"""# {category}

```query
path:"{OBSIDIAN_SUBDIR}/{category}"
```
"""


def _remove_generated_legacy_category_index(root, category):
    legacy_rel_path = os.path.join(OBSIDIAN_SUBDIR, f"{safe_path_part(category)}.md")
    legacy_path = os.path.join(root, legacy_rel_path)
    if not os.path.isfile(legacy_path):
        return False

    try:
        with open(legacy_path, encoding="utf-8") as f:
            current = f.read().strip()
    except OSError:
        return False

    if current != _category_index_text(category).strip():
        return False

    try:
        os.remove(legacy_path)
        return True
    except OSError:
        return False


def ensure_obsidian_vault(obsidian_root=OBSIDIAN_ROOT, include_app_config=None):
    """Create the app-owned Obsidian vault shell without touching custom vault UI."""
    root = os.path.expanduser(obsidian_root or OBSIDIAN_ROOT)
    created = []

    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, OBSIDIAN_SUBDIR), exist_ok=True)

    if include_app_config is None:
        include_app_config = _is_default_obsidian_root(root)
    if not include_app_config:
        return {"root": root, "created": created}

    obsidian_dir = os.path.join(root, ".obsidian")
    if _write_json_if_missing(os.path.join(obsidian_dir, "app.json"), OBSIDIAN_APP_CONFIG):
        created.append(".obsidian/app.json")
    if _write_json_if_missing(os.path.join(obsidian_dir, "core-plugins.json"), OBSIDIAN_CORE_PLUGINS):
        created.append(".obsidian/core-plugins.json")
    if _write_json_if_missing(os.path.join(obsidian_dir, "appearance.json"), OBSIDIAN_APPEARANCE_CONFIG):
        created.append(".obsidian/appearance.json")
    if _write_or_migrate_home_note(os.path.join(root, "首页.md")):
        created.append("首页.md")

    for category in OBSIDIAN_INDEX_CATEGORIES:
        rel_path = os.path.join(
            OBSIDIAN_SUBDIR,
            safe_path_part(category),
            OBSIDIAN_CATEGORY_INDEX_FILENAME,
        )
        text = _category_index_text(category)
        if _write_text_if_missing(os.path.join(root, rel_path), text):
            created.append(rel_path)
        _remove_generated_legacy_category_index(root, category)

    return {"root": root, "created": created}


def build_message_hash(messages):
    h = hashlib.sha256()
    for msg in messages:
        for key in ("timestamp", "sender", "text", "content"):
            h.update(str(msg.get(key, "")).encode("utf-8", errors="ignore"))
            h.update(b"\0")
    return h.hexdigest()


def message_excerpt(messages, limit=8):
    lines = []
    for msg in messages[:limit]:
        time_text = msg.get("time_str") or ""
        sender = msg.get("sender") or msg.get("group_nickname") or ""
        text = msg.get("text") or msg.get("content") or ""
        lines.append(_truncate(f"[{time_text}] {sender}: {text}", 240))
    if len(messages) > limit:
        lines.append(f"... 另有 {len(messages) - limit} 条")
    return "\n".join(lines)


def event_context(messages, config):
    senders = []
    seen = set()
    for msg in messages:
        sender = str(msg.get("sender") or msg.get("group_nickname") or "").strip()
        if sender and sender not in seen:
            seen.add(sender)
            senders.append(sender[:80])
        if len(senders) >= 12:
            break

    return {
        "source_chat": config.get("monitor_chat_display_name", "监控群聊"),
        "window_start": messages[0].get("time_str", "") if messages else "",
        "window_end": messages[-1].get("time_str", "") if messages else "",
        "senders": senders,
        "message_hash": build_message_hash(messages),
        "messages_excerpt": message_excerpt(messages),
    }


class KnowledgeStore:
    """SQLite-backed monitor knowledge base with Markdown mirror output."""

    def __init__(self, db_path=KNOWLEDGE_DB, obsidian_root=OBSIDIAN_ROOT, now_func=time.time, read_only=False):
        self.db_path = os.path.expanduser(db_path)
        self.obsidian_root = os.path.expanduser(obsidian_root)
        self.now_func = now_func
        self.read_only = read_only

    @classmethod
    def from_config(cls, config, now_func=time.time, read_only=False):
        return cls(
            config.get("monitor_knowledge_db") or KNOWLEDGE_DB,
            config.get("monitor_obsidian_root") or OBSIDIAN_ROOT,
            now_func=now_func,
            read_only=read_only,
        )

    def connect(self):
        if self.read_only and not os.path.exists(self.db_path):
            return None
        if not self.read_only:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if not self.read_only:
            self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS topics (
                topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_key TEXT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                entities_json TEXT NOT NULL,
                key_facts_json TEXT NOT NULL,
                links_json TEXT NOT NULL,
                source_chat TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                obsidian_path TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_topics_topic_key ON topics(topic_key);
            CREATE INDEX IF NOT EXISTS idx_topics_last_seen ON topics(last_seen);

            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL,
                relation TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                category TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status_hint TEXT NOT NULL,
                source_chat TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                senders_json TEXT NOT NULL,
                links_json TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                messages_excerpt TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(topic_id) REFERENCES topics(topic_id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_topic_id ON events(topic_id);
            CREATE INDEX IF NOT EXISTS idx_events_message_hash ON events(message_hash);

            CREATE TABLE IF NOT EXISTS relations (
                relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_topic_id INTEGER NOT NULL,
                target_topic_id INTEGER NOT NULL,
                relation TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(source_topic_id, target_topic_id, relation)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS topic_fts USING fts5(
                topic_id UNINDEXED,
                title,
                category,
                summary,
                entities,
                key_facts,
                links
            );
            """
        )
        conn.commit()

    def find_candidates(self, candidate, limit=5):
        conn = self.connect()
        if conn is None:
            return []
        try:
            seen = set()
            rows = []

            topic_key = candidate.get("topic_key", "")
            if topic_key:
                for row in conn.execute(
                    "SELECT * FROM topics WHERE topic_key = ? ORDER BY updated_at DESC LIMIT ?",
                    (topic_key, limit),
                ):
                    rows.append(row)
                    seen.add(row["topic_id"])

            fts_query = self._build_fts_query(candidate)
            if fts_query:
                try:
                    for row in conn.execute(
                        """
                        SELECT t.*
                        FROM topic_fts f
                        JOIN topics t ON t.topic_id = f.topic_id
                        WHERE topic_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_query, limit * 2),
                    ):
                        if row["topic_id"] not in seen:
                            rows.append(row)
                            seen.add(row["topic_id"])
                except sqlite3.Error:
                    pass

            for row in conn.execute("SELECT * FROM topics ORDER BY updated_at DESC LIMIT 80"):
                if row["topic_id"] not in seen:
                    rows.append(row)
                    seen.add(row["topic_id"])

            scored = []
            for row in rows:
                score = self._score_candidate(candidate, row)
                if score > 0:
                    scored.append((score, row))

            scored.sort(key=lambda item: item[0], reverse=True)
            return [self._topic_dict(row, score) for score, row in scored[:limit]]
        except sqlite3.Error:
            if self.read_only:
                return []
            raise
        finally:
            conn.close()

    def apply_event(self, candidate, messages, config, relation_decision):
        if self.read_only:
            raise RuntimeError("knowledge store is read-only")

        relation = normalize_relation(relation_decision.get("relation"))
        target_topic_id = relation_decision.get("target_topic_id")
        reason = str(relation_decision.get("reason") or "").strip()
        ctx = event_context(messages, config)
        now = self.now_func()

        conn = self.connect()
        try:
            if relation == "new" or not target_topic_id:
                topic_id = self._create_topic(conn, candidate, ctx, now)
                relation = "new"
            else:
                row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (target_topic_id,)).fetchone()
                if row is None:
                    topic_id = self._create_topic(conn, candidate, ctx, now)
                    relation = "new"
                else:
                    topic_id = int(row["topic_id"])

            event_id = self._insert_event(conn, topic_id, candidate, ctx, relation, now)
            if relation in {"update", "contradiction"}:
                self._update_topic(conn, topic_id, candidate, ctx, relation, now)
                rel_name = "updates" if relation == "update" else "contradicts"
                self._insert_relation(conn, topic_id, topic_id, rel_name, reason, now)
            elif relation == "new":
                self._bump_new_topic_event_count(conn, topic_id, now)
                self._link_related(conn, topic_id, relation_decision, now)
            elif relation == "duplicate":
                self._insert_relation(conn, topic_id, topic_id, "duplicate_of", reason, now)

            conn.commit()
            self._write_topic_markdown(conn, topic_id)
            topic = self.get_topic(topic_id)
            return {
                "relation": relation,
                "topic_id": topic_id,
                "event_id": event_id,
                "obsidian_path": topic.get("obsidian_path", "") if topic else "",
                "knowledge_path": self.full_obsidian_path(topic.get("obsidian_path", "")) if topic else "",
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_topic(self, topic_id):
        conn = self.connect()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
            return self._topic_dict(row) if row else None
        finally:
            conn.close()

    def full_obsidian_path(self, obsidian_path):
        return os.path.join(self.obsidian_root, obsidian_path) if obsidian_path else ""

    def _create_topic(self, conn, candidate, ctx, now):
        now_text = self._now_text(now)
        category = normalize_category(candidate.get("category"))
        status = normalize_status(candidate.get("status_hint") or "tracking")
        title = candidate.get("title") or "关注内容"
        cursor = conn.execute(
            """
            INSERT INTO topics (
                topic_key, title, category, status, summary, entities_json,
                key_facts_json, links_json, source_chat, first_seen, last_seen,
                obsidian_path, event_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, ?)
            """,
            (
                candidate.get("topic_key", ""),
                title,
                category,
                status,
                candidate.get("summary", ""),
                _json_dumps(candidate.get("entities")),
                _json_dumps(candidate.get("key_facts")),
                _json_dumps(candidate.get("links")),
                ctx["source_chat"],
                ctx["window_start"] or now_text,
                ctx["window_end"] or now_text,
                now,
                now,
            ),
        )
        topic_id = int(cursor.lastrowid)
        first_seen = ctx["window_start"] or now_text
        obsidian_path = self._unique_obsidian_path(conn, topic_id, category, title, first_seen=first_seen)
        conn.execute("UPDATE topics SET obsidian_path = ? WHERE topic_id = ?", (obsidian_path, topic_id))
        self._upsert_fts(conn, topic_id)
        return topic_id

    def _update_topic(self, conn, topic_id, candidate, ctx, relation, now):
        row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
        if row is None:
            return
        entities = merge_lists(_json_loads(row["entities_json"]), candidate.get("entities"))
        key_facts = merge_lists(_json_loads(row["key_facts_json"]), candidate.get("key_facts"), limit=40)
        links = merge_lists(_json_loads(row["links_json"]), candidate.get("links"), limit=30)
        status = "disputed" if relation == "contradiction" else normalize_status(candidate.get("status_hint") or row["status"])
        conn.execute(
            """
            UPDATE topics
            SET summary = ?, status = ?, entities_json = ?, key_facts_json = ?,
                links_json = ?, last_seen = ?, event_count = event_count + 1,
                updated_at = ?
            WHERE topic_id = ?
            """,
            (
                candidate.get("summary") or row["summary"],
                status,
                _json_dumps(entities),
                _json_dumps(key_facts),
                _json_dumps(links),
                ctx["window_end"] or self._now_text(now),
                now,
                topic_id,
            ),
        )
        self._upsert_fts(conn, topic_id)

    def _bump_new_topic_event_count(self, conn, topic_id, now):
        conn.execute(
            "UPDATE topics SET event_count = event_count + 1, updated_at = ? WHERE topic_id = ?",
            (now, topic_id),
        )

    def _insert_event(self, conn, topic_id, candidate, ctx, relation, now):
        cursor = conn.execute(
            """
            INSERT INTO events (
                topic_id, relation, title, summary, category, event_type,
                status_hint, source_chat, window_start, window_end, senders_json,
                links_json, message_hash, messages_excerpt, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                topic_id,
                relation,
                candidate.get("title", ""),
                candidate.get("summary", ""),
                normalize_category(candidate.get("category")),
                candidate.get("event_type", ""),
                candidate.get("status_hint", ""),
                ctx["source_chat"],
                ctx["window_start"],
                ctx["window_end"],
                _json_dumps(ctx["senders"]),
                _json_dumps(candidate.get("links")),
                ctx["message_hash"],
                ctx["messages_excerpt"],
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_relation(self, conn, source_topic_id, target_topic_id, relation, reason, now):
        conn.execute(
            """
            INSERT OR IGNORE INTO relations (
                source_topic_id, target_topic_id, relation, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_topic_id, target_topic_id, relation, reason, now),
        )

    def _link_related(self, conn, topic_id, relation_decision, now):
        """Link a freshly created topic to semantically nearby existing topics.

        These cross-topic `related` edges are what populate the "相关主题"
        section and let Obsidian's graph view connect notes; without them the
        relations table only ever held self-loops.
        """
        related_ids = relation_decision.get("related_topic_ids") or []
        seen = set()
        for rid in related_ids:
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue
            if rid == topic_id or rid in seen:
                continue
            seen.add(rid)
            if conn.execute("SELECT 1 FROM topics WHERE topic_id = ?", (rid,)).fetchone() is None:
                continue
            self._insert_relation(conn, topic_id, rid, "related", "语义相邻主题", now)

    def _upsert_fts(self, conn, topic_id):
        row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
        if row is None:
            return
        conn.execute("DELETE FROM topic_fts WHERE topic_id = ?", (topic_id,))
        conn.execute(
            """
            INSERT INTO topic_fts(topic_id, title, category, summary, entities, key_facts, links)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                topic_id,
                row["title"],
                row["category"],
                row["summary"],
                " ".join(_json_loads(row["entities_json"])),
                " ".join(_json_loads(row["key_facts_json"])),
                " ".join(_json_loads(row["links_json"])),
            ),
        )

    def _write_topic_markdown(self, conn, topic_id):
        topic_row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
        if topic_row is None:
            return
        events = conn.execute(
            "SELECT * FROM events WHERE topic_id = ? ORDER BY created_at, event_id",
            (topic_id,),
        ).fetchall()
        relations = conn.execute(
            """
            SELECT r.relation, r.reason, t.title, t.obsidian_path
            FROM relations r
            JOIN topics t ON t.topic_id = r.target_topic_id
            WHERE r.source_topic_id = ?
            ORDER BY r.created_at, r.relation
            """,
            (topic_id,),
        ).fetchall()

        topic = self._topic_dict(topic_row)
        text = self._render_markdown(topic, events, relations)
        ensure_obsidian_vault(self.obsidian_root)
        path = self.full_obsidian_path(topic["obsidian_path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _render_markdown(self, topic, events, relations):
        title = topic["title"]
        entities = topic["entities"]
        links = topic["links"]
        key_facts = topic["key_facts"]
        tags = ["wechat-monitor", safe_path_part(topic["category"], "uncategorized").replace(" ", "-")]

        lines = [
            "---",
            f"category: {_frontmatter_scalar(topic['category'])}",
            f"status: {_frontmatter_scalar(topic['status'])}",
            f"first_seen: {_frontmatter_scalar(topic['first_seen'])}",
            f"last_seen: {_frontmatter_scalar(topic['last_seen'])}",
            f"event_count: {int(topic['event_count'])}",
            f"source_chat: {_frontmatter_scalar(topic['source_chat'])}",
            _frontmatter_list("entities", entities),
            _frontmatter_list("tags", tags),
            "---",
            "",
            f"# {_note_heading(topic)}",
            "",
            "## 当前摘要",
            topic["summary"] or "（暂无摘要）",
            "",
            "## 时间线",
        ]

        for event in events:
            relation = event["relation"]
            label = RELATION_LABELS.get(relation, relation)
            when = event["window_end"] or datetime.fromtimestamp(event["created_at"]).strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {when} · {label}：{event['summary'] or event['title']}")
        if not events:
            lines.append("- （暂无事件）")

        lines.extend(["", "## 关键事实"])
        if key_facts:
            lines.extend(f"- {fact}" for fact in key_facts)
        else:
            lines.append("- （暂无关键事实）")

        lines.extend(["", "## 相关主题"])
        relation_lines = []
        for rel in relations:
            if rel["title"] == title and rel["relation"] in {"updates", "duplicate_of", "contradicts"}:
                continue
            relation_lines.append(
                f"- {rel['relation']}:: {_obsidian_link(rel['obsidian_path'], rel['title'])}"
            )
        if relation_lines:
            lines.extend(relation_lines)
        else:
            lines.append("- （暂无）")

        lines.extend(["", "## 来源记录"])
        for event in events:
            senders = ", ".join(_json_loads(event["senders_json"]))
            event_links = _json_loads(event["links_json"])
            when = event["window_end"] or datetime.fromtimestamp(event["created_at"]).strftime("%Y-%m-%d %H:%M")
            lines.extend([
                f"### {when} · {RELATION_LABELS.get(event['relation'], event['relation'])}",
                f"- 群聊：{event['source_chat']}",
                f"- 时间窗：{event['window_start']} ~ {event['window_end']}",
                f"- 发送者：{senders or '未知'}",
            ])
            if event_links:
                lines.append("- 链接：" + "、".join(event_links))
            lines.extend([
                "",
                event["summary"] or event["title"],
                "",
                "```text",
                event["messages_excerpt"],
                "```",
                "",
            ])

        if links:
            lines.extend(["## 链接", *[f"- {link}" for link in links], ""])

        return "\n".join(lines).rstrip() + "\n"

    def _unique_obsidian_path(self, conn, topic_id, category, title, first_seen="", current_path=""):
        category_part = safe_path_part(category)
        time_part = safe_path_part(_path_time(first_seen), "", max_len=20)
        title_part = safe_path_part(title, "关注内容", max_len=90)
        filename = f"{time_part} {title_part}".strip()
        rel_path = os.path.join(OBSIDIAN_SUBDIR, category_part, f"{filename}.md")
        existing = conn.execute(
            "SELECT topic_id FROM topics WHERE obsidian_path = ? AND topic_id != ?",
            (rel_path, topic_id),
        ).fetchone()
        full_path = self.full_obsidian_path(rel_path)
        if existing is None and (not os.path.exists(full_path) or rel_path == current_path):
            return rel_path
        return os.path.join(OBSIDIAN_SUBDIR, category_part, f"{filename}-{topic_id}.md")

    def _topic_dict(self, row, score=None):
        data = {
            "topic_id": int(row["topic_id"]),
            "topic_key": row["topic_key"],
            "title": row["title"],
            "category": row["category"],
            "status": row["status"],
            "summary": row["summary"],
            "entities": _json_loads(row["entities_json"]),
            "key_facts": _json_loads(row["key_facts_json"]),
            "links": _json_loads(row["links_json"]),
            "source_chat": row["source_chat"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "obsidian_path": row["obsidian_path"],
            "event_count": int(row["event_count"]),
        }
        if score is not None:
            data["score"] = score
        return data

    def _build_fts_query(self, candidate):
        text = " ".join([
            candidate.get("title", ""),
            candidate.get("topic_key", ""),
            " ".join(candidate.get("entities") or []),
            " ".join(candidate.get("links") or []),
            " ".join(candidate.get("key_facts") or []),
        ])
        tokens = []
        for token in re.findall(r"[0-9A-Za-z_\-.]{2,}|[\u4e00-\u9fff]{2,}", text):
            token = token.strip(".-")
            if token and token.lower() not in {t.lower() for t in tokens}:
                tokens.append(token)
            if len(tokens) >= 8:
                break
        return " OR ".join(f'"{token}"' for token in tokens)

    def _score_candidate(self, candidate, row):
        score = 0
        if candidate.get("topic_key") and candidate.get("topic_key") == row["topic_key"]:
            score += 100

        candidate_links = {x.lower() for x in candidate.get("links") or []}
        row_links = {x.lower() for x in _json_loads(row["links_json"])}
        score += len(candidate_links & row_links) * 80

        candidate_entities = {x.lower() for x in candidate.get("entities") or []}
        row_entities = {x.lower() for x in _json_loads(row["entities_json"])}
        score += len(candidate_entities & row_entities) * 25

        haystack = " ".join([
            row["title"],
            row["summary"],
            " ".join(_json_loads(row["key_facts_json"])),
        ]).lower()
        for token in re.findall(r"[0-9A-Za-z_\-.]{3,}|[\u4e00-\u9fff]{2,}", candidate.get("title", "").lower()):
            if token in haystack:
                score += 8
        return score

    # ── 维护：去重合并 + 全量重导出 ──────────────────────────

    def list_topics(self):
        conn = self.connect()
        if conn is None:
            return []
        try:
            return [
                self._topic_dict(r)
                for r in conn.execute("SELECT * FROM topics ORDER BY first_seen, topic_id")
            ]
        finally:
            conn.close()

    @staticmethod
    def _title_tokens(text):
        """Extract enough signal for maintenance without merging broad AI topics."""
        text = (text or "").lower()
        tokens = set()
        for token in re.findall(r"[0-9a-z][0-9a-z_.-]{1,}", text):
            tokens.add(token.strip("._-"))
        for chunk in re.findall(r"[一-鿿]{2,}", text):
            tokens.add(chunk)
            for i in range(len(chunk) - 1):
                tokens.add(chunk[i:i + 2])

        weak = {
            "ai", "claude", "codex", "openai", "deepseek", "模型", "讨论",
            "分享", "技巧", "群友", "新功能", "新版本", "消息", "体验",
            "工具", "链接", "发布", "传闻", "实际", "热聊",
        }
        return {t for t in tokens if t and t not in weak}

    @classmethod
    def _title_overlap(cls, a, b):
        a_tokens = cls._title_tokens(a)
        b_tokens = cls._title_tokens(b)
        if not a_tokens or not b_tokens:
            return 0, 0
        shared = a_tokens & b_tokens
        return len(shared), len(shared) / min(len(a_tokens), len(b_tokens))

    @classmethod
    def _topic_similarity(cls, a, b):
        ak = (a.get("topic_key") or "").strip().lower()
        bk = (b.get("topic_key") or "").strip().lower()
        if ak and ak == bk:
            return 100

        at = (a.get("title") or "").strip().lower()
        bt = (b.get("title") or "").strip().lower()
        if at and at == bt:
            return 100

        shared_title_count, title_overlap = cls._title_overlap(at, bt)
        a_links = {x.lower() for x in a.get("links") or []}
        b_links = {x.lower() for x in b.get("links") or []}
        shared_links = a_links & b_links
        if shared_links and (title_overlap >= 0.35 or shared_title_count >= 2):
            return 95

        a_facts = " ".join(a.get("key_facts") or []).lower()
        b_facts = " ".join(b.get("key_facts") or []).lower()
        fact_overlap_count, fact_overlap = cls._title_overlap(a_facts, b_facts)
        if title_overlap >= 0.75 and shared_title_count >= 3:
            return 90
        if title_overlap >= 0.6 and shared_title_count >= 2 and fact_overlap >= 0.45:
            return 90
        if shared_links and fact_overlap_count >= 2:
            return 90

        return 0

    @staticmethod
    def _pick_primary(group):
        return sorted(
            group,
            key=lambda t: (-t["event_count"], t["first_seen"], t["topic_id"]),
        )[0]

    def find_duplicate_groups(self, threshold=85):
        """Group topics that are near-certainly the same thing (union-find)."""
        topics = self.list_topics()
        n = len(topics)
        parent = list(range(n))

        def find(x):
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        for i in range(n):
            for j in range(i + 1, n):
                if self._topic_similarity(topics[i], topics[j]) >= threshold:
                    parent[find(i)] = find(j)

        clusters = {}
        for idx in range(n):
            clusters.setdefault(find(idx), []).append(topics[idx])
        return [g for g in clusters.values() if len(g) > 1]

    def _merge_group(self, conn, group):
        primary = self._pick_primary(group)
        primary_id = primary["topic_id"]
        entities = list(primary["entities"])
        key_facts = list(primary["key_facts"])
        links = list(primary["links"])
        status = primary["status"]
        first_seen = primary["first_seen"]
        last_seen = primary["last_seen"]
        event_total = primary["event_count"]
        removed_paths = []

        for t in group:
            if t["topic_id"] == primary_id:
                continue
            tid = t["topic_id"]
            entities = merge_lists(entities, t["entities"], limit=40)
            key_facts = merge_lists(key_facts, t["key_facts"], limit=60)
            links = merge_lists(links, t["links"], limit=40)
            if t["status"] == "disputed":
                status = "disputed"
            if t["first_seen"] and (not first_seen or t["first_seen"] < first_seen):
                first_seen = t["first_seen"]
            if t["last_seen"] and (not last_seen or t["last_seen"] > last_seen):
                last_seen = t["last_seen"]
            event_total += t["event_count"]
            conn.execute("UPDATE events SET topic_id = ? WHERE topic_id = ?", (primary_id, tid))
            conn.execute(
                "DELETE FROM relations WHERE source_topic_id = ? OR target_topic_id = ?",
                (tid, tid),
            )
            conn.execute("DELETE FROM topic_fts WHERE topic_id = ?", (tid,))
            conn.execute("DELETE FROM topics WHERE topic_id = ?", (tid,))
            removed_paths.append(self.full_obsidian_path(t["obsidian_path"]))

        conn.execute(
            """
            UPDATE topics
            SET entities_json = ?, key_facts_json = ?, links_json = ?, status = ?,
                first_seen = ?, last_seen = ?, event_count = ?, updated_at = ?
            WHERE topic_id = ?
            """,
            (
                _json_dumps(entities), _json_dumps(key_facts), _json_dumps(links),
                status, first_seen, last_seen, event_total, self.now_func(), primary_id,
            ),
        )
        self._upsert_fts(conn, primary_id)
        return primary_id, removed_paths

    def find_category_changes(self):
        """Return topics whose folder or filename should be normalized."""
        changes = []
        topics = self.list_topics()
        conn = self.connect()
        try:
            for topic in topics:
                canonical = normalize_category(topic["category"])
                expected_path = self._unique_obsidian_path(
                    conn,
                    topic["topic_id"],
                    canonical,
                    topic["title"],
                    first_seen=topic["first_seen"],
                    current_path=topic["obsidian_path"],
                )
                path_needs_update = expected_path != topic["obsidian_path"]
                category_changed = canonical != topic["category"]
                if category_changed or path_needs_update:
                    changes.append({
                        "title": topic["title"],
                        "from": topic["category"],
                        "to": canonical,
                        "reason": "category" if category_changed else "title",
                        "from_path": topic["obsidian_path"],
                        "to_path": expected_path,
                    })
        finally:
            if conn is not None:
                conn.close()
        return changes

    def _canonicalize_categories(self, conn):
        changes = []
        removed_paths = []
        rows = conn.execute("SELECT * FROM topics ORDER BY topic_id").fetchall()
        for row in rows:
            topic_id = int(row["topic_id"])
            old_category = row["category"]
            canonical = normalize_category(old_category)
            old_path = row["obsidian_path"]
            new_path = self._unique_obsidian_path(
                conn,
                topic_id,
                canonical,
                row["title"],
                first_seen=row["first_seen"],
                current_path=old_path,
            )
            if old_category == canonical and old_path == new_path:
                continue

            conn.execute(
                """
                UPDATE topics
                SET category = ?, obsidian_path = ?, updated_at = ?
                WHERE topic_id = ?
                """,
                (canonical, new_path, self.now_func(), topic_id),
            )
            conn.execute("UPDATE events SET category = ? WHERE topic_id = ?", (canonical, topic_id))
            self._upsert_fts(conn, topic_id)
            if old_path != new_path:
                removed_paths.append(self.full_obsidian_path(old_path))
            changes.append({
                "title": row["title"],
                "from": old_category,
                "to": canonical,
                "reason": "category" if old_category != canonical else "title",
                "from_path": old_path,
                "to_path": new_path,
            })
        return changes, removed_paths

    def _remove_empty_obsidian_dirs(self):
        root = os.path.join(self.obsidian_root, OBSIDIAN_SUBDIR)
        if not os.path.isdir(root):
            return 0
        removed = 0
        for current, _, _ in os.walk(root, topdown=False):
            if current == root:
                continue
            try:
                os.rmdir(current)
                removed += 1
            except OSError:
                pass
        return removed

    def reexport_all(self):
        """Rewrite every topic's Markdown to the current obsidian_root."""
        conn = self.connect()
        if conn is None:
            return 0
        count = 0
        try:
            ids = [r["topic_id"] for r in conn.execute("SELECT topic_id FROM topics")]
            for tid in ids:
                self._write_topic_markdown(conn, tid)
                count += 1
        finally:
            conn.close()
        return count

    def run_maintenance(self, dry_run=False, threshold=85):
        """Merge duplicate topics, fold category folders, then re-export all notes."""
        if self.read_only:
            raise RuntimeError("knowledge store is read-only")

        groups = self.find_duplicate_groups(threshold=threshold)
        category_changes = self.find_category_changes()
        summary = []
        for g in groups:
            primary = self._pick_primary(g)
            summary.append({
                "primary": primary["title"],
                "merged": [t["title"] for t in g if t["topic_id"] != primary["topic_id"]],
            })
        merge_note_count = sum(len(g) for g in groups)
        result = {
            "duplicate_groups": summary,
            "group_count": len(groups),
            "merge_note_count": merge_note_count,
            "removed_count": merge_note_count - len(groups),
            "total_topics": len(self.list_topics()),
            "category_changes": category_changes,
            "category_change_count": len(category_changes),
        }
        if dry_run:
            result["reexport_count"] = result["total_topics"] - result["removed_count"]
            return result

        conn = self.connect()
        removed_paths = []
        applied_category_changes = []
        try:
            for g in groups:
                _, paths = self._merge_group(conn, g)
                removed_paths.extend(paths)
            applied_category_changes, category_paths = self._canonicalize_categories(conn)
            removed_paths.extend(category_paths)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        for path in removed_paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

        result["category_changes"] = applied_category_changes
        result["category_change_count"] = len(applied_category_changes)
        result["reexport_count"] = self.reexport_all()
        result["removed_empty_dirs"] = self._remove_empty_obsidian_dirs()
        return result

    @staticmethod
    def _now_text(ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def normalize_candidate(decision):
    title = str(decision.get("title") or "发现关注内容").strip()[:80] or "发现关注内容"
    summary = str(decision.get("summary") or decision.get("digest") or "").strip()
    links = _normalize_list(decision.get("links"), limit=20)
    if not links:
        links = _normalize_list(re.findall(r"https?://[^\s）)]+", summary), limit=20)
    return {
        "title": title,
        "summary": summary[:1800],
        "topic_key": str(decision.get("topic_key") or title).strip()[:100],
        "category": normalize_category(decision.get("category")),
        "entities": _normalize_list(decision.get("entities"), limit=16),
        "key_facts": _normalize_list(decision.get("key_facts"), limit=20),
        "links": links,
        "event_type": str(decision.get("event_type") or "").strip()[:80],
        "status_hint": str(decision.get("status_hint") or "").strip()[:80],
    }


def normalize_relation(value):
    text = str(value or "").strip().lower()
    mapping = {
        "same": "duplicate",
        "repeat": "duplicate",
        "repeated": "duplicate",
        "duplicated": "duplicate",
        "duplicate": "duplicate",
        "old": "duplicate",
        "update": "update",
        "updated": "update",
        "new_info": "update",
        "new": "new",
        "fresh": "new",
        "contradiction": "contradiction",
        "conflict": "contradiction",
        "correction": "contradiction",
        "debunk": "contradiction",
        "rumor_debunked": "contradiction",
    }
    return mapping.get(text, "new")


def normalize_category(value):
    text = str(value or "").strip()
    if not text:
        return "未分类"
    compact = re.sub(r"[\s,，/、]+", "", text).lower()
    for canonical, needles in CATEGORY_ALIASES:
        if any(needle.lower().replace(" ", "") in compact for needle in needles):
            return canonical
    return text[:40]


def normalize_status(value):
    text = str(value or "").strip().lower()
    if text in {"resolved", "confirmed", "disputed", "tracking", "rumor"}:
        return text
    return "tracking"


def merge_lists(old_values, new_values, limit=30):
    merged = []
    seen = set()
    for value in _normalize_list(old_values, limit=limit) + _normalize_list(new_values, limit=limit):
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(value)
        if len(merged) >= limit:
            break
    return merged
