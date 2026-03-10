"""
微信数据库查询 - 联系人、群聊、消息读取
基于 wechat-decrypt 项目的 mcp_server.py 查询逻辑
"""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime

import zstandard as zstd

from .decryptor import decrypt_database, decrypt_wal

_zstd_dctx = zstd.ZstdDecompressor()

# 用于从 XML 消息中提取 <title> 的正则
_RE_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)


def _clean_msg_text(text):
    """清理微信消息文本，处理 XML 格式的特殊消息

    将拍一拍、引用回复、图片、表情等 XML 消息转为可读文本。
    纯文本消息原样返回。

    Returns:
        str | None: 清理后的文本，None 表示应跳过此消息
    """
    if not text:
        return None

    stripped = text.strip()

    # 纯文本消息：不以 < 开头的直接返回
    if not stripped.startswith("<"):
        return text

    # ── 系统消息 (sysmsg) → 跳过 ──
    if "<sysmsg" in stripped:
        return None

    # ── appmsg：引用回复、链接、文件、拍一拍 ──
    if "<appmsg" in stripped:
        m = _RE_TITLE.search(stripped)
        if m:
            title = m.group(1).strip()
            if not title:
                return None
            # 拍一拍：title 带有 "拍了拍"
            if "拍了拍" in title:
                return None
            # 引用回复 (type=57)：提取 title 作为回复内容
            if "<type>57</type>" in stripped:
                return f"[回复] {title}"
            # 文件 (type=6)
            if "<type>6</type>" in stripped:
                return f"[文件] {title}"
            # 链接/文章 (type=5 或其他)
            return f"[链接] {title}"
        return None

    # ── 图片 ──
    if "<img " in stripped or "<img>" in stripped:
        return "[图片]"

    # ── 表情 ──
    if "<emoji " in stripped or "<emoji>" in stripped:
        return "[表情]"

    # ── 语音 ──
    if "<voicemsg " in stripped:
        return "[语音]"

    # ── 视频 ──
    if "<videomsg " in stripped:
        return "[视频]"

    # ── 其他无法识别的 XML → 跳过 ──
    if stripped.startswith("<?xml") or stripped.startswith("<msg"):
        return None

    # 其他文本原样返回
    return text


