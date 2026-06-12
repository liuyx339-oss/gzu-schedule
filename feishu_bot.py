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


def _send_post_message(token, chat_id, title, content_blocks):
    """Send a rich-text post message to a Feishu chat.
    content_blocks is a list of paragraph lists. Each paragraph is a list of
    element dicts: {"tag":"text","text":"hello"} or {"tag":"text","text":"bold","style":["bold"]}
    """
    url = f"{FEISHU_API}/im/v1/messages?receive_id_type=chat_id"
    post_content = json.dumps({"zh_cn": {"title": title, "content": content_blocks}}, ensure_ascii=False)
    body = {"receive_id": chat_id, "msg_type": "post", "content": post_content}
    resp = _get_session().post(url, json=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }, timeout=30)
    data = resp.json()
    if data.get("code") == 0:
        msg_id = data.get("data", {}).get("message_id", "?")
        print(f"  [OK] Sent. message_id={msg_id}")
        return True
    print(f"  [FAIL] code={data.get('code')} msg={data.get('msg')}")
    return False


def _t(text, bold=False):
    """Shorthand for a text element."""
    el = {"tag": "text", "text": text}
    if bold:
        el["style"] = ["bold"]
    return el


def _build_rich_summary(target_date, a, b):
    """Build rich post content blocks for the summary message."""
    wday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    w = wday_cn[target_date.weekday()]
    # Chinese labels
    cn = {"CT": "CT", "X-ray": "X-ray", "B-ultrasound": "B超",
          "Echo": "心彩", "Mammo": "钼靶", "BoneDensity": "骨密度", "MRI": "MRI"}
    ob_cn = {"OB": "OB超声", "NT": "NT", "Anatomy": "大排畸"}

    blocks = []

    # Title line
    blocks.append([_t(f"明日需求日报 — {target_date} {w}", bold=True)])

    # Checkup section header
    total_p = a.get("total_persons", 0)
    blocks.append([_t("")])
    blocks.append([_t("🟦 体检人群 ", bold=True), _t(f"共 {total_p} 人", bold=True)])

    # Modality summary line
    parts = []
    for lbl in ["CT", "X-ray", "B-ultrasound", "Echo", "Mammo", "BoneDensity", "MRI"]:
        n = a.get("total_counts", {}).get(lbl, 0)
        if n:
            parts.append(f"{cn[lbl]} {n}")
    if parts:
        blocks.append([_t("    " + "  |  ".join(parts))])

    # Time estimation
    tm = a.get("total_tech_minutes", 0)
    dm = a.get("total_doc_minutes", 0)
    blocks.append([_t(f"    预估操作 {tm}min（{tm/60:.1f}h）| 预估报告 {dm}min（{dm/60:.1f}h）")])

    # OB section
    blocks.append([_t("")])
    ob_parts = []
    for c in OB_CLASSES:
        n = b.get("total_counts", {}).get(c, 0)
        if n:
            ob_parts.append(f"{ob_cn[c]} {n}")
    if ob_parts:
        blocks.append([_t("🟩 OB超声 ", bold=True), _t(f"{',  '.join(ob_parts)}")])
    else:
        blocks.append([_t("🟩 OB超声 ", bold=True), _t("暂无预约")])

    # Footer
    blocks.append([_t("")])
    blocks.append([_t("—— 数据来源：飞书 Bitable ｜ 自动播报")])

    return blocks


