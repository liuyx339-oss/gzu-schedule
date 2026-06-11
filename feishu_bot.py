"""
Feishu Bot - Daily Reservation Reporter
飞书机器人 — 每日预约人数播报

每天自动拉取体检人群和OB超声的明日预约数据，
格式化发送两条消息到指定飞书群聊。

Usage:
  python feishu_bot.py                     # 发送到配置的群聊
  python feishu_bot.py --discover-chats    # 发现机器人所在的所有群聊
  python feishu_bot.py --chat-id oc_xxx    # 发送到指定群聊
  python feishu_bot.py --target 2026-06-15 # 指定目标日期
"""

import os
import sys
import json
from datetime import date, timedelta

import requests
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from fetch_feishu_data import (
    get_tenant_access_token, get_field_meta, get_all_records, records_to_dataframe,
)
from fetch_real_reservations import process_table_a, process_table_b

# =====================================================
# CONFIG
# =====================================================

APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aaa8d24639b8dcd8")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "b0ayVQKIuUGmvzRu9YCm9gpZHUzniNz1")

# Target chat_id (set via env var or --chat-id)
CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "")

# Table configs
BASE_CHECKUP = "NM6HbB8gKaqtDysTTrRcve0ZnAc"
TABLE_CHECKUP = "tblrUOmKxEmHxCxa"
BASE_OB = "XDa9w6qGBigqGNkOENvctCJtnqd"
TABLE_OB = "tbltb1eix0QOEcQP"

FEISHU_API = "https://open.feishu.cn/open-apis"

# =====================================================
# HELPERS
# =====================================================


def _fetch_data(token, target_date):
    """Pull data from both Feishu tables."""
    # Table A: Checkup
    field_map_a = get_field_meta(token, BASE_CHECKUP, TABLE_CHECKUP)
    records_a = get_all_records(token, BASE_CHECKUP, TABLE_CHECKUP)
    df_a = records_to_dataframe(records_a, field_map_a)
    result_a = process_table_a(df_a, target_date)

    # Table B: OB Ultrasound
    try:
        field_map_b = get_field_meta(token, BASE_OB, TABLE_OB)
        records_b = get_all_records(token, BASE_OB, TABLE_OB)
        df_b = records_to_dataframe(records_b, field_map_b)
        result_b = process_table_b(df_b, target_date)
    except Exception as e:
        print(f"[WARN] OB table fetch failed: {e}")
        result_b = {"time_slots": [], "counts": {}, "total_counts": {}}

    return result_a, result_b


# =====================================================
# MESSAGE FORMATTING
# =====================================================


def _format_summary(target_date, result_a, result_b):
    """Format the summary message (text)."""
    wday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    wday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    w = wday_cn[target_date.weekday()]

    lines = [
        f"Daily Demand Report - {target_date} ({w})",
        "",
    ]

    # Checkup
    persons = result_a.get("total_persons", 0)
    totals = result_a.get("total_counts", {})
    tech_m = result_a.get("total_tech_minutes", 0)
    doc_m = result_a.get("total_doc_minutes", 0)

    lines.append(f"[Checkup]: {persons} people")
    modality_parts = []
    for label in ["CT", "X-ray", "B-ultrasound", "Echo", "Mammo", "BoneDensity", "MRI"]:
        n = totals.get(label, 0)
        if n > 0:
            cn = {"CT": "CT", "X-ray": "X-ray", "B-ultrasound": "B-ultrasound",
                  "Echo": "Echo", "Mammo": "Mammo", "BoneDensity": "BoneDensity", "MRI": "MRI"}
            modality_parts.append(f"{cn.get(label, label)}:{n}")
    if modality_parts:
        lines.append(" | ".join(modality_parts))
    lines.append(f"Est operation: {tech_m}min ({tech_m/60:.1f}h) | Est report: {doc_m}min ({doc_m/60:.1f}h)")

    # OB
    lines.append("")
    ob_totals = result_b.get("total_counts", {})
    ob_class_names = {"OB": "OB", "NT": "NT", "Anatomy": "Anatomy"}
    ob_parts = []
    for cls in ["OB", "NT", "Anatomy"]:
        n = ob_totals.get(cls, 0)
        if n > 0:
            ob_parts.append(f"{ob_class_names.get(cls, cls)}:{n}")
    if ob_parts:
        lines.append(f"[OB Ultrasound]: {', '.join(ob_parts)}")
    else:
        lines.append("[OB Ultrasound]: No reservations yet")

    return "\n".join(lines)


