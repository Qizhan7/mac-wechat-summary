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
        aligned = aes_size + (16 - aes_size % 16) % 16
        padded = image + b"\x00" * (aligned - aes_size)
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


if __name__ == "__main__":
    unittest.main()
