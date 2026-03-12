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
        self._nick_to_remark = {}  # {nick_name: remark} 昵称→备注反向映射
        os.makedirs(self.CACHE_DIR, exist_ok=True)

    def invalidate_cache(self):
        """清除所有数据库解密缓存，下次查询时强制重新解密"""
        self._db_cache.clear()
        self._contacts = None
        self._contacts_full = None
        self._nick_to_remark = {}
        # 清理磁盘上的缓存文件
        if os.path.isdir(self.CACHE_DIR):
            import glob as _glob
            for f in _glob.glob(os.path.join(self.CACHE_DIR, "*.db")):
                try:
                    os.remove(f)
                except OSError:
                    pass

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
            # WAL checkpoint 后 WAL 文件消失（wal_mtime 从非零变 0），
            # 此时如果 db_mtime 也没变（数据已合入主文件），缓存仍然有效
            wal_ok = (c_wal_mt == wal_mtime) or (wal_mtime == 0 and c_db_mt == db_mtime)
            if c_db_mt == db_mtime and wal_ok and os.path.exists(c_path):
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
            self._nick_to_remark = {}
            return

        names = {}
        full = []
        nick_to_remark = {}  # 昵称 → 备注名（反向映射，用于群消息中按昵称查备注）
        conn = sqlite3.connect(path)
        try:
            for r in conn.execute("SELECT username, nick_name, remark FROM contact"):
                uname, nick, remark = r
                display = remark if remark else nick if nick else uname
                names[uname] = display
                full.append({"username": uname, "nick_name": nick or "", "remark": remark or ""})
                # 如果有备注名，建立 昵称→备注 的反向映射
                if remark and nick:
                    nick_to_remark[nick] = remark
        except Exception as e:
            print(f"[wechat_db] warning: 加载联系人失败: {e}", file=__import__('sys').stderr)
        finally:
            conn.close()

        self._contacts = names
        self._contacts_full = full
        self._nick_to_remark = nick_to_remark

    def get_groups(self, include_unnamed=False):
        """获取所有群聊列表

        Args:
            include_unnamed: 是否包含没有群名的群聊（显示为原始 ID 的群）

        Returns:
            list[dict]: [{"username": "xxx@chatroom", "name": "群名"}, ...]
        """
        self._load_contacts()
        groups = []
        unnamed_idx = 0
        for c in self._contacts_full:
            if "@chatroom" in c["username"]:
                name = c["remark"] or c["nick_name"] or c["username"]
                if name == c["username"]:
                    if include_unnamed:
                        unnamed_idx += 1
                        groups.append({"username": c["username"],
                                       "name": f"无名称群{unnamed_idx}"})
                    else:
                        # 检查是否有消息，有的话保留并编号
                        msgs = self.get_messages(c["username"], limit=1)
                        if msgs:
                            unnamed_idx += 1
                            groups.append({"username": c["username"],
                                           "name": f"无名称群{unnamed_idx}"})
                else:
                    groups.append({"username": c["username"], "name": name})
        return groups

    def get_recent_sessions(self, limit=200):
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
        unnamed_idx = 0
        for username, unread, summary, ts in rows:
            display = self._contacts.get(username, username)
            is_group = "@chatroom" in username

            # 无名群聊：在 SessionTable 里有记录说明活跃过，用编号显示
            if is_group and display == username:
                unnamed_idx += 1
                display = f"无名称群{unnamed_idx}"

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
            raw_sender_id = ""
            text = content
            if is_group and ":\n" in content:
                raw_sender_id, text = content.split(":\n", 1)
                # 优先按 wxid 查（备注 > 昵称）；查不到时按昵称反查备注
                sender = self._contacts.get(raw_sender_id)
                if not sender:
                    sender = self._nick_to_remark.get(raw_sender_id, raw_sender_id)
            elif not is_group:
                # 私聊：status=2 是自己发的，status=3 是对方发的
                sender = "我" if status == 2 else contact_name

            # 群聊中自己发的消息没有 wxid:\n 前缀，sender 会是空的
            if is_group and not sender:
                sender = "您"

            # 清理 XML 特殊消息
            cleaned = _clean_msg_text(text)
            if cleaned is None:
                continue
            text = cleaned

            messages.append({
                "sender": sender,
                "raw_sender_id": raw_sender_id,
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

    # ── 图片消息相关 ─────────────────────────────────────

    @staticmethod
    def _extract_file_hash_from_protobuf(data):
        """从 packed_info_data 的 protobuf 中提取 32 位 hex 文件哈希

        手工解析 protobuf wire format（含嵌套子消息），
        寻找 32 字节的 ASCII hex 字符串或 16 字节的原始哈希。

        Returns:
            str | None: 32 字符 hex 哈希，或 None
        """
        if not data or len(data) < 10:
            return None

        def _scan(buf, depth=0):
            """递归扫描 protobuf 字段"""
            if depth > 3:
                return []
            pos = 0
            found = []
            while pos < len(buf):
                try:
                    tag_byte = buf[pos]
                    wire_type = tag_byte & 0x07
                    pos += 1
                    if tag_byte & 0x80:
                        while pos < len(buf) and buf[pos] & 0x80:
                            pos += 1
                        pos += 1

                    if wire_type == 0:  # varint
                        while pos < len(buf) and buf[pos] & 0x80:
                            pos += 1
                        pos += 1
                    elif wire_type == 1:
                        pos += 8
                    elif wire_type == 5:
                        pos += 4
                    elif wire_type == 2:  # length-delimited
                        length = 0
                        shift = 0
                        while pos < len(buf):
                            b = buf[pos]
                            length |= (b & 0x7F) << shift
                            pos += 1
                            if not (b & 0x80):
                                break
                            shift += 7
                        if pos + length > len(buf):
                            break
                        field_data = buf[pos:pos + length]
                        pos += length

                        if length == 32:
                            try:
                                ascii_str = field_data.decode("ascii")
                                bytes.fromhex(ascii_str)
                                found.append(ascii_str)
                                continue
                            except (UnicodeDecodeError, ValueError):
                                pass
                        if length == 16 and not all(b == 0 for b in field_data):
                            found.append(field_data.hex())
                            continue
                        # 递归解析子消息
                        if length > 10:
                            found.extend(_scan(field_data, depth + 1))
                    else:
                        break
                except (IndexError, ValueError):
                    break
            return found

        candidates = _scan(data)
        return candidates[0] if candidates else None

    def _get_attach_base_dir(self):
        """从 db_dir 推导出 msg/attach 图片附件目录

        db_dir = .../xwechat_files/{wxid}/db_storage
        attach = .../xwechat_files/{wxid}/msg/attach
        """
        wxid_dir = os.path.dirname(self.db_dir)
        attach_dir = os.path.join(wxid_dir, "msg", "attach")
        return attach_dir if os.path.isdir(attach_dir) else None

    _V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"

    @staticmethod
    def decode_image_v2(data, aes_key, xor_key=0x88):
        """解密 V2 格式图片（WeChat 2026+ 新格式）

        V2 结构: [6B magic][4B aes_size LE][4B xor_size LE][1B pad]
                 [AES-ECB 数据][原始数据][XOR 数据]

        Args:
            data: V2 格式的原始 bytes
            aes_key: 16 字节 AES 密钥（bytes 或 hex 字符串）
            xor_key: XOR 密钥字节，默认 0x88

        Returns:
            bytes | None: 解密后的图片数据
        """
        if len(data) < 15 or data[:6] != WeChatDB._V2_MAGIC:
            return None

        if isinstance(aes_key, str):
            aes_key = aes_key.encode("ascii")
        if len(aes_key) not in (16, 32):
            return None

        import struct
        aes_size = struct.unpack_from("<I", data, 6)[0]
        xor_size = struct.unpack_from("<I", data, 10)[0]
        body = data[15:]  # 跳过 15 字节头

        # AES 部分对齐到 16 字节
        aligned_aes = aes_size + (16 - aes_size % 16) % 16

        if aligned_aes + xor_size > len(body):
            return None

        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            cipher = Cipher(algorithms.AES(aes_key[:16]), modes.ECB())
            dec = cipher.decryptor()
            aes_part = dec.update(body[:aligned_aes]) + dec.finalize()
            aes_part = aes_part[:aes_size]  # 去掉 padding
        except Exception:
            return None

        raw_start = aligned_aes
        raw_end = len(body) - xor_size
        raw_part = body[raw_start:raw_end] if raw_end > raw_start else b""

        xor_part = bytes(b ^ xor_key for b in body[-xor_size:]) if xor_size > 0 else b""

        result = aes_part + raw_part + xor_part

        # 验证解密结果是否为有效图片
        if result[:2] == b"\xff\xd8" or result[:4] == b"\x89PNG" or \
           result[:4] == b"GIF8" or (result[:4] == b"RIFF" and result[8:12] == b"WEBP"):
            return result
        return None

    def _find_image_file(self, username, create_time, file_hash=None, prefer_thumbnail=True):
        """定位磁盘上的图片文件

        主策略：用 file_hash 直接查 attach/{md5(username)}/{YYYY-MM}/Img/{hash}_M.dat
        后备：按文件修改时间匹配（±60 秒内最接近的文件）

        Args:
            username: 聊天 username
            create_time: 消息时间戳
            file_hash: 从 packed_info_data 提取的 32 位 hex 哈希
            prefer_thumbnail: 优先返回缩略图

        Returns:
            str | None: 图片文件绝对路径
        """
        attach_base = self._get_attach_base_dir()
        if not attach_base:
            return None

        user_hash = hashlib.md5(username.encode()).hexdigest()
        user_dir = os.path.join(attach_base, user_hash)
        if not os.path.isdir(user_dir):
            return None

        dt = datetime.fromtimestamp(create_time)
        month_str = dt.strftime("%Y-%m")
        img_dir = os.path.join(user_dir, month_str, "Img")

        # ── 主策略：按哈希直接定位 ──
        if file_hash:
            months_to_try = [month_str]
            # 月份边界：也检查前后一个月
            if dt.day <= 2:
                prev = dt.replace(day=1)
                prev = prev.replace(
                    year=prev.year - 1, month=12
                ) if prev.month == 1 else prev.replace(month=prev.month - 1)
                months_to_try.append(prev.strftime("%Y-%m"))
            elif dt.day >= 28:
                nxt = dt.replace(day=1)
                nxt = nxt.replace(
                    year=nxt.year + 1, month=1
                ) if nxt.month == 12 else nxt.replace(month=nxt.month + 1)
                months_to_try.append(nxt.strftime("%Y-%m"))

            # 新版(2026+): _t.dat=缩略图, .dat=中图, _h.dat=原图
            # 旧版(~2025): _t_M.dat=缩略图, _M.dat=原图
            thumb_suffixes = ("_t_M.dat", "_t.dat")
            full_suffixes = ("_h.dat", "_M.dat", ".dat")

            for m in months_to_try:
                d = os.path.join(user_dir, m, "Img")
                if not os.path.isdir(d):
                    continue
                if prefer_thumbnail:
                    for sfx in thumb_suffixes:
                        p = os.path.join(d, file_hash + sfx)
                        if os.path.isfile(p):
                            return p
                for sfx in full_suffixes:
                    p = os.path.join(d, file_hash + sfx)
                    if os.path.isfile(p):
                        return p
                if not prefer_thumbnail:
                    for sfx in thumb_suffixes:
                        p = os.path.join(d, file_hash + sfx)
                        if os.path.isfile(p):
                            return p

        # ── 后备：按修改时间匹配 ──
        if not os.path.isdir(img_dir):
            return None

        if prefer_thumbnail:
            target_suffixes = ("_t_M.dat", "_t.dat")
        else:
            target_suffixes = ("_h.dat", "_M.dat", ".dat")
        best_path = None
        best_delta = float("inf")

        for fname in os.listdir(img_dir):
            if not any(fname.endswith(sfx) for sfx in target_suffixes):
                continue
            # 排除带下划线前缀的不匹配后缀（如 _t.dat 不应匹配 .dat 规则）
            fpath = os.path.join(img_dir, fname)
            try:
                delta = abs(os.path.getmtime(fpath) - create_time)
                if delta < best_delta:
                    best_delta = delta
                    best_path = fpath
            except OSError:
                continue

        return best_path if best_path and best_delta < 60 else None

    def get_image_messages(self, username, since_ts=0, limit=10):
        """获取聊天中的图片消息及其文件路径

        类似 get_messages()，但只查 local_type=3（图片），
        并额外提取 packed_info_data 用于定位磁盘上的图片文件。

        Returns:
            list[dict]: 每条包含 sender, time_str, timestamp,
                        thumb_path, image_path, file_hash
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
                           WCDB_CT_message_content, status, packed_info_data
                    FROM [{table_name}]
                    WHERE local_type = 3 AND create_time > ?
                    ORDER BY create_time ASC
                    LIMIT ?
                """, (since_ts, limit)).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT local_type, create_time, message_content,
                           WCDB_CT_message_content, status, packed_info_data
                    FROM [{table_name}]
                    WHERE local_type = 3
                    ORDER BY create_time DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                rows = list(reversed(rows))
        except Exception:
            return []
        finally:
            conn.close()

        contact_name = ""
        if not is_group:
            contact_name = self._contacts.get(username, username)

        messages = []
        for local_type, create_time, content, ct, status, packed_info in rows:
            # 解压内容
            if ct and ct == 4 and isinstance(content, bytes):
                try:
                    content = _zstd_dctx.decompress(content).decode("utf-8", errors="replace")
                except Exception:
                    content = ""
            elif isinstance(content, bytes):
                try:
                    content = content.decode("utf-8", errors="replace")
                except Exception:
                    content = ""

            # 提取发送者
            sender = ""
            text = content or ""
            if is_group and ":\n" in text:
                raw_id, text = text.split(":\n", 1)
                sender = self._contacts.get(raw_id)
                if not sender:
                    sender = self._nick_to_remark.get(raw_id, raw_id)
            elif not is_group:
                sender = "我" if status == 2 else contact_name
            if is_group and not sender:
                sender = "您"

            # 提取文件哈希
            file_hash = None
            if packed_info and isinstance(packed_info, bytes):
                file_hash = self._extract_file_hash_from_protobuf(packed_info)

            # 定位文件
            thumb_path = self._find_image_file(
                username, create_time, file_hash, prefer_thumbnail=True
            )
            image_path = self._find_image_file(
                username, create_time, file_hash, prefer_thumbnail=False
            )

            messages.append({
                "sender": sender,
                "timestamp": create_time,
                "time_str": datetime.fromtimestamp(create_time).strftime("%m-%d %H:%M"),
                "file_hash": file_hash,
                "thumb_path": thumb_path,
                "image_path": image_path,
            })

        return messages

    def get_emoji_messages(self, username, since_ts=0, limit=10):
        """获取聊天中的表情包/动图消息

        查询 local_type=47 的消息，解析 XML 提取 cdnurl、md5 等信息。

        Returns:
            list[dict]: 每条包含 sender, time_str, timestamp,
                        md5, cdnurl, aeskey, width, height
        """
        import re

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
                    WHERE local_type = 47 AND create_time > ?
                    ORDER BY create_time ASC
                    LIMIT ?
                """, (since_ts, limit)).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT local_type, create_time, message_content,
                           WCDB_CT_message_content, status
                    FROM [{table_name}]
                    WHERE local_type = 47
                    ORDER BY create_time DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                rows = list(reversed(rows))
        except Exception:
            return []
        finally:
            conn.close()

        contact_name = ""
        if not is_group:
            contact_name = self._contacts.get(username, username)

        messages = []
        for local_type, create_time, content, ct, status in rows:
            # 解压内容
            if ct and ct == 4 and isinstance(content, bytes):
                try:
                    content = _zstd_dctx.decompress(content).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    content = ""
            elif isinstance(content, bytes):
                try:
                    content = content.decode("utf-8", errors="replace")
                except Exception:
                    content = ""

            if not content:
                continue

            # 解析 emoji XML
            md5_match = re.search(r'md5\s*=\s*"([a-fA-F0-9]{32})"', content)
            cdn_match = re.search(r'cdnurl\s*=\s*"([^"]+)"', content)
            aes_match = re.search(r'aeskey\s*=\s*"([^"]*)"', content)
            w_match = re.search(r'width\s*=\s*"(\d+)"', content)
            h_match = re.search(r'height\s*=\s*"(\d+)"', content)
            from_match = re.search(
                r'fromusername\s*=\s*"([^"]+)"', content
            )

            if not md5_match:
                continue

            emoji_md5 = md5_match.group(1)
            cdnurl = cdn_match.group(1).replace("&amp;", "&") if cdn_match else ""
            aeskey = aes_match.group(1) if aes_match else ""
            width = int(w_match.group(1)) if w_match else 0
            height = int(h_match.group(1)) if h_match else 0

            # 提取发送者
            sender = ""
            if is_group:
                # 群聊表情包 XML 中 fromusername 是发送者
                if from_match:
                    raw_id = from_match.group(1)
                    sender = self._contacts.get(raw_id)
                    if not sender:
                        sender = self._nick_to_remark.get(raw_id, raw_id)
            else:
                sender = "我" if status == 2 else contact_name

            messages.append({
                "sender": sender,
                "timestamp": create_time,
                "time_str": datetime.fromtimestamp(create_time).strftime(
                    "%m-%d %H:%M"
                ),
                "md5": emoji_md5,
                "cdnurl": cdnurl,
                "aeskey": aeskey,
                "width": width,
                "height": height,
                "msg_type": "emoji",
            })

        return messages

    def format_messages_for_ai(self, messages, show_group_nickname=False):
        """将消息列表格式化为适合 AI 总结的文本

        Args:
            messages: 消息列表
            show_group_nickname: 是否显示群昵称（原始 sender ID + 已知名字）
        """
        lines = []
        for msg in messages:
            if msg["type"] in (10000, 10002):
                continue  # 跳过系统消息和撤回
            sender = msg.get("sender", "")
            if sender and show_group_nickname:
                raw_id = msg.get("raw_sender_id", "")
                # 如果有原始 ID 且与显示名不同，附上原始 ID 帮助识别
                if raw_id and raw_id != sender:
                    sender = f"{sender}({raw_id})"
            if sender:
                lines.append(f"[{msg['time_str']}] {sender}: {msg['text']}")
            else:
                lines.append(f"[{msg['time_str']}] {msg['text']}")
        return "\n".join(lines)
