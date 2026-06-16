"""
WeCom Session Archive Fetcher — 企业微信会话内容存档数据提取

Reads conversation messages from WeChat Work's session archive (会话内容存档).
Supports two modes:
  1. REST-only:  fetch group chat metadata, member lists, user info (no SDK needed)
  2. SDK mode:   fetch actual message content via the native C SDK (requires .so/.dll)

Usage:
  python wecom_archive.py                          # REST mode: list group chats
  python wecom_archive.py --sync-msg               # SDK mode: sync messages
  python wecom_archive.py --sync-msg --cursor 0    # SDK mode: from specific cursor
  python wecom_archive.py --sync-msg --limit 500   # SDK mode: custom batch size
  python wecom_archive.py --chat-id wrXXXXX        # Get specific group chat info
  python wecom_archive.py --export json            # Export synced messages to JSON
  python wecom_archive.py --export csv             # Export synced messages to CSV
  python wecom_archive.py --dry-run                # Print what would happen

Prerequisites (REST mode):
  - Enterprise WeChat admin account
  - Corpid + Secret of an app with "customer contact" (客户联系) permission

Prerequisites (SDK mode — message content):
  - "Session Content Archive" (会话内容存档) feature purchased & enabled
  - RSA public/private key pair configured in the admin panel
  - libWeWorkFinanceSdk_C.so (Linux) or WeWorkFinanceSdk.dll (Windows) in the SDK dir

Environment variables (recommended, same pattern as feishu_bot.py):
  WECOM_CORPID           Your enterprise WeChat corpid
  WECOM_SECRET           App secret (for REST APIs)
  WECOM_CHAT_SECRET      Session archive secret (for SDK init, different from above)
  WECOM_PRI_KEY_PATH     Path to RSA private key PEM file (for message decryption)
  WECOM_SDK_LIB_PATH     Path to the native SDK library (.so / .dll)
"""

import os
import sys
import json
import time
import base64
import struct
import hashlib
import logging
import argparse
import ctypes
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple

import requests

# =====================================================
# LOGGING
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wecom_archive")

# =====================================================
# CONFIG (from env vars — no hardcoded secrets)
# =====================================================

CORPID = os.environ.get("WECOM_CORPID", "")
SECRET = os.environ.get("WECOM_SECRET", "")            # App secret for REST APIs
CHAT_SECRET = os.environ.get("WECOM_CHAT_SECRET", "")  # Session archive secret (may differ)
PRI_KEY_PATH = os.environ.get("WECOM_PRI_KEY_PATH", "") # RSA private key PEM file
SDK_LIB_PATH = os.environ.get("WECOM_SDK_LIB_PATH", "") # Native SDK library path

WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"

# =====================================================
# MESSAGE TYPE CONSTANTS
# =====================================================

MSGTYPE_MAP = {
    1:  "text",      2:  "image",      3:  "emotion",
    4:  "link",      5:  "miniprogram",6:  "voice",
    7:  "video",     8:  "file",       9:  "card",
    10: "forward",   11: "video_channel", 12: "calendar",
    13: "redpacket", 14: "location",   15: "quick_meeting",
    16: "todo",      17: "vote",       18: "online_doc",
    19: "rich_text", 20: "mixed",      21: "audio_archive",
    22: "voip",      23: "wedrive",    27: "markdown",
}

SENDER_TYPE_MAP = {1: "member", 2: "external", 3: "bot"}

# =====================================================
# REST API CLIENT (pure HTTP, no SDK needed)
# =====================================================

_session = None

def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.trust_env = False
    return _session


