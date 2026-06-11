"""
Feishu Bot - Daily Reservation Reporter
Self-contained, no local-file dependencies. Uses env vars only.

Usage:
  python feishu_bot.py                     # Send to configured chat
  python feishu_bot.py --chat-id oc_xxx    # Send to specific chat
  python feishu_bot.py --target 2026-06-15 # Specific target date
  python feishu_bot.py --dry-run           # Print only, don't send
"""

import os
import sys
import json
import re
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

# =====================================================
# CONFIG (from env vars only - no hardcoded secrets)
# =====================================================

APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "")

BASE_CHECKUP = "NM6HbB8gKaqtDysTTrRcve0ZnAc"
TABLE_CHECKUP = "tblrUOmKxEmHxCxa"
BASE_OB = "XDa9w6qGBigqGNkOENvctCJtnqd"
TABLE_OB = "tbltb1eix0QOEcQP"

FEISHU_API = "https://open.feishu.cn/open-apis"

ESTIMATES = {
    "MRI": (30, 20), "CT": (15, 10), "X-ray": (10, 5),
    "Mammo": (10, 5), "BoneDensity": (10, 5),
    "B-ultrasound": (20, 10), "Echo": (20, 10),
}
MODALITY_LABELS = list(ESTIMATES.keys())
OB_CLASSES = ["OB", "NT", "Anatomy"]

SERVICE_EXACT_MAP = {
    "NT ultrasound prenatal screening prescription service": "NT",
    "Prenatal Care Package (Week 6-10)": "OB",
    "Prenatal Care Package (Week 10-13)": "NT",
    "Prenatal Care Package (Week 20)": "Anatomy",
    "Prenatal Care Package (Week 32)": "OB",
    "Prenatal Care Package (Week 38)": "OB",
}

SECONDARY_SERVICE_PREFIXES = [
    "Prenatal Care Package (Week 16)", "Prenatal Care Package (Week 24)",
    "Prenatal Care Package (Week 28)", "Prenatal Care Package (Week 30)",
    "Prenatal Care Package (Week 34)", "Prenatal Care Package (Week 36)",
    "Prenatal Care Package (Week 37)", "Prenatal Care Package (Week 39)",
    "Prenatal Care Package (Week 40)", "New to GYN Clinic",
    "New to OB Clinic", "Breastfeeding Consultation",
    "Established GYN Visit", "Established Prenatal Patient Visit to OB Clinic",
    "Prenatal Genetic Counseling Clinic", "Simple consultation booking",
    "Video follow-up for Internet hospital",
    "12w prenatal screening prescription service", "20w prenatal screening prescription service",
    "Follicle Monitor", "GYN Pre-op treatment/assessment",
    "High-Risk Pregnancy Consultation Clinic",
    "Infertility Initial Consult", "Infertility Treatment Plan Review",
]

# =====================================================
# FEISHU API HELPERS (inlined from fetch_feishu_data)
# =====================================================

_session = None

def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.trust_env = False
    return _session


def get_tenant_access_token(app_id, app_secret):
    """Get Feishu tenant_access_token (valid ~2h)."""
    url = f"{FEISHU_API}/auth/v3/tenant_access_token/internal"
    resp = _get_session().post(
        url,
        json={"app_id": app_id, "app_secret": app_secret},
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=(10, 60),
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Auth failed: code={data.get('code')} msg={data.get('msg')}")
    return data["tenant_access_token"]


def get_field_meta(token, base_token, table_id):
    """Get field metadata: {field_id: {name, type}}."""
    url = f"{FEISHU_API}/bitable/v1/apps/{base_token}/tables/{table_id}/fields"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"page_size": 100}
    field_map = {}
    session = _get_session()
    while True:
        resp = session.get(url, headers=headers, params=params, timeout=(10, 60))
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Field meta failed: {data.get('msg')}")
        for item in data.get("data", {}).get("items", []):
            fid = item.get("field_id", "")
            ftype_raw = item.get("type", "")
            ftype = ftype_raw if isinstance(ftype_raw, str) else str(ftype_raw)
            if fid:
                field_map[fid] = {"name": item.get("field_name", ""), "type": ftype}
        if data.get("data", {}).get("has_more", False):
            params["page_token"] = data["data"]["page_token"]
        else:
            break
    return field_map


