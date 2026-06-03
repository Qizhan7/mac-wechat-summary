"""Decode local WeChat image attachment files."""
import struct

from Crypto.Cipher import AES

V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"


def detect_mime(data):
    """Detect supported image MIME type from bytes."""
    if not data or len(data) < 4:
        return None
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) > 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_v2_image_data(data):
    return bool(data and data.startswith(V2_MAGIC) and len(data) >= 15)


def parse_image_key(value):
    """Normalize a saved image AES key to 16 raw bytes."""
    text = str(value or "").strip()
    if not text:
        return None

    try:
        raw = bytes.fromhex(text)
        if len(raw) >= 16:
            return raw[:16]
    except ValueError:
        pass

    raw = text.encode("ascii", errors="ignore")
    if len(raw) >= 16:
        return raw[:16]
    return None


def decode_wechat_image_data(data, image_aes_key=None):
    """Return decoded image bytes when possible.

    Old WeChat attachments can already be JPEG/PNG/GIF/WebP bytes. Newer V2
    attachments are encrypted and require the transient image AES key captured
    from WeChat while viewing images.
    """
    if detect_mime(data):
        return data
    if not is_v2_image_data(data):
        return None

    key = parse_image_key(image_aes_key)
    if not key:
        return None

    try:
        aes_size = struct.unpack_from("<I", data, 6)[0]
        xor_size = struct.unpack_from("<I", data, 10)[0]
    except struct.error:
        return None

    body = data[15:]
    aligned_aes = aes_size + (16 - aes_size % 16) % 16
    if aligned_aes > len(body):
        return None

    try:
        cipher = AES.new(key, AES.MODE_ECB)
        aes_part = cipher.decrypt(body[:aligned_aes])[:aes_size]
    except Exception:
        return None

    raw_start = aligned_aes
    raw_end = len(body) - xor_size
    raw_part = body[raw_start:raw_end] if raw_end > raw_start else b""
    xor_part = bytes(b ^ 0x88 for b in body[-xor_size:]) if xor_size > 0 else b""
    decoded = aes_part + raw_part + xor_part

    return decoded if detect_mime(decoded) else None