def _format_detail(target_date, result_a, result_b):
    """Format the detailed breakdown message (text with table)."""
    wday_cn = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    w = wday_cn[target_date.weekday()]

    lines = [
        f"Detail Breakdown - {target_date} ({w})",
        "",
    ]

    # Checkup detail
    if result_a.get("time_slots"):
        lines.append("=== Checkup ===")
        times = result_a["time_slots"]
        persons = result_a.get("person_count", [])
        svc_detail = result_a.get("service_breakdown", {})

        for i, ts in enumerate(times):
            p = persons[i] if i < len(persons) else 0
            # Build modality tags
            tag_parts = []
            for label in ["CT", "X-ray", "B-ultrasound", "Echo", "Mammo", "BoneDensity", "MRI"]:
                counts = result_a.get("counts", {}).get(label, [])
                n = counts[i] if i < len(counts) else 0
                if n > 0:
                    cn = {"CT": "CT", "X-ray": "XR", "B-ultrasound": "B", "Echo": "Echo",
                          "Mammo": "Mam", "BoneDensity": "BD", "MRI": "MRI"}
                    tag_parts.append(f"{cn.get(label, label)}:{n}")

            line = f"  {ts}  |  {p}p  |  {' '.join(tag_parts)}"
            lines.append(line)

            # Add service names for this slot
            svcs = svc_detail.get(ts, {})
            if svcs:
                svc_items = []
                for svc_name, cnt in svcs.items():
                    short = svc_name[:45] + ".." if len(svc_name) > 47 else svc_name
                    svc_items.append(f"{short} x{cnt}")
                for item in svc_items[:3]:  # max 3 per slot
                    lines.append(f"         {item}")
                if len(svc_items) > 3:
                    lines.append(f"         ... and {len(svc_items)-3} more")
    else:
        lines.append("[Checkup]: No data")

    # OB detail
    lines.append("")
    if result_b.get("time_slots"):
        lines.append("=== OB Ultrasound ===")
        times = result_b["time_slots"]
        class_cn = {"OB": "OB", "NT": "NT", "Anatomy": "Anatomy"}
        for i, ts in enumerate(times):
            parts = []
            for cls in ["OB", "NT", "Anatomy"]:
                counts = result_b.get("counts", {}).get(cls, [])
                n = counts[i] if i < len(counts) else 0
                if n > 0:
                    parts.append(f"{class_cn.get(cls, cls)}:{n}")
            if parts:
                lines.append(f"  {ts}  |  {' '.join(parts)}")
    else:
        lines.append("[OB Ultrasound]: No data")

    return "\n".join(lines)


# =====================================================
# MESSAGE SENDING
# =====================================================


def send_text_message(token, chat_id, text):
    """Send a text message to a Feishu chat."""
    url = f"{FEISHU_API}/im/v1/messages?receive_id_type=chat_id"
    content = json.dumps({"text": text}, ensure_ascii=False)
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": content,
    }
    session = requests.Session()
    session.trust_env = False
    resp = session.post(
        url,
        json=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=30,
    )
    data = resp.json()
    if data.get("code") == 0:
        print(f"  [OK] Message sent, message_id={data.get('data', {}).get('message_id', '?')}")
        return True
    else:
        print(f"  [FAIL] code={data.get('code')}, msg={data.get('msg')}")
        return False


def discover_chats(token):
    """List all chats the bot is in."""
    url = f"{FEISHU_API}/im/v1/chats"
    session = requests.Session()
    session.trust_env = False
    resp = session.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 50},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") == 0:
        items = data.get("data", {}).get("items", [])
        print(f"\nBot is in {len(items)} chat(s):")
        for chat in items:
            print(f"  chat_id={chat.get('chat_id')}, name=\"{chat.get('name', '?')}\", type={chat.get('chat_type', '?')}")
        return items
    else:
        print(f"[FAIL] Cannot list chats: code={data.get('code')}, msg={data.get('msg')}")
        print("Make sure im:chat permission is granted AND app version is published.")
        return []


# =====================================================
# MAIN
# =====================================================


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Feishu Bot - Daily Reservation Reporter")
    parser.add_argument("--discover-chats", action="store_true", help="List all chats the bot is in")
    parser.add_argument("--chat-id", type=str, default=None, help="Target chat_id (overrides env FEISHU_CHAT_ID)")
    parser.add_argument("--target", type=str, default=None, help="Target date YYYY-MM-DD (default: tomorrow)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch data and print messages, but do NOT send")
    args = parser.parse_args()

    # Determine target date
    if args.target:
        target_date = date.fromisoformat(args.target)
    else:
        target_date = date.today() + timedelta(days=1)

    # Determine chat_id
    chat_id = args.chat_id or CHAT_ID

    print("=" * 60)
    print("Feishu Bot - Daily Reservation Reporter")
    print(f"Target date: {target_date}")
    print("=" * 60)

    # Get token
    print("\n[1] Getting Feishu access token...")
    token = get_tenant_access_token(APP_ID, APP_SECRET)

    # Discover chats mode
    if args.discover_chats:
        discover_chats(token)
        return

    # Fetch data
    print("\n[2] Fetching reservation data...")
    result_a, result_b = _fetch_data(token, target_date)

    # Format messages
    print("\n[3] Formatting messages...")
    summary = _format_summary(target_date, result_a, result_b)
    detail = _format_detail(target_date, result_a, result_b)

    print("\n--- Summary ---")
    print(summary)
    print("\n--- Detail ---")
    print(detail)

    # Send
    if args.dry_run:
        print("\n[DRY RUN] Messages NOT sent.")
        return

    if not chat_id:
        print("\n[WARN] No chat_id configured!")
        print("Set FEISHU_CHAT_ID env var, use --chat-id, or run --discover-chats first.")
        return

    print(f"\n[4] Sending messages to chat {chat_id}...")
    ok1 = send_text_message(token, chat_id, summary)
    ok2 = send_text_message(token, chat_id, detail)

    if ok1 and ok2:
        print("\n[DONE] Both messages sent successfully!")
    else:
        print("\n[WARN] Some messages failed to send.")


if __name__ == "__main__":
    main()