def get_all_records(token, base_token, table_id, max_records=None):
    """Fetch all records from a Bitable table."""
    url = f"{FEISHU_API}/bitable/v1/apps/{base_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"page_size": 500}
    records = []
    session = _get_session()
    page = 0
    while True:
        page += 1
        resp = session.get(url, headers=headers, params=params, timeout=(10, 120))
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Records failed: {data.get('msg')}")
        items = data.get("data", {}).get("items", [])
        records.extend(items)
        if max_records and len(records) >= max_records:
            records = records[:max_records]
            break
        if data.get("data", {}).get("has_more", False):
            params["page_token"] = data["data"]["page_token"]
        else:
            break
    return records


def records_to_dataframe(records, field_map):
    """Convert Feishu records to DataFrame with Chinese column names."""
    rows = []
    for rec in records:
        row = {}
        fields = rec.get("fields", {})
        for fid, fvalue in fields.items():
            col_info = field_map.get(fid, {})
            col_name = col_info.get("name", fid)
            col_type = col_info.get("type", "Text")
            row[col_name] = _extract_value(fvalue, col_type)
        rows.append(row)
    return pd.DataFrame(rows)


def _extract_value(fvalue, col_type):
    """Extract plain-text value from a Feishu field value."""
    if fvalue is None:
        return None
    t = str(col_type).lower() if col_type else "text"
    if isinstance(fvalue, (int, float)):
        return fvalue
    if isinstance(fvalue, list):
        # Text: list of {"text": "..."}
        if len(fvalue) > 0 and isinstance(fvalue[0], dict):
            texts = [item.get("text", "") for item in fvalue if isinstance(item, dict)]
            return "".join(texts)
        return ", ".join(str(v) for v in fvalue)
    if isinstance(fvalue, dict):
        return fvalue.get("text", str(fvalue))
    if isinstance(fvalue, str):
        # Check if it's a numeric timestamp string
        try:
            ts = int(fvalue)
            if ts > 1000000000000:  # looks like ms timestamp
                return fvalue  # Return raw, let caller handle
        except (ValueError, TypeError):
            pass
        return fvalue
    return str(fvalue)


# =====================================================
# DATA PROCESSING (from fetch_real_reservations)
# =====================================================


def _fuzzy_find(cols, keywords):
    for kw in keywords:
        for col in cols:
            if kw.lower() in str(col).lower():
                return col
    return None


def _discover_modality_columns(cols):
    cn_labels = {
        "MRI": ["MRI", "磁共振", "核磁"],
        "CT": ["CT", "CT扫描"],
        "X-ray": ["X-ray", "X线", "X光", "DR", "X - ray", "X - rays"],
        "Mammo": ["Mammo", "Mammogram", "钼靶"],
        "BoneDensity": ["BoneDensity", "骨密度", "DXA"],
        "B-ultrasound": ["B-ultrasound", "B超", "Ultrasound", "超声"],
        "Echo": ["Echo", "Echocardiogram", "心彩", "心超", "心脏超声", "Transthoracic"],
    }
    mapping = {}
    for label, keywords in cn_labels.items():
        for c in cols:
            cs = str(c).strip()
            for kw in keywords:
                if kw.lower() in cs.lower():
                    mapping[label] = c
                    break
            if label in mapping:
                break
    return mapping


def _sort_key_time(ts):
    m = re.match(r'(\d{1,2}):(\d{2})', str(ts))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 9999