def _build_rich_detail(target_date, a, b):
    """Build rich post content blocks for the detail breakdown."""
    wday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    w = wday_cn[target_date.weekday()]
    blocks = []

    blocks.append([_t(f"时间段明细 — {target_date} {w}", bold=True)])

    # Checkup detail
    if a.get("time_slots"):
        blocks.append([_t("")])
        blocks.append([_t("🟦 体检人群", bold=True)])
        cn = {"CT": "CT", "X-ray": "XR", "B-ultrasound": "B超",
              "Echo": "心彩", "Mammo": "钼靶", "BoneDensity": "骨密度", "MRI": "MRI"}
        times = a["time_slots"]
        persons = a.get("person_count", [])
        for i, ts in enumerate(times):
            p = persons[i] if i < len(persons) else 0
            if p == 0:
                continue
            tags = []
            for lbl in ["CT", "X-ray", "B-ultrasound", "Echo", "Mammo", "BoneDensity", "MRI"]:
                n = a["counts"].get(lbl, [])[i] if i < len(a["counts"].get(lbl, [])) else 0
                if n:
                    tags.append(f"{cn[lbl]}{n}")
            line = f"  {ts}  {p}人  {' '.join(tags)}" if tags else f"  {ts}  {p}人"
            blocks.append([_t(line)])
    else:
        blocks.append([_t("  暂无体检预约")])

    # OB detail
    if b.get("time_slots"):
        blocks.append([_t("")])
        blocks.append([_t("🟩 OB超声", bold=True)])
        ob_cn = {"OB": "OB", "NT": "NT", "Anatomy": "大排畸"}
        times = b["time_slots"]
        for i, ts in enumerate(times):
            parts = []
            for c in OB_CLASSES:
                n = b["counts"].get(c, [])[i] if i < len(b["counts"].get(c, [])) else 0
                if n:
                    parts.append(f"{ob_cn[c]}{n}")
            if parts:
                blocks.append([_t(f"  {ts}  {' '.join(parts)}")])
    else:
        blocks.append([_t("  暂无OB超声预约")])

    blocks.append([_t("")])
    blocks.append([_t("—— 完整看板见固定链接")])
    return blocks


# =====================================================
# HTML DASHBOARD GENERATION
# =====================================================