def get_access_token(corpid: str, secret: str) -> str:
    """Get WeChat Work access_token (valid ~2h)."""
    url = f"{WECOM_API_BASE}/gettoken"
    params = {"corpid": corpid, "corpsecret": secret}
    resp = _get_session().get(url, params=params, timeout=(10, 30))
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(
            f"gettoken failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        )
    log.info("Access token obtained, expires in %ds", data.get("expires_in", 0))
    return data["access_token"]


# ---------- External Contact (客户联系) APIs ----------

def get_groupchat_list(token: str, status_filter: int = 0,
                       cursor: str = "") -> dict:
    """
    Get customer group chat list (外部群列表).
    POST /cgi-bin/externalcontact/groupchat/list
    status_filter: 0=all, 1=normal, 2=disbanded, 3=manual disband
    """
    url = f"{WECOM_API_BASE}/externalcontact/groupchat/list"
    body = {
        "status_filter": status_filter,
        "cursor": cursor,
        "limit": 1000,
    }
    headers = {"Content-Type": "application/json"}
    resp = _get_session().post(
        url, params={"access_token": token},
        json=body, headers=headers, timeout=(10, 60),
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(
            f"groupchat/list failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        )
    return data


def get_groupchat_detail(token: str, chat_id: str) -> dict:
    """
    Get single customer group chat detail.
    POST /cgi-bin/externalcontact/groupchat/get
    """
    url = f"{WECOM_API_BASE}/externalcontact/groupchat/get"
    body = {"chat_id": chat_id}
    headers = {"Content-Type": "application/json"}
    resp = _get_session().post(
        url, params={"access_token": token},
        json=body, headers=headers, timeout=(10, 30),
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(
            f"groupchat/get failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        )
    return data


# ---------- External Contact user APIs ----------

def get_external_contact_detail(token: str, external_userid: str,
                                cursor: str = "") -> dict:
    """
    Get external contact detail.
    GET /cgi-bin/externalcontact/get?access_token=TOKEN&external_userid=xxx
    """
    url = f"{WECOM_API_BASE}/externalcontact/get"
    params = {"access_token": token, "external_userid": external_userid}
    if cursor:
        params["cursor"] = cursor
    resp = _get_session().get(url, params=params, timeout=(10, 30))
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(
            f"externalcontact/get failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        )
    return data


def get_follow_user_list(token: str) -> dict:
    """
    Get list of employees who have external contacts.
    GET /cgi-bin/externalcontact/get_follow_user_list
    """
    url = f"{WECOM_API_BASE}/externalcontact/get_follow_user_list"
    resp = _get_session().get(
        url, params={"access_token": token}, timeout=(10, 30),
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(
            f"get_follow_user_list failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        )
    return data


# ---------- Session Archive Management APIs (REST) ----------

def get_permit_user_list(token: str, msg_type: int = 0) -> dict:
    """
    Get list of users whose conversations are archived.
    POST /cgi-bin/msgaudit/get_permit_user_list
    msg_type: 0=internal, 1=external, 2=both
    """
    url = f"{WECOM_API_BASE}/msgaudit/get_permit_user_list"
    body = {"type": msg_type}
    headers = {"Content-Type": "application/json"}
    resp = _get_session().post(
        url, params={"access_token": token},
        json=body, headers=headers, timeout=(10, 30),
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(
            f"get_permit_user_list failed: errcode={data.get('errcode')}"
            f" errmsg={data.get('errmsg')}"
        )
    return data


def get_internal_groupchat(token: str, roomid: str) -> dict:
    """
    Get internal group chat info via session archive API.
    POST /cgi-bin/msgaudit/groupchat/get
    Note: ONLY for internal groups. External groups will return errcode 90501.
    """
    url = f"{WECOM_API_BASE}/msgaudit/groupchat/get"
    body = {"roomid": roomid}
    headers = {"Content-Type": "application/json"}
    resp = _get_session().post(
        url, params={"access_token": token},
        json=body, headers=headers, timeout=(10, 30),
    )
    return resp.json()


# ---------- User / Department APIs ----------

def get_user_detail(token: str, userid: str) -> dict:
    """
    Get internal user detail.
    GET /cgi-bin/user/get
    """
    url = f"{WECOM_API_BASE}/user/get"
    params = {"access_token": token, "userid": userid}
    resp = _get_session().get(url, params=params, timeout=(10, 30))
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(
            f"user/get failed: errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
        )
    return data


# =====================================================
# UTILITY: RSA + AES Decryption
# =====================================================

def load_rsa_private_key(path: str):
    """Load RSA private key from PEM file.  Supports PKCS#1 and PKCS#8."""
    try:
        from Crypto.PublicKey import RSA
        with open(path, "r", encoding="utf-8") as f:
            key_data = f.read()
        return RSA.import_key(key_data)
    except Exception:
        # Fall back to cryptography library
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key,
        )
        with open(path, "rb") as f:
            return load_pem_private_key(f.read(), password=None)


def rsa_decrypt_key(encrypted_b64: str, private_key) -> bytes:
    """
    Decrypt the encrypt_random_key (base64-encoded) using RSA private key.
    Returns the AES key used to decrypt the actual message content.
    """
    from Crypto.Cipher import PKCS1_v1_5
    cipher = PKCS1_v1_5.new(private_key)
    encrypted_bytes = base64.b64decode(encrypted_b64)
    key = cipher.decrypt(encrypted_bytes, sentinel=b"ERROR")
    return key


def aes_decrypt_message(encrypted_msg: str, aes_key: bytes) -> str:
    """
    Decrypt a single message using AES-256-CBC.
    The encrypted_msg from the archive API is base64-encoded.
    Format: AES-256-CBC with IV = first 16 bytes of the key.
    """
    from Crypto.Cipher import AES
    # The SDK uses AES-256-CBC with IV = first 16 bytes of the key
    # Or alternatively, the message itself may contain the IV prepended
    encrypted_bytes = base64.b64decode(encrypted_msg)

    try:
        # Method 1: IV = first 16 bytes of the AES key
        iv = aes_key[:16]
        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted_bytes)
        # Remove PKCS7 padding
        pad_len = decrypted[-1]
        if pad_len <= 32:
            decrypted = decrypted[:-pad_len]
        return decrypted.decode("utf-8", errors="replace")
    except Exception:
        pass

    try:
        # Method 2: Openssl compatible: "Salted__" prefix
        if encrypted_bytes[:8] == b"Salted__":
            salt = encrypted_bytes[8:16]
            # Derive key + IV using PBKDF1-like (OpenSSL EVP_BytesToKey)
            d = b""
            while len(d) < 48:  # 32 key + 16 IV
                d += hashlib.md5(d[-16:] + aes_key + salt).digest()
            key, iv = d[:32], d[32:48]
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted = cipher.decrypt(encrypted_bytes[16:])
            pad_len = decrypted[-1]
            if pad_len <= 16:
                decrypted = decrypted[:-pad_len]
            return decrypted.decode("utf-8", errors="replace")
    except Exception:
        pass

    raise ValueError("Unable to decrypt message with provided AES key")


# =====================================================
# NATIVE SDK WRAPPER (via ctypes — for message content)
# =====================================================

class WeComFinanceSDK:
    """
    Python wrapper for the enterprise WeChat finance SDK native library.

    The SDK provides:
      - NewSdk() / DestroySdk()
      - Init(sdk, corpid, secret)
      - GetChatData(sdk, seq, limit, proxy, passwd, timeout, slice)
      - DecryptData(encrypt_key, encrypt_msg, slice)
      - GetContentFromSlice(slice)  → char*
      - NewSlice() / FreeSlice(slice)

    Expected library name:
      - Linux:   libWeWorkFinanceSdk_C.so
      - Windows: WeWorkFinanceSdk.dll
    """

    def __init__(self, corpid: str, secret: str, lib_path: str):
        self.corpid = corpid
        self.secret = secret
        self.lib_path = lib_path
        self._sdk = None
        self._dll = None
        self._initialized = False

    def _load_library(self):
        """Load the native SDK shared library."""
        if self._dll is not None:
            return
        try:
            self._dll = ctypes.cdll.LoadLibrary(self.lib_path)
        except OSError as e:
            raise RuntimeError(
                f"Failed to load SDK library '{self.lib_path}': {e}\n"
                f"Make sure WECOM_SDK_LIB_PATH points to the correct "
                f"libWeWorkFinanceSdk_C.so / WeWorkFinanceSdk.dll"
            )

    def init(self):
        """Initialize the SDK. Must be called before fetching data."""
        self._load_library()

        # Define function signatures
        self._dll.NewSdk.restype = ctypes.c_void_p
        self._dll.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        self._dll.Init.restype = ctypes.c_int

        self._dll.GetChatData.argtypes = [
            ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_uint,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
            ctypes.c_ulonglong,
        ]
        self._dll.GetChatData.restype = ctypes.c_int

        self._dll.GetContentFromSlice.argtypes = [ctypes.c_ulonglong]
        self._dll.GetContentFromSlice.restype = ctypes.c_char_p

        self._dll.NewSlice.restype = ctypes.c_ulonglong
        self._dll.FreeSlice.argtypes = [ctypes.c_ulonglong]
        self._dll.FreeSlice.restype = None

        self._dll.DecryptData.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_ulonglong,
        ]
        self._dll.DecryptData.restype = ctypes.c_int

        self._dll.DestroySdk.argtypes = [ctypes.c_void_p]

        # Create SDK instance
        self._sdk = self._dll.NewSdk()
        ret = self._dll.Init(
            self._sdk,
            self.corpid.encode("utf-8"),
            self.secret.encode("utf-8"),
        )
        if ret != 0:
            raise RuntimeError(f"SDK Init failed with code {ret}")
        self._initialized = True
        log.info("SDK initialized successfully")

    def get_chat_data(self, seq: int = 0, limit: int = 1000,
                      proxy: str = "", passwd: str = "",
                      timeout: int = 30) -> List[dict]:
        """
        Fetch chat data starting from `seq`.
        Returns list of raw message dicts (still encrypted).
        Each dict has: seq, msgid, publickey_ver, encrypt_random_key, encrypt_chat_msg
        """
        if not self._initialized:
            self.init()

        # Create slice to receive data
        slice_handle = self._dll.NewSlice()

        ret = self._dll.GetChatData(
            self._sdk,
            ctypes.c_ulonglong(seq),
            ctypes.c_uint(limit),
            proxy.encode("utf-8") if proxy else None,
            passwd.encode("utf-8") if passwd else None,
            timeout,
            slice_handle,
        )

        if ret != 0:
            self._dll.FreeSlice(slice_handle)
            raise RuntimeError(f"GetChatData failed with code {ret} (seq={seq})")

        # Get content from slice
        content_ptr = self._dll.GetContentFromSlice(slice_handle)
        raw = ctypes.string_at(content_ptr).decode("utf-8", errors="replace")
        self._dll.FreeSlice(slice_handle)

        data = json.loads(raw)
        if data.get("errcode", 0) != 0:
            raise RuntimeError(
                f"GetChatData API error: errcode={data.get('errcode')} "
                f"errmsg={data.get('errmsg')}"
            )

        return data.get("chatdata", [])

    def decrypt_chat_msg(self, encrypt_random_key: str,
                         encrypt_chat_msg: str) -> dict:
        """
        Decrypt a single chat message.
        encrypt_random_key: base64-encoded RSA-encrypted AES key
        encrypt_chat_msg:   base64-encoded AES-encrypted message JSON
        """
        if not self._initialized:
            self.init()

        slice_handle = self._dll.NewSlice()

        ret = self._dll.DecryptData(
            encrypt_random_key.encode("utf-8"),
            encrypt_chat_msg.encode("utf-8"),
            slice_handle,
        )

        if ret != 0:
            self._dll.FreeSlice(slice_handle)
            raise RuntimeError(f"DecryptData failed with code {ret}")

        content_ptr = self._dll.GetContentFromSlice(slice_handle)
        raw = ctypes.string_at(content_ptr).decode("utf-8", errors="replace")
        self._dll.FreeSlice(slice_handle)

        return json.loads(raw)

    def destroy(self):
        """Clean up SDK resources."""
        if self._sdk is not None and self._dll is not None:
            self._dll.DestroySdk(self._sdk)
            self._sdk = None
            self._initialized = False

    def __del__(self):
        try:
            self.destroy()
        except Exception:
            pass


# =====================================================
# SOFTWARE-BASED DECRYPTION (no SDK library needed)
# =====================================================

def decrypt_message_software(encrypt_random_key: str, encrypt_chat_msg: str,
                             private_key) -> dict:
    """
    Decrypt a chat message using pure Python (no native SDK).
    Uses RSA to decrypt the random key, then AES to decrypt the message body.
    This is a fallback when the SDK library is unavailable.
    """
    # Step 1: RSA decrypt the random key → AES key
    aes_key = rsa_decrypt_key(encrypt_random_key, private_key)

    # Step 2: AES decrypt the message body
    decrypted = aes_decrypt_message(encrypt_chat_msg, aes_key)

    return json.loads(decrypted)


# =====================================================
# MESSAGE FETCHER — Orchestrates SDK + decryption
# =====================================================

class MessageFetcher:
    """
    High-level fetcher that combines SDK data pulling with decryption.
    Handles pagination, cursor tracking, and message formatting.
    """

    def __init__(self, corpid: str, secret: str, private_key,
                 sdk_lib_path: str = ""):
        self.corpid = corpid
        self.secret = secret
        self.private_key = private_key
        self.sdk_lib_path = sdk_lib_path
        self.sdk = None
        self._use_sdk = bool(sdk_lib_path and os.path.exists(sdk_lib_path))
        if self._use_sdk:
            self.sdk = WeComFinanceSDK(corpid, secret, sdk_lib_path)

    def fetch_messages(self, seq: int = 0, limit: int = 500,
                       max_messages: int = None,
                       decrypt: bool = True) -> List[dict]:
        """
        Fetch messages starting from `seq`, auto-paginating.
        Returns list of decrypted message dicts.
        """
        all_messages = []
        current_seq = seq
        pages = 0

        log.info("Starting message fetch from seq=%d", current_seq)

        while True:
            pages += 1
            if max_messages and len(all_messages) >= max_messages:
                break

            # Fetch raw data
            if self._use_sdk:
                raw_messages = self.sdk.get_chat_data(
                    current_seq, limit=min(limit, 1000)
                )
            else:
                raise RuntimeError(
                    "SDK library is required for message fetching. "
                    "Set WECOM_SDK_LIB_PATH to the native library path."
                )

            if not raw_messages:
                log.info("No more messages (empty page at seq=%d)", current_seq)
                break

            # Decrypt each message
            for msg in raw_messages:
                try:
                    if decrypt:
                        decrypted = self.sdk.decrypt_chat_msg(
                            msg["encrypt_random_key"],
                            msg["encrypt_chat_msg"],
                        )
                    else:
                        decrypted = None
                except Exception as e:
                    # Fallback: try software decryption
                    try:
                        if decrypt and self.private_key:
                            decrypted = decrypt_message_software(
                                msg["encrypt_random_key"],
                                msg["encrypt_chat_msg"],
                                self.private_key,
                            )
                        else:
                            decrypted = None
                    except Exception:
                        log.warning(
                            "Failed to decrypt msgid=%s: %s",
                            msg.get("msgid", "?"), e,
                        )
                        decrypted = {"_error": str(e), "_raw_encrypted": True}

                all_messages.append({
                    "msgid": msg.get("msgid"),
                    "seq": msg.get("seq"),
                    "action": msg.get("action"),
                    "from": msg.get("from"),
                    "tolist": msg.get("tolist", []),
                    "roomid": msg.get("roomid"),
                    "msgtime": msg.get("msgtime"),
                    "msgtype": msg.get("msgtype"),
                    "content": decrypted,
                })

            # Update cursor to the last seq
            current_seq = raw_messages[-1]["seq"]
            log.info(
                "Page %d: fetched %d messages (total: %d, next_seq=%d)",
                pages, len(raw_messages), len(all_messages), current_seq,
            )

            # If fewer than requested, we reached the end
            if len(raw_messages) < limit:
                break

            # Safety: max pages
            if pages >= 100:
                log.warning("Reached 100 pages, stopping for safety")
                break

        log.info("Fetch complete: %d messages across %d pages", len(all_messages), pages)
        return all_messages

    def destroy(self):
        if self.sdk:
            self.sdk.destroy()


# =====================================================
# MESSAGE FORMATTING & EXPORT
# =====================================================

def format_message_summary(msg: dict) -> str:
    """Format a single message as a human-readable summary line."""
    msgtype = msg.get("msgtype", "?")
    type_name = MSGTYPE_MAP.get(msgtype, f"type_{msgtype}")
    sender = msg.get("from", "?")
    roomid = msg.get("roomid", "")
    ts = msg.get("msgtime", 0)
    dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"

    content = msg.get("content", {})
    if isinstance(content, dict) and not content.get("_error"):
        # Extract text preview from content
        text_preview = ""
        if content:
            if isinstance(content, dict):
                text_preview = content.get("content", "") or content.get("text", "")
                if isinstance(text_preview, str):
                    text_preview = text_preview[:80]
                else:
                    text_preview = json.dumps(content, ensure_ascii=False)[:80]
            else:
                text_preview = str(content)[:80]
        return f"[{dt}] {sender} → room:{roomid} | {type_name} | {text_preview}"
    else:
        return f"[{dt}] {sender} → room:{roomid} | {type_name} | (encrypted/failed)"


def export_to_json(messages: List[dict], output_path: str):
    """Export messages to JSON file."""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2, default=str)
    log.info("Exported %d messages to %s", len(messages), output_path)


def export_to_csv(messages: List[dict], output_path: str):
    """Export messages to CSV file (flattened format)."""
    import csv
    rows = []
    for msg in messages:
        content = msg.get("content", {})
        if isinstance(content, dict):
            text = content.get("content", "") or content.get("text", "")
            if isinstance(text, (dict, list)):
                text = json.dumps(text, ensure_ascii=False)
        else:
            text = str(content) if content else ""

        rows.append({
            "msgid": msg.get("msgid", ""),
            "seq": msg.get("seq", ""),
            "time": datetime.fromtimestamp(msg.get("msgtime", 0)).isoformat(),
            "sender": msg.get("from", ""),
            "roomid": msg.get("roomid", ""),
            "msgtype": MSGTYPE_MAP.get(msg.get("msgtype", 0), str(msg.get("msgtype"))),
            "content": text[:1000] if text else "",
        })

    if rows:
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        log.info("Exported %d messages to %s", len(rows), output_path)


def build_conversation_map(messages: List[dict]) -> dict:
    """
    Build a conversation map: {roomid: [messages sorted by time]}.
    Also tracks single-chat conversations by participant pairs.
    """
    conversations = {}

    for msg in messages:
        roomid = msg.get("roomid", "")
        if not roomid:
            # Single chat: use sorted pair of from/to as key
            sender = msg.get("from", "")
            receivers = msg.get("tolist", [])
            for r in receivers:
                pair = "|".join(sorted([sender, r]))
                conversations.setdefault(pair, []).append(msg)
        else:
            conversations.setdefault(roomid, []).append(msg)

    # Sort within each conversation
    for key in conversations:
        conversations[key].sort(key=lambda m: m.get("msgtime", 0))

    return conversations


# =====================================================
# HIGH-LEVEL OPERATIONS (REST-ONLY — no SDK needed)
# =====================================================


def list_all_group_chats(token: str) -> List[dict]:
    """List all customer group chats with full pagination."""
    all_groups = []
    cursor = ""
    while True:
        data = get_groupchat_list(token, cursor=cursor)
        groups = data.get("group_chat_list", [])
        all_groups.extend(groups)
        cursor = data.get("next_cursor", "")
        if not cursor or len(groups) == 0:
            break
    return all_groups


def get_groupchat_with_members(token: str, chat_id: str) -> dict:
    """Get group chat detail with member name resolution."""
    detail = get_groupchat_detail(token, chat_id)
    gc = detail.get("group_chat", {})

    # Resolve member names
    enriched_members = []
    for member in gc.get("member_list", []):
        userid = member.get("userid", "")
        mtype = member.get("type", 0)  # 1=internal, 2=external
        name = member.get("unionid", "") or member.get("userid", "")

        # Try to get user name
        if mtype == 1 and userid:
            try:
                user = get_user_detail(token, userid)
                name = user.get("name", name)
            except Exception:
                pass
        elif mtype == 2 and userid:
            try:
                ext = get_external_contact_detail(token, userid)
                contact = ext.get("external_contact", {})
                name = contact.get("name", name)
            except Exception:
                pass

        enriched_members.append({
            "userid": userid,
            "type": "internal" if mtype == 1 else "external",
            "name": name,
            "join_time": member.get("join_time", 0),
        })

    return {
        "chat_id": gc.get("chat_id", ""),
        "name": gc.get("name", ""),
        "owner": gc.get("owner", ""),
        "create_time": gc.get("create_time", 0),
        "notice": gc.get("notice", ""),
        "member_count": len(enriched_members),
        "members": enriched_members,
    }


# =====================================================
# MAIN
# =====================================================

def main():
    parser = argparse.ArgumentParser(
        description="WeCom Session Archive Fetcher — 企业微信会话内容存档数据提取",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python wecom_archive.py                              # List all customer groups
  python wecom_archive.py --chat-id wrXXXXX            # Get single group detail
  python wecom_archive.py --sync-msg                   # Sync messages (SDK required)
  python wecom_archive.py --sync-msg --cursor 0 --limit 500
  python wecom_archive.py --sync-msg --export json --output messages.json
  python wecom_archive.py --dry-run --sync-msg
        """,
    )

    # Config overrides
    parser.add_argument("--corpid", default=CORPID, help="Enterprise WeChat corpid")
    parser.add_argument("--secret", default=SECRET, help="App secret for REST APIs")
    parser.add_argument("--chat-secret", default=CHAT_SECRET,
                        help="Session archive secret (may differ from --secret)")
    parser.add_argument("--pri-key-path", default=PRI_KEY_PATH,
                        help="RSA private key PEM file path")
    parser.add_argument("--sdk-lib-path", default=SDK_LIB_PATH,
                        help="Native SDK library path (.so/.dll)")

    # Operations (REST)
    parser.add_argument("--list-groups", action="store_true",
                        help="List all customer group chats")
    parser.add_argument("--chat-id", default=None,
                        help="Get details for a specific group chat")
    parser.add_argument("--permit-users", action="store_true",
                        help="List users with session archive enabled")

    # Operations (SDK)
    parser.add_argument("--sync-msg", action="store_true",
                        help="Sync messages from session archive (needs SDK)")
    parser.add_argument("--cursor", type=int, default=0,
                        help="Message seq cursor to start from (default: 0)")
    parser.add_argument("--limit", type=int, default=500,
                        help="Messages per page (default: 500, max: 1000)")
    parser.add_argument("--max-messages", type=int, default=None,
                        help="Maximum total messages to fetch")

    # Output
    parser.add_argument("--export", choices=["json", "csv"], default=None,
                        help="Export format for synced messages")
    parser.add_argument("--output", "-o", default="",
                        help="Output file path (auto-generated if not specified)")
    parser.add_argument("--no-decrypt", action="store_true",
                        help="Fetch raw encrypted messages (no decryption)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate config only, don't fetch data")

    args = parser.parse_args()

    # ---------------------------------------
    # Validate config
    # ---------------------------------------
    corpid = args.corpid
    secret = args.secret
    chat_secret = args.chat_secret or secret  # often the same

    if not corpid:
        print("ERROR: WECOM_CORPID not set. Use --corpid or set env var.")
        sys.exit(1)
    if not secret:
        print("ERROR: WECOM_SECRET not set. Use --secret or set env var.")
        sys.exit(1)

    print(f"Corpid: {corpid}")
    print(f"REST API secret: {'***' if secret else '(not set)'}")
    print(f"Chat archive secret: {'***' if chat_secret else '(not set)'}")

    if args.sync_msg:
        if not args.sdk_lib_path:
            print("WARNING: No SDK library path set. "
                  "Message sync requires the native library.")
            print("  Linux:   libWeWorkFinanceSdk_C.so")
            print("  Windows: WeWorkFinanceSdk.dll")
            print("  Set WECOM_SDK_LIB_PATH or --sdk-lib-path")
            if not args.dry_run:
                print("  Continuing without SDK — will fail if sync is attempted.")
        elif not os.path.exists(args.sdk_lib_path):
            print(f"ERROR: SDK library not found at: {args.sdk_lib_path}")
            sys.exit(1)

        if not args.no_decrypt and not args.pri_key_path:
            print("WARNING: No private key path set. "
                  "Messages will be fetched but cannot be decrypted.")
            print("  Set WECOM_PRI_KEY_PATH or --pri-key-path")

    # Load private key if available
    private_key = None
    if args.pri_key_path and os.path.exists(args.pri_key_path):
        try:
            private_key = load_rsa_private_key(args.pri_key_path)
            print(f"RSA private key loaded: {args.pri_key_path}")
        except Exception as e:
            print(f"WARNING: Failed to load private key: {e}")

    if args.dry_run:
        print("\n[DRY RUN] Config validated. Would connect to:")
        print(f"  REST API: {WECOM_API_BASE}")
        if args.sync_msg:
            print(f"  SDK: {args.sdk_lib_path or '(not set)'}")
            print(f"  Cursor: {args.cursor}")
            print(f"  Limit/page: {args.limit}")
            print(f"  Decrypt: {not args.no_decrypt}")
        print("\n[DRY RUN] No data fetched.")
        return

    # ---------------------------------------
    # REST API Operations
    # ---------------------------------------
    token = get_access_token(corpid, secret)

    # --- List permit users ---
    if args.permit_users:
        print("\n=== Users with Session Archive Enabled ===")
        try:
            data = get_permit_user_list(token)
            ids = data.get("ids", [])
            print(f"Found {len(ids)} permitted users:")
            for uid in ids:
                print(f"  {uid}")
        except Exception as e:
            print(f"ERROR: {e}")

    # --- List all group chats ---
    if args.list_groups or (not args.sync_msg and not args.chat_id and not args.permit_users):
        print("\n=== Customer Group Chat List ===")
        try:
            groups = list_all_group_chats(token)
            print(f"Total customer groups: {len(groups)}")
            print()
            for g in groups[:50]:  # Limit display to 50
                name = g.get("name", "(unnamed)")
                chat_id = g.get("chat_id", "")
                owner = g.get("owner", "")
                create_time = g.get("create_time", 0)
                ct_str = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d") if create_time else "?"
                print(f"  [{ct_str}] {name}")
                print(f"           chat_id: {chat_id}  owner: {owner}")
            if len(groups) > 50:
                print(f"  ... and {len(groups) - 50} more groups")
        except Exception as e:
            print(f"ERROR listing groups: {e}")

    # --- Single group chat detail ---
    if args.chat_id:
        print(f"\n=== Group Chat Detail: {args.chat_id} ===")
        try:
            detail = get_groupchat_with_members(token, args.chat_id)
            print(f"Name:    {detail['name']}")
            print(f"Owner:   {detail['owner']}")
            print(f"Created: {datetime.fromtimestamp(detail['create_time']).strftime('%Y-%m-%d %H:%M') if detail['create_time'] else '?'}")
            print(f"Notice:  {detail['notice'][:200] if detail['notice'] else '(none)'}")
            print(f"Members: {detail['member_count']}")
            print()
            print("  Member List:")
            for m in detail['members']:
                tag = "[内]" if m['type'] == 'internal' else "[外]"
                print(f"    {tag} {m['name']} ({m['userid']})")
        except Exception as e:
            print(f"ERROR: {e}")
            # Try internal group chat API as fallback
            print("  Trying internal group API...")
            try:
                result = get_internal_groupchat(token, args.chat_id)
                if result.get("errcode") == 0:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print(f"  Also failed: {result}")
            except Exception as e2:
                print(f"  Also failed: {e2}")

    # ---------------------------------------
    # SDK Operations — Message Sync
    # ---------------------------------------
    if args.sync_msg:
        print(f"\n=== Syncing Messages (SDK mode) ===")
        print(f"  Starting cursor: {args.cursor}")
        print(f"  Limit per page:  {args.limit}")
        print(f"  Decrypt:         {not args.no_decrypt}")

        if not args.sdk_lib_path or not os.path.exists(args.sdk_lib_path):
            print("\nERROR: Cannot sync messages without the SDK library.")
            print("  Download the SDK from the WeChat Work admin panel.")
            print("  Set --sdk-lib-path to the path of:")
            print("    Linux:   libWeWorkFinanceSdk_C.so")
            print("    Windows: WeWorkFinanceSdk.dll")
            sys.exit(1)

        fetcher = MessageFetcher(
            corpid=corpid,
            secret=chat_secret,
            private_key=private_key,
            sdk_lib_path=args.sdk_lib_path,
        )

        try:
            messages = fetcher.fetch_messages(
                seq=args.cursor,
                limit=args.limit,
                max_messages=args.max_messages,
                decrypt=not args.no_decrypt,
            )

            print(f"\nFetched {len(messages)} messages")

            # Print summary
            if messages:
                print("\n--- Message Previews (first 20) ---")
                for msg in messages[:20]:
                    print(format_message_summary(msg))

                # Conversation stats
                convos = build_conversation_map(messages)
                print(f"\n--- Conversation Stats ---")
                print(f"Total conversations: {len(convos)}")
                for conv_id, msgs in sorted(
                    convos.items(),
                    key=lambda x: len(x[1]),
                    reverse=True,
                )[:10]:
                    print(f"  {conv_id[:30]:30s}  {len(msgs)} messages")

            # Export
            if args.export and messages:
                fmt = args.export
                out_path = args.output or (
                    f"wecom_messages_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt}"
                )
                if fmt == "json":
                    export_to_json(messages, out_path)
                elif fmt == "csv":
                    export_to_csv(messages, out_path)

        finally:
            fetcher.destroy()

    print("\n[DONE]")


if __name__ == "__main__":
    main()