def process_table_a(df, target_date):
    """Process checkup table: filter, group by time, count modalities."""
    UNCONFIRMED_KEYWORD = "未确认套餐"
    cols = list(df.columns)

    appt_col = _fuzzy_find(cols, ["appt_dt", "appt", "预约日期", "预约时间", "date", "日期"])
    if appt_col and appt_col in df.columns:
        ts_num = pd.to_numeric(df[appt_col], errors="coerce")
        df[appt_col] = pd.to_datetime(ts_num, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
        df = df[df[appt_col].dt.date == target_date].copy()

    if len(df) == 0:
        return _empty_checkup_result()

    time_col = _fuzzy_find(cols, ["time_slot", "时段", "时间", "time", "预约时段"])
    if time_col and time_col in df.columns:
        t_num = pd.to_numeric(df[time_col], errors="coerce")
        df["_ts"] = pd.to_datetime(t_num, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%H:%M")
    elif appt_col and appt_col in df.columns:
        df["_ts"] = df[appt_col].dt.strftime("%H:%M")
    else:
        df["_ts"] = "Unknown"

    df["_ts"] = df["_ts"].fillna("Unknown")

    modality_cols = _discover_modality_columns(cols)
    service_col = _fuzzy_find(cols, ["service_desc", "服务描述", "检查项目", "医嘱描述", "order_item", "description", "内容"])
    unconfirmed_col = _fuzzy_find(cols, ["套餐", "未确认", "未定"])

    time_slots = sorted(df["_ts"].unique(), key=_sort_key_time)

    result = {
        "time_slots": time_slots,
        "counts": {label: [] for label in MODALITY_LABELS},
        "person_count": [],
        "total_counts": {label: 0 for label in MODALITY_LABELS},
        "total_persons": 0,
        "total_tech_minutes": 0,
        "total_doc_minutes": 0,
    }

    for ts in time_slots:
        slot_df = df[df["_ts"] == ts]
        n_persons = len(slot_df)
        result["person_count"].append(n_persons)
        result["total_persons"] += n_persons
        slot_counts = {label: 0 for label in MODALITY_LABELS}

        for _, row in slot_df.iterrows():
            for label, mcol in modality_cols.items():
                if mcol in slot_df.columns:
                    n = 0
                    try:
                        n = int(float(row.get(mcol, 0))) if pd.notna(row.get(mcol, 0)) else 0
                    except:
                        pass
                    if label == "B-ultrasound":
                        slot_counts[label] += 1 if n > 0 else 0
                    else:
                        slot_counts[label] += n

        # Unconfirmed handling
        for _, row in slot_df.iterrows():
            is_unconfirmed = False
            if unconfirmed_col and unconfirmed_col in slot_df.columns:
                val = str(row.get(unconfirmed_col, "")).strip().upper()
                if val in ('Y', 'YES', 'TRUE') or '未确认' in val:
                    is_unconfirmed = True
            if not is_unconfirmed and service_col and service_col in slot_df.columns:
                if UNCONFIRMED_KEYWORD in str(row.get(service_col, "")):
                    is_unconfirmed = True
            if is_unconfirmed:
                slot_counts["CT"] += 1
                slot_counts["B-ultrasound"] += 1

        for label in MODALITY_LABELS:
            result["counts"][label].append(slot_counts[label])
            result["total_counts"][label] += slot_counts[label]

        for label in MODALITY_LABELS:
            result["total_tech_minutes"] += slot_counts[label] * ESTIMATES[label][0]
            result["total_doc_minutes"] += slot_counts[label] * ESTIMATES[label][1]

    return result


def _empty_checkup_result():
    return {"time_slots": [], "counts": {l: [] for l in MODALITY_LABELS},
            "person_count": [], "total_counts": {l: 0 for l in MODALITY_LABELS},
            "total_persons": 0, "total_tech_minutes": 0, "total_doc_minutes": 0}


def _classify_ob(svc, cmt):
    """Classify OB service into OB/NT/Anatomy."""
    s = str(svc).strip() if pd.notna(svc) else ""
    c = str(cmt).strip() if pd.notna(cmt) else ""
    for prefix, label in SERVICE_EXACT_MAP.items():
        if prefix.lower() in s.lower():
            return label, False
    is_sec = any(prefix.lower() in s.lower() for prefix in SECONDARY_SERVICE_PREFIXES)
    if not is_sec:
        return ("OB", False) if ("B超" in c or "超声" in c) else ("OB", True)
    return ("OB", False) if ("B超" in c or "超声" in c) else ("OB", True)


def process_table_b(df, target_date):
    """Process OB ultrasound table."""
    cols = list(df.columns)
    time_col = _fuzzy_find(cols, ["time", "时间", "预约时间", "date", "日期", "appt", "时段"])

    if time_col and time_col in df.columns:
        t_num = pd.to_numeric(df[time_col], errors="coerce")
        df[time_col] = pd.to_datetime(t_num, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
        df = df[df[time_col].dt.date == target_date].copy()
        df["_ts"] = df[time_col].dt.strftime("%H:%M")
    else:
        df["_ts"] = "Unknown"

    df["_ts"] = df["_ts"].fillna("Unknown")
    if len(df) == 0:
        return _empty_ob_result()

    service_col = _fuzzy_find(cols, ["service", "服务", "项目", "套餐", "预约项目", "预约服务"])
    comment_col = _fuzzy_find(cols, ["comment", "备注", "说明", "note", "remark"])

    raw = []
    for _, row in df.iterrows():
        label, is_half = _classify_ob(row.get(service_col) if service_col else "",
                                       row.get(comment_col) if comment_col else "")
        raw.append((row["_ts"], label, is_half))

    time_slots = sorted(df["_ts"].unique(), key=_sort_key_time)
    result = {"time_slots": time_slots, "counts": {c: [] for c in OB_CLASSES},
              "total_counts": {c: 0 for c in OB_CLASSES}}

    for ts in time_slots:
        slot_counts = {"OB": 0.0, "NT": 0.0, "Anatomy": 0.0}
        for r_ts, r_label, r_half in raw:
            if r_ts == ts:
                slot_counts[r_label] += 0.5 if r_half else 1
        for c in OB_CLASSES:
            result["counts"][c].append(int(slot_counts[c]))

    for c in OB_CLASSES:
        result["total_counts"][c] = sum(result["counts"][c])
    return result


def _empty_ob_result():
    return {"time_slots": [], "counts": {c: [] for c in OB_CLASSES},
            "total_counts": {c: 0 for c in OB_CLASSES}}


# =====================================================
# MESSAGE FORMATTING
# =====================================================


def _format_summary(target_date, a, b):
    wday_cn = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    w = wday_cn[target_date.weekday()]
    lines = [f"Daily Demand Report - {target_date} ({w})", ""]

    lines.append(f"[Checkup]: {a.get('total_persons',0)} people")
    parts = []
    cn = {"CT": "CT", "X-ray": "X-ray", "B-ultrasound": "B-ultrasound",
          "Echo": "Echo", "Mammo": "Mammo", "BoneDensity": "BoneDensity", "MRI": "MRI"}
    for lbl in ["CT", "X-ray", "B-ultrasound", "Echo", "Mammo", "BoneDensity", "MRI"]:
        n = a.get("total_counts", {}).get(lbl, 0)
        if n: parts.append(f"{cn[lbl]}:{n}")
    if parts:
        lines.append(" | ".join(parts))
    tm = a.get("total_tech_minutes", 0)
    dm = a.get("total_doc_minutes", 0)
    lines.append(f"Est operation: {tm}min ({tm/60:.1f}h) | Est report: {dm}min ({dm/60:.1f}h)")

    lines.append("")
    ob_cn = {"OB": "OB", "NT": "NT", "Anatomy": "Anatomy"}
    ob_parts = []
    for c in OB_CLASSES:
        n = b.get("total_counts", {}).get(c, 0)
        if n: ob_parts.append(f"{ob_cn[c]}:{n}")
    if ob_parts:
        lines.append(f"[OB Ultrasound]: {', '.join(ob_parts)}")
    else:
        lines.append("[OB Ultrasound]: No reservations yet")
    return "\n".join(lines)


def _format_detail(target_date, a, b):
    wday_cn = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    w = wday_cn[target_date.weekday()]
    lines = [f"Detail Breakdown - {target_date} ({w})", ""]

    if a.get("time_slots"):
        lines.append("=== Checkup ===")
        for i, ts in enumerate(a["time_slots"]):
            p = a["person_count"][i] if i < len(a.get("person_count", [])) else 0
            tags = []
            cn = {"CT": "CT", "X-ray": "XR", "B-ultrasound": "B", "Echo": "Echo",
                  "Mammo": "Mam", "BoneDensity": "BD", "MRI": "MRI"}
            for lbl in ["CT", "X-ray", "B-ultrasound", "Echo", "Mammo", "BoneDensity", "MRI"]:
                n = a["counts"].get(lbl, [])[i] if i < len(a["counts"].get(lbl, [])) else 0
                if n: tags.append(f"{cn[lbl]}:{n}")
            lines.append(f"  {ts}  |  {p}p  |  {' '.join(tags)}")
    else:
        lines.append("[Checkup]: No data")

    lines.append("")
    if b.get("time_slots"):
        lines.append("=== OB Ultrasound ===")
        ob_cn = {"OB": "OB", "NT": "NT", "Anatomy": "Anatomy"}
        for i, ts in enumerate(b["time_slots"]):
            parts = []
            for c in OB_CLASSES:
                n = b["counts"].get(c, [])[i] if i < len(b["counts"].get(c, [])) else 0
                if n: parts.append(f"{ob_cn[c]}:{n}")
            if parts:
                lines.append(f"  {ts}  |  {' '.join(parts)}")
    else:
        lines.append("[OB Ultrasound]: No data")
    return "\n".join(lines)


# =====================================================
# SENDING
# =====================================================


def send_text_message(token, chat_id, text):
    url = f"{FEISHU_API}/im/v1/messages?receive_id_type=chat_id"
    body = {"receive_id": chat_id, "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False)}
    resp = _get_session().post(url, json=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }, timeout=30)
    data = resp.json()
    if data.get("code") == 0:
        print(f"  [OK] Sent. message_id={data.get('data',{}).get('message_id','?')}")
        return True
    print(f"  [FAIL] code={data.get('code')} msg={data.get('msg')}")
    return False


# =====================================================
# MAIN
# =====================================================


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Feishu Bot - Daily Reservation Reporter")
    parser.add_argument("--chat-id", type=str, default=None)
    parser.add_argument("--target", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.target) if args.target else (date.today() + timedelta(days=1))
    chat_id = args.chat_id or CHAT_ID

    print(f"Daily Reservation Report - {target_date}")

    token = get_tenant_access_token(APP_ID, APP_SECRET)

    # Table A
    field_map_a = get_field_meta(token, BASE_CHECKUP, TABLE_CHECKUP)
    records_a = get_all_records(token, BASE_CHECKUP, TABLE_CHECKUP)
    df_a = records_to_dataframe(records_a, field_map_a)
    result_a = process_table_a(df_a, target_date)

    # Table B
    try:
        field_map_b = get_field_meta(token, BASE_OB, TABLE_OB)
        records_b = get_all_records(token, BASE_OB, TABLE_OB)
        df_b = records_to_dataframe(records_b, field_map_b)
        result_b = process_table_b(df_b, target_date)
    except Exception as e:
        print(f"[WARN] OB failed: {e}")
        result_b = _empty_ob_result()

    summary = _format_summary(target_date, result_a, result_b)
    detail = _format_detail(target_date, result_a, result_b)

    print("--- SUMMARY ---")
    print(summary)
    print("--- DETAIL ---")
    print(detail)

    if args.dry_run:
        print("[DRY RUN] Not sent.")
        return

    if not chat_id:
        print("[WARN] No chat_id. Set FEISHU_CHAT_ID env var.")
        return

    print(f"Sending to {chat_id}...")
    ok1 = send_text_message(token, chat_id, summary)
    ok2 = send_text_message(token, chat_id, detail)
    print("[DONE]" if (ok1 and ok2) else "[WARN] Some messages failed")


if __name__ == "__main__":
    main()