def _generate_html_report(target_date, a, b):
    """Generate a self-contained HTML dashboard from reservation data."""
    wday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    w = wday_cn[target_date.weekday()]

    # --- Checkup section ---
    total_p = a.get("total_persons", 0)
    badges = ""
    cn = {"CT": "CT", "X-ray": "X-ray", "B-ultrasound": "B超", "Echo": "心彩",
          "Mammo": "钼靶", "BoneDensity": "骨密度", "MRI": "MRI"}
    colors_mod = {"CT": "#9C27B0", "X-ray": "#2196F3", "B-ultrasound": "#4CAF50",
                  "Echo": "#00BCD4", "Mammo": "#FF9800", "BoneDensity": "#795548", "MRI": "#E91E63"}
    for lbl in ["CT", "X-ray", "B-ultrasound", "Echo", "Mammo", "BoneDensity", "MRI"]:
        n = a.get("total_counts", {}).get(lbl, 0)
        if n:
            badges += f'<span style="background:{colors_mod.get(lbl,"#999")};color:#fff;padding:4px 10px;border-radius:4px;font-size:13px;font-weight:600;margin:2px">{cn[lbl]} {n}</span> '

    tech_m = a.get("total_tech_minutes", 0)
    doc_m = a.get("total_doc_minutes", 0)

    # Checkup table rows
    checkup_rows = ""
    times = a.get("time_slots", [])
    persons = a.get("person_count", [])
    max_p = max(persons) if persons else 1
    for i, ts in enumerate(times):
        p = persons[i] if i < len(persons) else 0
        pct = round(p / max_p * 100)
        tags = ""
        for lbl in ["CT", "X-ray", "B-ultrasound", "Echo", "Mammo", "BoneDensity", "MRI"]:
            n = a["counts"].get(lbl, [])[i] if i < len(a["counts"].get(lbl, [])) else 0
            if n:
                tags += f'<span style="background:{colors_mod.get(lbl,"#999")};color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:600;margin:1px">{cn[lbl]} {n}</span> '
        checkup_rows += f'<tr><td style="font-weight:600;white-space:nowrap">{ts}</td><td style="text-align:center;font-weight:700;font-size:16px;color:#1a73e8">{p}</td><td><div style="background:#eef2f7;border-radius:4px;height:16px;min-width:50px"><div style="background:linear-gradient(90deg,#1a73e8,#4a9af5);height:100%;border-radius:4px;width:{pct}%"></div></div></td><td>{tags}</td></tr>'

    # OB section
    ob_badges = ""
    ob_class_cn = {"OB": "OB超声", "NT": "NT", "Anatomy": "大排畸"}
    ob_colors = {"OB": "#4CAF50", "NT": "#FF9800", "Anatomy": "#E91E63"}
    for cls in OB_CLASSES:
        n = b.get("total_counts", {}).get(cls, 0)
        if n:
            ob_badges += f'<span style="background:{ob_colors.get(cls,"#999")};color:#fff;padding:4px 10px;border-radius:4px;font-size:13px;font-weight:600;margin:2px">{ob_class_cn[cls]} {n}</span> '

    ob_rows = ""
    ob_times = b.get("time_slots", [])
    for i, ts in enumerate(ob_times):
        parts = ""
        ob_nums = []
        for cls in OB_CLASSES:
            n = b["counts"].get(cls, [])[i] if i < len(b["counts"].get(cls, [])) else 0
            if n:
                parts += '<span style="background:{};color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:600;margin:1px">{} {}</span> '.format(
                    ob_colors.get(cls, "#999"), ob_class_cn[cls], n)
                ob_nums.append(str(n))
        if parts:
            ob_rows += '<tr><td style="font-weight:600;white-space:nowrap">{}</td><td style="text-align:center;font-weight:700;font-size:15px;color:#1a73e8">{}</td><td>{}</td></tr>'.format(
                ts, " ".join(ob_nums), parts)

    ob_has_data = bool(ob_times)

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>需求日报 - {target_date}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f7fa;color:#333}}
.header{{background:linear-gradient(135deg,#1a73e8 0%,#0d47a1 100%);color:#fff;padding:20px 24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}}
.header h1{{font-size:22px}}
.header .nav a{{color:#fff;opacity:0.8;text-decoration:none;font-size:14px;margin-left:14px}}
.header .nav a:hover{{opacity:1}}
.section{{margin:20px 24px}}
.section-title{{font-size:16px;font-weight:700;color:#1a73e8;padding:10px 14px;background:#e8f0fe;border-radius:8px;margin-bottom:12px;border-left:4px solid #1a73e8}}
.badges{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}}
.badge-white{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:8px 16px;display:inline-flex;align-items:center;gap:6px}}
.badge-white .lbl{{font-size:12px;color:#888}}
.badge-white .val{{font-size:22px;font-weight:700;color:#1a73e8}}
.badge-white.highlight{{border-color:#1a73e8;background:#e8f0fe}}
.time-badge{{background:#fff;border:1px solid #ddd;border-radius:6px;padding:5px 12px;font-size:12px}}
.time-badge .lbl{{color:#888}}
.time-badge .val{{font-weight:700;color:#e65100}}
.table-wrap{{overflow-x:auto;background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,0.06)}}
.rpt-table{{width:100%;border-collapse:collapse;font-size:13px}}
.rpt-table th{{background:#f0f5ff;color:#1a73e8;font-weight:600;padding:8px 10px;text-align:left;border-bottom:2px solid #c5d9f5;white-space:nowrap}}
.rpt-table td{{padding:8px 10px;border-bottom:1px solid #eef2f7;vertical-align:middle}}
.rpt-table tr:hover td{{background:#f8fafe}}
.footer{{text-align:center;padding:20px;font-size:12px;color:#999}}
.empty{{text-align:center;padding:40px;color:#999;font-size:14px}}
@media(max-width:768px){{.header h1{{font-size:18px}}.section{{margin:10px 12px}}}}
</style>
</head>
<body>

<div class="header">
    <h1>明日需求日报 — {target_date} ({w})</h1>
    <div class="nav">
        <a href="index.html">首页</a>
        <a href="schedule.html">排班表</a>
    </div>
</div>

<!-- REAL RESERVATIONS -->
<div class="section">
    <div class="section-title">真实预约</div>

    <!-- Checkup -->
    <div style="margin-bottom:20px">
        <h3 style="font-size:15px;margin-bottom:8px;color:#333">体检人群</h3>
        <div class="badges">
            <span class="badge-white highlight"><span class="lbl">总人数</span><span class="val">{total_p}</span></span>
            {badges}
        </div>
        <div class="badges" style="margin-bottom:12px">
            <span class="time-badge"><span class="lbl">预估操作</span> <span class="val">{tech_m}min ({tech_m/60:.1f}h)</span></span>
            <span class="time-badge"><span class="lbl">预估报告</span> <span class="val">{doc_m}min ({doc_m/60:.1f}h)</span></span>
        </div>
        <div class="table-wrap">
            <table class="rpt-table">
                <thead><tr><th>时间</th><th style="text-align:center">人数</th><th>负荷</th><th>检查项</th></tr></thead>
                <tbody>{checkup_rows}</tbody>
            </table>
        </div>
    </div>

    <!-- OB -->
    <div>
        <h3 style="font-size:15px;margin-bottom:8px;color:#333">OB超声</h3>
        <div class="badges">{ob_badges if ob_badges else '<span style="color:#999;font-size:14px">暂无预约数据</span>'}</div>
        {f'''<div class="table-wrap"><table class="rpt-table"><thead><tr><th>时间</th><th style="text-align:center">人数</th><th>检查项</th></tr></thead><tbody>{ob_rows}</tbody></table></div>''' if ob_has_data else '<div class="empty">明天暂无OB超声预约</div>'}
    </div>
</div>

<div class="footer">
    数据来源: 飞书 Bitable &nbsp;|&nbsp; 自动更新于 GitHub Actions &nbsp;|&nbsp; {target_date}
</div>

</body>
</html>'''
    return html


# =====================================================
# DAILY DATA JSON
# =====================================================


def _build_daily_data(target_date, a, b):
    """Build a JSON-serializable dict for the dynamic dashboard."""
    wday_cn = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Checkup section
    checkup = {
        "total_persons": a.get("total_persons", 0),
        "total_tech_minutes": a.get("total_tech_minutes", 0),
        "total_doc_minutes": a.get("total_doc_minutes", 0),
        "time_slots": a.get("time_slots", []),
        "person_count": a.get("person_count", []),
        "counts": a.get("counts", {}),
        "total_counts": a.get("total_counts", {}),
        "service_breakdown": a.get("service_breakdown", {}),
    }

    # OB section
    ob = {
        "time_slots": b.get("time_slots", []),
        "counts": b.get("counts", {}),
        "total_counts": b.get("total_counts", {}),
    }

    return {
        "target_date": str(target_date),
        "weekday": wday_cn[target_date.weekday()],
        "checkup": checkup,
        "ob": ob,
    }


# =====================================================
# MAIN
# =====================================================


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Feishu Bot - Daily Reservation Reporter")
    parser.add_argument("--chat-id", type=str, default=None)
    parser.add_argument("--target", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-html", type=str, default=None,
                        help="Write dashboard HTML to this path (e.g. publish/dashboard.html)")
    parser.add_argument("--output-data", type=str, default=None,
                        help="Write daily JSON data to this path (e.g. publish/daily_data.json)")
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

    # Build rich post message content
    rich_title = f"明日需求日报 — {target_date} {['周一','周二','周三','周四','周五','周六','周日'][target_date.weekday()]}"
    rich_blocks = _build_rich_summary(target_date, result_a, result_b)
    detail_title = f"时间段明细 — {target_date}"
    detail_blocks = _build_rich_detail(target_date, result_a, result_b)

    print(f"--- SUMMARY: {rich_title} ---")
    print(f"--- DETAIL: {detail_title} ---")

    if args.output_html:
        html = _generate_html_report(target_date, result_a, result_b)
        os.makedirs(os.path.dirname(args.output_html) or ".", exist_ok=True)
        with open(args.output_html, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[OK] Dashboard HTML written to {args.output_html}")

    if args.output_data:
        data = _build_daily_data(target_date, result_a, result_b)
        os.makedirs(os.path.dirname(args.output_data) or ".", exist_ok=True)
        with open(args.output_data, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[OK] Daily data JSON written to {args.output_data}")

    if args.dry_run:
        print("[DRY RUN] Not sent.")
        return

    if not chat_id:
        print("[WARN] No chat_id. Set FEISHU_CHAT_ID env var.")
        return

    print(f"Sending to {chat_id}...")
    ok1 = _send_post_message(token, chat_id, rich_title, rich_blocks)
    ok2 = _send_post_message(token, chat_id, detail_title, detail_blocks)
    print("[DONE]" if (ok1 and ok2) else "[WARN] Some messages failed")


if __name__ == "__main__":
    main()
