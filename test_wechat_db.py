import os
import sqlite3
import struct
import tempfile
import unittest

from Crypto.Cipher import AES

from core.image_decoder import decode_wechat_image_data, detect_mime
from core.wechat_db import WeChatDB


class WeChatDBPagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "messages.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE Chat_test (
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER
                )
            """)
            for ts in range(101, 111):
                conn.execute(
                    "INSERT INTO Chat_test VALUES (?, ?, ?, ?, ?)",
                    (1, ts, f"sender:\nmsg{ts}", None, 0),
                )
            conn.commit()
        finally:
            conn.close()

        self.db = object.__new__(WeChatDB)
        self.db._contacts = {"sender": "成员"}
        self.db._nick_to_remark = {}
        self.db._load_contacts = lambda: None
        self.db._find_msg_table = lambda username: ([self.db_path], "Chat_test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_messages_default_returns_newest_page_after_bookmark(self):
        messages = self.db.get_messages("room@chatroom", since_ts=100, limit=3)

        self.assertEqual([m["timestamp"] for m in messages], [108, 109, 110])

    def test_get_messages_page_forward_returns_next_page_after_bookmark(self):
        first = self.db.get_messages("room@chatroom", since_ts=100, limit=3, page_forward=True)
        second = self.db.get_messages(
            "room@chatroom",
            since_ts=first[-1]["timestamp"],
            limit=3,
            page_forward=True,
        )

        self.assertEqual([m["timestamp"] for m in first], [101, 102, 103])
        self.assertEqual([m["timestamp"] for m in second], [104, 105, 106])


class WeChatImageDecoderTests(unittest.TestCase):
    def test_v2_image_data_decodes_with_saved_key(self):
        key = b"1234567890abcdef"
        image = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 10
        aes_size = len(image)
        padding_size = 16 - (aes_size % 16)
        padded = image + bytes([padding_size]) * padding_size
        encrypted = AES.new(key, AES.MODE_ECB).encrypt(padded)
        data = (
            b"\x07\x08\x56\x32\x08\x07"
            + struct.pack("<I", aes_size)
            + struct.pack("<I", 0)
            + b"\x01"
            + encrypted
        )

        decoded = decode_wechat_image_data(data, key.hex())

        self.assertEqual(decoded, image)
        self.assertEqual(detect_mime(decoded), "image/jpeg")

    def test_v2_image_data_without_key_returns_none(self):
        data = b"\x07\x08\x56\x32\x08\x07" + b"\x00" * 32

        self.assertIsNone(decode_wechat_image_data(data, ""))


class WeChatMediaPagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "media.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE Chat_media (
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER,
                    packed_info_data BLOB
                )
            """)
            for ts in range(101, 111):
                conn.execute(
                    "INSERT INTO Chat_media VALUES (?, ?, ?, ?, ?, ?)",
                    (3, ts, "sender:\n<msg><img /></msg>", None, 0, None),
                )
            for ts in range(201, 211):
                md5 = f"{ts:032x}"[-32:]
                content = (
                    "sender:\n"
                    f'<msg><emoji md5="{md5}" cdnurl="https://example.com/{ts}.png" '
                    'fromusername="sender" /></msg>'
                )
                conn.execute(
                    "INSERT INTO Chat_media VALUES (?, ?, ?, ?, ?, ?)",
                    (47, ts, content, None, 0, None),
                )
            conn.commit()
        finally:
            conn.close()

        self.db = object.__new__(WeChatDB)
        self.db._contacts = {"sender": "成员"}
        self.db._nick_to_remark = {}
        self.db._emoticon_map = {}
        self.db._load_contacts = lambda: None
        self.db._load_emoticon_db = lambda: None
        self.db._find_msg_table = lambda username: ([self.db_path], "Chat_media")
        self.db._find_image_file = lambda *args, **kwargs: None

    def tearDown(self):
        self.tmp.cleanup()

    def test_image_messages_since_returns_latest_page_chronological(self):
        messages = self.db.get_image_messages("room@chatroom", since_ts=100, limit=3)

        self.assertEqual([m["timestamp"] for m in messages], [108, 109, 110])

    def test_emoji_messages_since_returns_latest_page_chronological(self):
        messages = self.db.get_emoji_messages("room@chatroom", since_ts=200, limit=3)

        self.assertEqual([m["timestamp"] for m in messages], [208, 209, 210])


if __name__ == "__main__":
    unittest.main()