class WeChatDB:
    """微信数据库查询接口"""

    CACHE_DIR = os.path.join(tempfile.gettempdir(), "wechat_summary_cache")

    def __init__(self, db_dir, keys):
        """
        Args:
            db_dir: 微信 db_storage 目录路径
            keys: {rel_path: {"enc_key": hex}, ...} 密钥字典
        """
        self.db_dir = db_dir
        self.keys = keys
        self._db_cache = {}  # rel_key -> (db_mtime, wal_mtime, cache_path)
        self._contacts = None  # {username: display_name}
        self._contacts_full = None  # [{username, nick_name, remark}]
        os.makedirs(self.CACHE_DIR, exist_ok=True)

    def invalidate_cache(self):
        """清除所有数据库解密缓存，下次查询时强制重新解密"""
        self._db_cache.clear()
        self._contacts = None
        self._contacts_full = None

    def _get_key(self, rel_path):
        """获取数据库密钥"""
        normalized = rel_path.replace("\\", "/")
        for candidate in (rel_path, normalized, normalized.replace("/", "\\")):
            if candidate in self.keys:
                return self.keys[candidate]["enc_key"]
        return None

    @staticmethod
    def _is_plain_sqlite(path):
        """检查文件是否为未加密的 SQLite 数据库"""
        try:
            with open(path, "rb") as f:
                return f.read(15) == b"SQLite format 3"
        except OSError:
            return False

    def _get_decrypted_db(self, rel_path):
        """获取解密后的数据库路径（带缓存）

        如果数据库是明文 SQLite（未加密），直接返回原路径。
        """
        db_path = os.path.join(self.db_dir, rel_path)
        if not os.path.exists(db_path):
            return None

        # ── 如果是明文 SQLite，直接返回原路径，无需解密 ──
        if self._is_plain_sqlite(db_path):
            return db_path

        # ── 需要解密 ──
        enc_key = self._get_key(rel_path)
        if not enc_key:
            return None

        wal_path = db_path + "-wal"
        try:
            db_mtime = os.path.getmtime(db_path)
            wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
        except OSError:
            return None

        # 检查缓存
        if rel_path in self._db_cache:
            c_db_mt, c_wal_mt, c_path = self._db_cache[rel_path]
            if c_db_mt == db_mtime and c_wal_mt == wal_mtime and os.path.exists(c_path):
                return c_path

        # 解密
        h = hashlib.md5(rel_path.encode()).hexdigest()[:12]
        cache_path = os.path.join(self.CACHE_DIR, f"{h}.db")

        pages = decrypt_database(db_path, cache_path, enc_key)
        if pages == 0:
            return None

        # Patch WAL
        if os.path.exists(wal_path):
            decrypt_wal(wal_path, cache_path, enc_key)

        self._db_cache[rel_path] = (db_mtime, wal_mtime, cache_path)
        return cache_path

    def _load_contacts(self):
        """加载联系人"""
        if self._contacts is not None:
            return

        path = self._get_decrypted_db(os.path.join("contact", "contact.db"))
        if not path:
            self._contacts = {}
            self._contacts_full = []
            return

        names = {}
        full = []
        conn = sqlite3.connect(path)
        try:
            for r in conn.execute("SELECT username, nick_name, remark FROM contact"):
                uname, nick, remark = r
                display = remark if remark else nick if nick else uname
                names[uname] = display
                full.append({"username": uname, "nick_name": nick or "", "remark": remark or ""})
        except Exception:
            pass
        finally:
            conn.close()

        self._contacts = names
        self._contacts_full = full

    def get_groups(self):
        """获取所有群聊列表

        Returns:
            list[dict]: [{"username": "xxx@chatroom", "name": "群名"}, ...]
        """
        self._load_contacts()
        groups = []
        for c in self._contacts_full:
            if "@chatroom" in c["username"]:
                name = c["remark"] or c["nick_name"] or c["username"]
                groups.append({"username": c["username"], "name": name})
        return groups

    def get_recent_sessions(self, limit=30):
        """获取最近会话（含群聊和私聊）"""
        path = self._get_decrypted_db(os.path.join("session", "session.db"))
        if not path:
            return []

        self._load_contacts()
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute("""
                SELECT username, unread_count, summary, last_timestamp
                FROM SessionTable
                WHERE last_timestamp > 0
                ORDER BY last_timestamp DESC
                LIMIT ?
            """, (limit,)).fetchall()
        except Exception:
            return []
        finally:
            conn.close()

        sessions = []
        for username, unread, summary, ts in rows:
            display = self._contacts.get(username, username)
            is_group = "@chatroom" in username

            if isinstance(summary, bytes):
                try:
                    summary = _zstd_dctx.decompress(summary).decode("utf-8", errors="replace")
                except Exception:
                    summary = ""
            if isinstance(summary, str) and ":\n" in summary:
                summary = summary.split(":\n", 1)[1]

            sessions.append({
                "username": username,
                "name": display,
                "is_group": is_group,
                "unread": unread or 0,
                "summary": summary or "",
                "timestamp": ts,
                "time_str": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
            })

        return sessions

    def _find_msg_table(self, username):
        """查找用户的消息表所在的数据库

        搜索所有消息相关的数据库文件（message_N.db 和 biz_message_N.db），
        如果同一张表出现在多个数据库中，返回消息行数最多的那个（真正的聊天记录）。
        同时搜索仍存在于缓存中的旧解密文件（即使当前没有密钥）。
        """
        table_hash = hashlib.md5(username.encode()).hexdigest()
        table_name = f"Msg_{table_hash}"

        # 1) 找有密钥的消息数据库
        msg_keys = sorted([
            k for k in self.keys
            if re.search(r"message[/\\](?:biz_)?message_\d+\.db$", k.replace("\\", "/"))
        ])

        best_path = None
        best_count = -1

        for rel_key in msg_keys:
            path = self._get_decrypted_db(rel_key)
            if not path:
                continue
            conn = sqlite3.connect(path)
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                ).fetchone()
                if exists:
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM [{table_name}]"
                    ).fetchone()[0]
                    if count > best_count:
                        best_count = count
                        best_path = path
            except Exception:
                pass
            finally:
                conn.close()

        # 2) 如果未找到或行数太少，搜索缓存中可能存在的旧解密文件
        #    （某些 message_N.db 可能之前解密过但现在密钥丢失）
        if best_count < 100:
            for prefix in ("message", "biz_message"):
                for i in range(10):
                    rel = f"message/{prefix}_{i}.db"
                    if rel in self.keys:
                        continue  # 已经在上面搜过了
                    h = hashlib.md5(rel.encode()).hexdigest()[:12]
                    cache_path = os.path.join(self.CACHE_DIR, f"{h}.db")
                    if not os.path.exists(cache_path):
                        continue
                    conn = sqlite3.connect(cache_path)
                    try:
                        exists = conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table_name,),
                        ).fetchone()
                        if exists:
                            count = conn.execute(
                                f"SELECT COUNT(*) FROM [{table_name}]"
                            ).fetchone()[0]
                            if count > best_count:
                                best_count = count
                                best_path = cache_path
                    except Exception:
                        pass
                    finally:
                        conn.close()

        if best_path:
            return best_path, table_name
        return None, None

    def count_messages_since(self, username, since_ts):
        """快速统计某个群自 since_ts 之后的新消息数（只读缓存副本，安全）

        Returns:
            int: 新消息数量，查询失败返回 -1
        """
        db_path, table_name = self._find_msg_table(username)
        if not db_path:
            return -1

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM [{table_name}] WHERE create_time > ?",
                (since_ts,),
            ).fetchone()
            return row[0] if row else 0
        except Exception:
            return -1
        finally:
            conn.close()

    def get_messages(self, username, since_ts=0, limit=500):
        """获取群聊/私聊消息

        Args:
            username: 用户名或群聊 ID (xxx@chatroom)
            since_ts: 起始时间戳（只获取此时间之后的消息）
            limit: 最大消息数

        Returns:
            list[dict]: [{"sender": "名字", "text": "内容", "timestamp": ts, "type": int}, ...]
        """
        self._load_contacts()
        is_group = "@chatroom" in username

        db_path, table_name = self._find_msg_table(username)
        if not db_path:
            return []

        conn = sqlite3.connect(db_path)
        try:
            if since_ts > 0:
                rows = conn.execute(f"""
                    SELECT local_type, create_time, message_content,
                           WCDB_CT_message_content, status
                    FROM [{table_name}]
                    WHERE create_time > ?
                    ORDER BY create_time ASC
                    LIMIT ?
                """, (since_ts, limit)).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT local_type, create_time, message_content,
                           WCDB_CT_message_content, status
                    FROM [{table_name}]
                    ORDER BY create_time DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                rows = list(reversed(rows))
        except Exception:
            return []
        finally:
            conn.close()

        # 私聊时，用联系人显示名标注对方消息
        contact_name = ""
        if not is_group:
            contact_name = self._contacts.get(username, username)

        messages = []
        for local_type, create_time, content, ct, status in rows:
            # zstd 解压
            if ct and ct == 4 and isinstance(content, bytes):
                try:
                    content = _zstd_dctx.decompress(content).decode("utf-8", errors="replace")
                except Exception:
                    content = None
            elif isinstance(content, bytes):
                try:
                    content = content.decode("utf-8", errors="replace")
                except Exception:
                    content = None

            if content is None:
                continue

            sender = ""
            text = content
            if is_group and ":\n" in content:
                sender, text = content.split(":\n", 1)
                sender = self._contacts.get(sender, sender)
            elif not is_group:
                # 私聊：status=2 是自己发的，status=3 是对方发的
                sender = "我" if status == 2 else contact_name

            # 群聊中自己发的消息没有 wxid:\n 前缀，sender 会是空的
            if is_group and not sender and status == 2:
                sender = "我"

            # 清理 XML 特殊消息
            cleaned = _clean_msg_text(text)
            if cleaned is None:
                continue
            text = cleaned

            messages.append({
                "sender": sender,
                "text": text,
                "timestamp": create_time,
                "time_str": datetime.fromtimestamp(create_time).strftime("%m-%d %H:%M"),
                "type": local_type,
            })

        return messages

    def _get_fts_db(self):
        """获取解密后的 FTS 全文搜索数据库路径"""
        return self._get_decrypted_db("message/message_fts.db")

    def _build_fts_lookup(self, fts_conn):
        """构建 FTS 辅助映射表

        Returns:
            (name2id, id2name, id2sender_name)
            - name2id: {username: session_id}
            - id2name: {session_id: username}
            - id2sender_name: {sender_id: display_name}
        """
        self._load_contacts()
        name2id = {}
        id2name = {}
        id2sender_name = {}

        for row in fts_conn.execute("SELECT rowid, username FROM name2id"):
            rid, uname = row
            name2id[uname] = rid
            id2name[rid] = uname
            # 解析发送者显示名
            display = self._contacts.get(uname, uname)
            id2sender_name[rid] = display

        return name2id, id2name, id2sender_name

    def search_messages(self, keywords, usernames, start_ts, end_ts, limit_per_group=2000):
        """跨群搜索消息（AND 布尔搜索 + 滑动窗口）

        优先使用 FTS 全文搜索数据库（覆盖所有历史消息），
        若 FTS 不可用则回退到直接扫描消息数据库。
        只读取解密缓存副本，不接触微信原始数据库。

        Args:
            keywords: list[str] - 关键词列表（已按空格拆分）
            usernames: list[str] - 要搜索的群聊 username 列表
            start_ts: int/float - 起始时间戳
            end_ts: int/float - 结束时间戳
            limit_per_group: int - 每个群最多检索的消息数

        Returns:
            dict: {username: [{"sender", "text", "timestamp", "time_str", "group_name"}]}
        """
        # 尝试 FTS 搜索（覆盖全部历史数据）
        fts_path = self._get_fts_db()
        if fts_path:
            print("[search] 使用 FTS 全文索引搜索（覆盖全部历史数据）")
            return self._search_via_fts(fts_path, keywords, usernames, start_ts, end_ts, limit_per_group)

        # FTS 不可用，回退到直接数据库搜索
        print("[search] FTS 不可用，回退到直接数据库搜索")
        return self._search_via_db(keywords, usernames, start_ts, end_ts, limit_per_group)

    def _search_via_fts(self, fts_path, keywords, usernames, start_ts, end_ts, limit_per_group=2000):
        """通过 FTS 全文搜索数据库搜索消息

        FTS 数据库包含所有消息的全文索引，不依赖消息数据库的解密密钥，
        覆盖全部历史消息（可追溯至 2022 年）。
        """
        self._load_contacts()
        results = {}

        fts_conn = sqlite3.connect(fts_path)
        try:
            name2id, id2name, id2sender_name = self._build_fts_lookup(fts_conn)
        except Exception as e:
            print(f"[search] FTS 映射表构建失败: {e}")
            fts_conn.close()
            return results

        # 预处理关键词
        keywords_lower = [kw.lower() for kw in keywords]

        # 构建要搜索的 session_id 集合
        target_sessions = {}  # session_id → username
        for uname in usernames:
            sid = name2id.get(uname)
            if sid is not None:
                target_sessions[sid] = uname
            else:
                group_name = self._contacts.get(uname, uname)
                print(f"[search]   ⚠ {group_name}: 不在 FTS 索引中")

        print(f"[search] 正在搜索 {len(target_sessions)} 个群聊...")

        # FTS 有 4 个内容分片表 (message_fts_v4_0_content ~ _3_content)
        # 结构: id, c0(acontent), c1(message_local_id), c2(sort_seq),
        #        c3(local_type), c4(session_id), c5(sender_id), c6(create_time)
        FTS_TABLES = [f"message_fts_v4_{i}_content" for i in range(4)]

        for idx, (sid, uname) in enumerate(target_sessions.items()):
            group_name = self._contacts.get(uname, uname)
            print(f"[search]   ({idx+1}/{len(target_sessions)}) 搜索 {group_name}...")

            # 从所有 FTS 分片收集消息
            all_msgs = []
            total_rows = 0
            for tbl in FTS_TABLES:
                try:
                    rows = fts_conn.execute(f"""
                        SELECT c0, c5, c6 FROM [{tbl}]
                        WHERE c4 = ? AND c6 >= ? AND c6 <= ?
                        ORDER BY c6 ASC
                        LIMIT ?
                    """, (sid, int(start_ts), int(end_ts), limit_per_group)).fetchall()
                except Exception:
                    continue

                total_rows += len(rows)
                for text, sender_id, create_time in rows:
                    if not text or not create_time:
                        continue
                    # FTS 内容已经是清理后的文本，不需要 XML 处理
                    sender = id2sender_name.get(sender_id, str(sender_id))
                    all_msgs.append({
                        "sender": sender,
                        "text": text,
                        "text_lower": text.lower(),
                        "timestamp": create_time,
                        "time_str": datetime.fromtimestamp(create_time).strftime("%m-%d %H:%M"),
                        "group_name": group_name,
                    })

            # 按时间排序（来自不同分片的消息需要合并排序）
            all_msgs.sort(key=lambda m: m["timestamp"])

            # 限制每群最大消息数
            if len(all_msgs) > limit_per_group:
                all_msgs = all_msgs[:limit_per_group]

            if not all_msgs:
                if total_rows > 0:
                    print(f"[search]   · {group_name}: 该时间段无文本消息")
                else:
                    print(f"[search]   · {group_name}: 该时间段无消息")
                continue

            print(f"[search]     {total_rows} 条记录 → {len(all_msgs)} 条文本")

            # 滑动窗口搜索（上下 1 条 = 共 3 条消息为一个窗口）
            CONTEXT = 1
            matched_indices = set()
            for i in range(len(all_msgs)):
                win_start = max(0, i - CONTEXT)
                win_end = min(len(all_msgs), i + CONTEXT + 1)
                combined = " ".join(all_msgs[j]["text_lower"] for j in range(win_start, win_end))
                if all(kw in combined for kw in keywords_lower):
                    for j in range(win_start, win_end):
                        matched_indices.add(j)

            matched = []
            for mi in sorted(matched_indices):
                msg = all_msgs[mi]
                matched.append({
                    "sender": msg["sender"],
                    "text": msg["text"],
                    "timestamp": msg["timestamp"],
                    "time_str": msg["time_str"],
                    "group_name": msg["group_name"],
                })

            if matched:
                results[uname] = matched
                print(f"[search]   ✓ {group_name}: {len(matched)} 条命中（共扫描 {total_rows} 条）")
            else:
                print(f"[search]   · {group_name}: 扫描 {len(all_msgs)} 条，无命中")

        fts_conn.close()

        total_hits = sum(len(msgs) for msgs in results.values())
        print(f"[search] ✅ 搜索完成，共命中 {total_hits} 条消息，涉及 {len(results)} 个群")
        return results

    def _search_via_db(self, keywords, usernames, start_ts, end_ts, limit_per_group=2000):
        """直接扫描消息数据库搜索（FTS 不可用时的回退方案）"""
        self._load_contacts()
        results = {}

        # 预先构建 username → (db_path, table_name) 映射
        table_cache = {}
        total = len(usernames)
        print(f"[search] 正在定位 {total} 个群聊的消息表...")
        for i, username in enumerate(usernames):
            db_path, table_name = self._find_msg_table(username)
            if db_path:
                table_cache[username] = (db_path, table_name)
        print(f"[search] 定位完成，{len(table_cache)}/{total} 个群有消息表")

        keywords_lower = [kw.lower() for kw in keywords]

        for i, username in enumerate(table_cache):
            is_group = "@chatroom" in username
            group_name = self._contacts.get(username, username)
            db_path, table_name = table_cache[username]

            print(f"[search]   ({i+1}/{len(table_cache)}) 搜索 {group_name}...")

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(f"""
                    SELECT local_type, create_time, message_content,
                           WCDB_CT_message_content
                    FROM [{table_name}]
                    WHERE create_time >= ? AND create_time <= ?
                    ORDER BY create_time ASC
                    LIMIT ?
                """, (int(start_ts), int(end_ts), limit_per_group)).fetchall()
            except Exception as e:
                print(f"[search]   ⚠ {group_name} 查询失败: {e}")
                continue
            finally:
                conn.close()

            if not rows:
                continue

            all_msgs = []
            for local_type, create_time, content, ct in rows:
                if ct and ct == 4 and isinstance(content, bytes):
                    try:
                        content = _zstd_dctx.decompress(content).decode("utf-8", errors="replace")
                    except Exception:
                        continue
                elif isinstance(content, bytes):
                    try:
                        content = content.decode("utf-8", errors="replace")
                    except Exception:
                        continue

                if content is None:
                    continue

                sender = ""
                text = content
                if is_group and ":\n" in content:
                    sender, text = content.split(":\n", 1)
                    sender = self._contacts.get(sender, sender)

                text = _clean_msg_text(text)
                if text is None:
                    continue

                all_msgs.append({
                    "sender": sender,
                    "text": text,
                    "text_lower": text.lower(),
                    "timestamp": create_time,
                    "time_str": datetime.fromtimestamp(create_time).strftime("%m-%d %H:%M"),
                    "group_name": group_name,
                })

            print(f"[search]     {len(rows)} 条记录 → {len(all_msgs)} 条文本")

            CONTEXT = 1
            matched_indices = set()
            for i in range(len(all_msgs)):
                win_start = max(0, i - CONTEXT)
                win_end = min(len(all_msgs), i + CONTEXT + 1)
                combined = " ".join(all_msgs[j]["text_lower"] for j in range(win_start, win_end))
                if all(kw in combined for kw in keywords_lower):
                    for j in range(win_start, win_end):
                        matched_indices.add(j)

            matched = []
            for idx in sorted(matched_indices):
                msg = all_msgs[idx]
                matched.append({
                    "sender": msg["sender"],
                    "text": msg["text"],
                    "timestamp": msg["timestamp"],
                    "time_str": msg["time_str"],
                    "group_name": msg["group_name"],
                })

            if matched:
                results[username] = matched
                print(f"[search]   ✓ {group_name}: {len(matched)} 条命中（共扫描 {len(rows)} 条）")
            elif rows:
                print(f"[search]   · {group_name}: 扫描 {len(rows)} 条，无命中")
            else:
                print(f"[search]   · {group_name}: 该时间段无文本消息")

        total_hits = sum(len(msgs) for msgs in results.values())
        print(f"[search] ✅ 搜索完成，共命中 {total_hits} 条消息，涉及 {len(results)} 个群")
        return results

    def get_fts_coverage(self, usernames):
        """获取 FTS 中各群聊的数据覆盖范围

        Args:
            usernames: list[str] - 群聊 username 列表

        Returns:
            dict: {username: {"earliest": timestamp, "latest": timestamp, "count": int}}
                  如果 FTS 不可用或群聊无数据，对应 username 不在结果中
        """
        fts_path = self._get_fts_db()
        if not fts_path:
            return {}

        fts_conn = sqlite3.connect(fts_path)
        try:
            name2id = dict(fts_conn.execute("SELECT username, rowid FROM name2id").fetchall())
        except Exception:
            fts_conn.close()
            return {}

        coverage = {}
        FTS_TABLES = [f"message_fts_v4_{i}_content" for i in range(4)]

        for uname in usernames:
            sid = name2id.get(uname)
            if sid is None:
                continue
            earliest = None
            latest = None
            total = 0
            for tbl in FTS_TABLES:
                try:
                    row = fts_conn.execute(
                        f"SELECT MIN(c6), MAX(c6), COUNT(*) FROM [{tbl}] WHERE c4 = ?",
                        (sid,),
                    ).fetchone()
                    if row[2] > 0:
                        total += row[2]
                        if row[0] and (earliest is None or row[0] < earliest):
                            earliest = row[0]
                        if row[1] and (latest is None or row[1] > latest):
                            latest = row[1]
                except Exception:
                    continue
            if total > 0:
                coverage[uname] = {"earliest": earliest, "latest": latest, "count": total}

        fts_conn.close()
        return coverage

    def resolve_username(self, name):
        """将群名/备注名/昵称解析为 username"""
        self._load_contacts()
        if name in self._contacts or "@chatroom" in name:
            return name
        name_lower = name.lower()
        # 精确匹配
        for uname, display in self._contacts.items():
            if name_lower == display.lower():
                return uname
        # 模糊匹配
        for uname, display in self._contacts.items():
            if name_lower in display.lower():
                return uname
        return None

    def format_messages_for_ai(self, messages):
        """将消息列表格式化为适合 AI 总结的文本"""
        lines = []
        for msg in messages:
            if msg["type"] in (10000, 10002):
                continue  # 跳过系统消息和撤回
            if msg["sender"]:
                lines.append(f"[{msg['time_str']}] {msg['sender']}: {msg['text']}")
            else:
                lines.append(f"[{msg['time_str']}] {msg['text']}")
        return "\n".join(lines)
