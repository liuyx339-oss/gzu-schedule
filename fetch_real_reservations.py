"""
Real Reservations Data Fetcher
需求日报 - 真实预约数据拉取

从两个飞书多维表格拉取明日的真实预约数据：
  Table A (Checkup): Base=NM6HbB8g..., Table=tblrUOmKxEmHxCxa
  Table B (OB):      Base=XDa9w6qGBigq..., Table=tbltb1eix0QOEcQP

Output:
  forecast_output/Real_Reservations_Checkup.csv
  forecast_output/Real_Reservations_OB.csv
"""

import os
import sys
import json
import re
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from fetch_feishu_data import (
    get_tenant_access_token,
    get_field_meta,
    get_all_records,
    records_to_dataframe,
)

warnings.filterwarnings("ignore")

# =====================================================
# CONFIG
# =====================================================

APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aaa8d24639b8dcd8")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "b0ayVQKIuUGmvzRu9YCm9gpZHUzniNz1")

# Table A - Checkup population
BASE_CHECKUP = "NM6HbB8gKaqtDysTTrRcve0ZnAc"
TABLE_CHECKUP = "tblrUOmKxEmHxCxa"

# Table B - OB ultrasound
BASE_OB = "XDa9w6qGBigqGNkOENvctCJtnqd"
TABLE_OB = "tbltb1eix0QOEcQP"

# Standard minutes: (operation_minutes, report_minutes)
ESTIMATES = {
    "MRI": (30, 20),
    "CT": (15, 10),
    "X-ray": (10, 5),
    "Mammo": (10, 5),
    "BoneDensity": (10, 5),
    "B-ultrasound": (20, 10),
    "Echo": (20, 10),
}

MODALITY_LABELS = list(ESTIMATES.keys())

# Chinese display names for modalities
MODALITY_CN = {
    "MRI": "MRI",
    "CT": "CT",
    "X-ray": "X-ray",
    "Mammo": "Mammo",
    "BoneDensity": "BoneDensity",
    "B-ultrasound": "B-ultrasound",
    "Echo": "Echo",
}

OUTPUT_DIR = os.path.join(SCRIPT_DIR, "forecast_output")

# =====================================================
# HELPERS
# =====================================================


def _fuzzy_find(cols, keywords):
    """Find a column name by fuzzy-matching against keywords. Returns None if not found."""
    for kw in keywords:
        for col in cols:
            if kw.lower() in str(col).lower():
                return col
    return None


def _parse_time_slot(raw_val):
    """Normalize a time value to HH:MM string, or None if unparseable."""
    if raw_val is None or (isinstance(raw_val, float) and np.isnan(raw_val)):
        return None
    s = str(raw_val).strip()
    m = re.match(r'(\d{1,2}:\d{2})', s)
    if m:
        return m.group(1)
    try:
        h = int(float(s))
        return f"{h:02d}:00"
    except (ValueError, TypeError):
        return None


def _discover_modality_columns(cols):
    """Discover modality columns (MRI, CT, X-ray, etc.) in the DataFrame.
    Returns {internal_label: actual_col_name} mapping."""
    # Chinese labels that might appear in Feishu
    cn_labels = {
        "MRI": ["MRI", "磁共振", "核磁"],
        "CT": ["CT", "CT扫描"],
        "X-ray": ["X-ray", "X线", "X光", "DR", "X - ray", "X - rays"],
        "Mammo": ["Mammo", "Mammogram", "钼靶"],
        "BoneDensity": ["BoneDensity", "骨密度", "DXA"],
        "B-ultrasound": ["B-ultrasound", "B超", "B-ultrasound", "Ultrasound", "超声"],
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
    """Sort key for time slot strings like '08:00'."""
    m = re.match(r'(\d{1,2}):(\d{2})', str(ts))
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return 9999


# =====================================================
# TABLE A - Checkup Population
# =====================================================


def process_table_a(df, target_date):
    """Process the Checkup table.

    1. Find appt_dt column -> filter to target_date
    2. Find time column -> group by time slot
    3. Find modality columns -> count per slot
    4. Find service_desc column -> detail breakdown
    5. "未确认套餐" handling -> +1 CT +1 B-ultrasound
    6. Time estimation
    """
    UNCONFIRMED_KEYWORD = "未确认套餐"

    print(f"\n{'='*60}")
    print(f"[Table A] Checkup population")
    print(f"   Target date: {target_date}")
    print(f"   Total records: {len(df)}")

    cols = list(df.columns)
    print(f"   Columns ({len(cols)}): {cols}")

    # 1. Find date column
    appt_col = _fuzzy_find(cols, ["appt_dt", "appt", "预约日期", "预约时间", "date", "日期"])
    print(f"   appt_dt column: {appt_col}")

    if appt_col and appt_col in df.columns:
        # Feishu DateTime fields may come as raw ms timestamp strings
        ts_num = pd.to_numeric(df[appt_col], errors="coerce")
        df[appt_col] = pd.to_datetime(ts_num, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
        df = df[df[appt_col].dt.date == target_date].copy()
        print(f"   After date filter: {len(df)} records")
    else:
        print("   [WARN] Date column not found, using all records")

    if len(df) == 0:
        print("   [WARN] No data for target date")
        return _empty_checkup_result()

    # 2. Find time column
    time_col = _fuzzy_find(cols, ["time_slot", "时段", "时间", "time", "预约时段"])
    print(f"   Time column: {time_col}")

    if time_col and time_col in df.columns:
        # Feishu DateTime - may be raw ms string
        ts_num = pd.to_numeric(df[time_col], errors="coerce")
        df["_time_slot"] = pd.to_datetime(ts_num, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%H:%M")
    elif appt_col and appt_col in df.columns:
        df["_time_slot"] = df[appt_col].dt.strftime("%H:%M")
    else:
        df["_time_slot"] = "Unknown"

    df["_time_slot"] = df["_time_slot"].fillna("Unknown")

    # 3. Discover modality columns
    modality_cols = _discover_modality_columns(cols)
    print(f"   Modality columns: {modality_cols}")

    # 4. Find service_desc column
    service_col = _fuzzy_find(cols, ["service_desc", "服务描述", "检查项目", "项目名称", "医嘱描述", "order_item", "description", "内容"])
    print(f"   service_desc column: {service_col}")

    # 4b. Find unconfirmed column (dedicated Y/N column)
    unconfirmed_col = _fuzzy_find(cols, ["套餐", "未确认", "未定"])
    print(f"   unconfirmed column: {unconfirmed_col}")

    # 5. Group by time slot
    time_slots = sorted(df["_time_slot"].unique(), key=_sort_key_time)

    result = {
        "time_slots": time_slots,
        "counts": {label: [] for label in MODALITY_LABELS},
        "person_count": [],           # total people per time slot
        "service_breakdown": {},
        "total_counts": {label: 0 for label in MODALITY_LABELS},
        "total_persons": 0,
        "total_tech_minutes": 0,
        "total_doc_minutes": 0,
    }

    for ts in time_slots:
        slot_df = df[df["_time_slot"] == ts]
        n_persons = len(slot_df)
        result["person_count"].append(n_persons)
        result["total_persons"] += n_persons

        slot_counts = {label: 0 for label in MODALITY_LABELS}

        # Count modalities PER PERSON (B超 = 1 if person has any B超)
        for _, row in slot_df.iterrows():
            for label, mcol in modality_cols.items():
                if mcol in slot_df.columns:
                    val = row.get(mcol, 0)
                    try:
                        n = int(float(val)) if pd.notna(val) else 0
                    except (ValueError, TypeError):
                        n = 0
                    if label == "B-ultrasound":
                        # B超: binary per person (many scans done together = 1)
                        slot_counts[label] += 1 if n > 0 else 0
                    else:
                        slot_counts[label] += n

        # 未确认套餐: +1 CT +1 B超 (counts as 1 person for B超)
        for _, row in slot_df.iterrows():
            is_unconfirmed = False
            if unconfirmed_col and unconfirmed_col in slot_df.columns:
                val = str(row.get(unconfirmed_col, "")).strip().upper()
                if val == 'Y' or val == 'YES' or val == 'TRUE' or '未确认' in val:
                    is_unconfirmed = True
            if not is_unconfirmed and service_col and service_col in slot_df.columns:
                svc = str(row.get(service_col, ""))
                if UNCONFIRMED_KEYWORD in svc:
                    is_unconfirmed = True
            if is_unconfirmed:
                slot_counts["CT"] += 1
                slot_counts["B-ultrasound"] += 1

        for label in MODALITY_LABELS:
            result["counts"][label].append(slot_counts[label])
            result["total_counts"][label] += slot_counts[label]

        # Service detail
        if service_col and service_col in slot_df.columns:
            svc_counts = slot_df[service_col].value_counts().to_dict()
            svc_counts = {str(k): int(v) for k, v in svc_counts.items()}
            result["service_breakdown"][ts] = svc_counts

        # Time estimation (B超 = 1 scan per person, not sum of values)
        for label in MODALITY_LABELS:
            n = slot_counts[label]
            tech_m, doc_m = ESTIMATES[label]
            if label == "B-ultrasound":
                # 1 scan per person, not per value in cell
                pass  # n already = person count (binary)
            result["total_tech_minutes"] += n * tech_m
            result["total_doc_minutes"] += n * doc_m

    print(f"\n   Time slots: {len(time_slots)}")
    print(f"   Totals: {result['total_counts']}")
    print(f"   Est. operation: {result['total_tech_minutes']}min, Est. report: {result['total_doc_minutes']}min")
    return result


def _empty_checkup_result():
    return {
        "time_slots": [],
        "counts": {label: [] for label in MODALITY_LABELS},
        "service_breakdown": {},
        "total_counts": {label: 0 for label in MODALITY_LABELS},
        "total_tech_minutes": 0,
        "total_doc_minutes": 0,
    }


# =====================================================
# TABLE B - OB Ultrasound
# =====================================================

# Level 1: exact service match -> classification
SERVICE_EXACT_MAP = {
    "NT ultrasound prenatal screening prescription service": "NT",
    "Prenatal Care Package (Week 6-10)": "OB",
    "Prenatal Care Package (Week 10-13)": "NT",
    "Prenatal Care Package (Week 20)": "Anatomy",
    "Prenatal Care Package (Week 32)": "OB",
    "Prenatal Care Package (Week 38)": "OB",
}

# Level 2 services - classified by comment field
SECONDARY_SERVICE_PREFIXES = [
    "Prenatal Care Package (Week 16)",
    "Prenatal Care Package (Week 24)",
    "Prenatal Care Package (Week 28)",
    "Prenatal Care Package (Week 30)",
    "Prenatal Care Package (Week 34)",
    "Prenatal Care Package (Week 36)",
    "Prenatal Care Package (Week 37)",
    "Prenatal Care Package (Week 39)",
    "Prenatal Care Package (Week 40)",
    "New to GYN Clinic",
    "New to OB Clinic",
    "Breastfeeding Consultation",
    "Established GYN Visit",
    "Established Prenatal Patient Visit to OB Clinic",
    "Prenatal Genetic Counseling Clinic",
    "Simple consultation booking",
    "Video follow-up for Internet hospital",
    "12w prenatal screening prescription service",
    "20w prenatal screening prescription service",
    "Follicle Monitor",
    "GYN Pre-op treatment/assessment",
    "High-Risk Pregnancy Consultation Clinic",
    "Infertility Initial Consult",
    "Infertility Treatment Plan Review",
]

OB_CLASSES = ["OB", "NT", "Anatomy"]

# Chinese display names
OB_CLASS_CN = {
    "OB": "OB",
    "NT": "NT",
    "Anatomy": "Anatomy",
}


def _classify_ob_service(service_val, comment_val):
    """Classify a service+comment into OB / NT / Anatomy.
    Returns (label, is_half_count).
    """
    svc = str(service_val).strip() if pd.notna(service_val) else ""
    cmt = str(comment_val).strip() if pd.notna(comment_val) else ""

    # Level 1: try prefix match on service
    for prefix, label in SERVICE_EXACT_MAP.items():
        if prefix.lower() in svc.lower():
            return (label, False)

    # Level 2: check if service starts with any secondary prefix
    is_secondary = False
    for prefix in SECONDARY_SERVICE_PREFIXES:
        if prefix.lower() in svc.lower():
            is_secondary = True
            break

    if not is_secondary:
        # Not in secondary list either - treat as OB directly?
        # Fallback: check comment
        if "B超" in cmt or "超声" in cmt:
            return ("OB", False)
        return ("OB", True)  # half count

    # In secondary list: check comment
    if "B超" in cmt or "超声" in cmt:
        return ("OB", False)

    # Otherwise: count / 2 -> OB (half count)
    return ("OB", True)


def process_table_b(df, target_date):
    """Process the OB ultrasound table.

    1. Find time column -> filter to target_date
    2. Find service and comment columns
    3. Classify: Level 1 (exact service) -> Level 2 (comment filter)
    4. Group by time slot: OB count, NT count, Anatomy count
    """
    print(f"\n{'='*60}")
    print(f"[Table B] OB Ultrasound")
    print(f"   Target date: {target_date}")
    print(f"   Total records: {len(df)}")

    cols = list(df.columns)
    print(f"   Columns ({len(cols)}): {cols}")

    # 1. Find time/date column
    time_col = _fuzzy_find(cols, ["time", "时间", "预约时间", "date", "日期", "appt", "时段"])
    print(f"   time column: {time_col}")

    if time_col and time_col in df.columns:
        # Feishu DateTime fields may come as raw ms timestamp strings
        ts_num = pd.to_numeric(df[time_col], errors="coerce")
        df[time_col] = pd.to_datetime(ts_num, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
        df = df[df[time_col].dt.date == target_date].copy()
        df["_time_slot"] = df[time_col].dt.strftime("%H:%M")
        print(f"   After date filter: {len(df)} records")
    else:
        print("   [WARN] Date column not found, using all records")
        df["_time_slot"] = "Unknown"

    df["_time_slot"] = df["_time_slot"].fillna("Unknown")

    if len(df) == 0:
        print("   [WARN] No data for target date")
        return _empty_ob_result()

    # 2. Find service and comment columns
    service_col = _fuzzy_find(cols, ["service", "服务", "项目", "套餐", "预约项目", "预约类型", "预约服务"])
    comment_col = _fuzzy_find(cols, ["comment", "备注", "说明", "note", "remark"])
    print(f"   service column: {service_col}")
    print(f"   comment column: {comment_col}")

    # 3. Classify each record
    raw = []  # (time_slot, label, is_half)
    level1_count = 0
    level2_comment_count = 0
    level2_half_count = 0

    for _, row in df.iterrows():
        ts = row["_time_slot"]
        svc = row.get(service_col) if service_col else ""
        cmt = row.get(comment_col) if comment_col else ""
        label, is_half = _classify_ob_service(svc, cmt)

        raw.append((ts, label, is_half))
        if not is_half:
            level1_count += 1
        else:
            level2_half_count += 1

    # Also count those matched via comment in level 2
    level2_comment_count = level1_count  # approximation - actually level1 matches

    print(f"   Level 1 exact matches: {level1_count}")
    print(f"   Level 2 half-count: {level2_half_count} -> OB += {level2_half_count // 2}")

    # 4. Group by time slot
    time_slots = sorted(df["_time_slot"].unique(), key=_sort_key_time)

    result = {
        "time_slots": time_slots,
        "counts": {cls: [] for cls in OB_CLASSES},
        "service_breakdown": {},
        "total_counts": {cls: 0 for cls in OB_CLASSES},
    }

    for ts in time_slots:
        slot_counts = {"OB": 0.0, "NT": 0.0, "Anatomy": 0.0}
        for r_ts, r_label, r_half in raw:
            if r_ts == ts:
                if r_half:
                    slot_counts[r_label] += 0.5
                else:
                    slot_counts[r_label] += 1

        for cls in OB_CLASSES:
            result["counts"][cls].append(int(slot_counts[cls]))

        # Service detail
        slot_svc = df[df["_time_slot"] == ts]
        if service_col and service_col in slot_svc.columns:
            svc_counts = slot_svc[service_col].value_counts().to_dict()
            svc_counts = {str(k): int(v) for k, v in svc_counts.items()}
            result["service_breakdown"][ts] = svc_counts

    for cls in OB_CLASSES:
        result["total_counts"][cls] = sum(result["counts"][cls])

    print(f"   Time slots: {len(time_slots)}")
    print(f"   Totals: {result['total_counts']}")
    return result


def _empty_ob_result():
    return {
        "time_slots": [],
        "counts": {cls: [] for cls in OB_CLASSES},
        "service_breakdown": {},
        "total_counts": {cls: 0 for cls in OB_CLASSES},
    }


# =====================================================
# CSV OUTPUT
# =====================================================


def _save_checkup_csv(result, output_dir):
    """Save checkup CSV."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "Real_Reservations_Checkup.csv")

    rows = []
    for i, ts in enumerate(result["time_slots"]):
        row = {"time_slot": ts, "persons": result["person_count"][i] if i < len(result["person_count"]) else 0}
        for label in MODALITY_LABELS:
            row[label] = result["counts"][label][i] if i < len(result["counts"][label]) else 0
        row["service_detail"] = json.dumps(
            result["service_breakdown"].get(ts, {}), ensure_ascii=False
        )
        rows.append(row)

    df = pd.DataFrame(rows)
    summary = {"time_slot": "TOTAL", "persons": result["total_persons"]}
    for label in MODALITY_LABELS:
        summary[label] = result["total_counts"][label]
    summary["service_detail"] = ""
    df = pd.concat([df, pd.DataFrame([summary])], ignore_index=True)

    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[OK] Checkup CSV -> {path}")
    return path


def _save_ob_csv(result, output_dir):
    """Save OB ultrasound CSV."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "Real_Reservations_OB.csv")

    rows = []
    for i, ts in enumerate(result["time_slots"]):
        row = {"time_slot": ts}
        for cls in OB_CLASSES:
            row[cls] = result["counts"][cls][i] if i < len(result["counts"][cls]) else 0
        row["service_detail"] = json.dumps(
            result["service_breakdown"].get(ts, {}), ensure_ascii=False
        )
        rows.append(row)

    df = pd.DataFrame(rows)
    summary = {"time_slot": "TOTAL"}
    for cls in OB_CLASSES:
        summary[cls] = result["total_counts"][cls]
    summary["service_detail"] = ""
    df = pd.concat([df, pd.DataFrame([summary])], ignore_index=True)

    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[OK] OB ultrasound CSV -> {path}")
    return path


# =====================================================
# REPORT
# =====================================================


def _print_report(result_a, result_b, target_date):
    """Print console summary report."""
    wday_cn = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print(f"\n{'='*60}")
    print(f"Daily Demand Report - Real Reservations")
    print(f"   Date: {target_date} ({wday_cn[target_date.weekday()]})")
    print(f"{'='*60}")

    print(f"\n[Checkup]")
    if result_a["time_slots"]:
        for label in MODALITY_LABELS:
            n = result_a["total_counts"].get(label, 0)
            if n:
                print(f"   {label}: {n}")
        print(f"   Est. operation: {result_a['total_tech_minutes']} min ({result_a['total_tech_minutes']/60:.1f}h)")
        print(f"   Est. report: {result_a['total_doc_minutes']} min ({result_a['total_doc_minutes']/60:.1f}h)")
    else:
        print("   (no data)")

    print(f"\n[OB Ultrasound]")
    if result_b["time_slots"]:
        for cls in OB_CLASSES:
            n = result_b["total_counts"].get(cls, 0)
            if n:
                print(f"   {cls}: {n}")
    else:
        print("   (no data)")

    print(f"\n{'='*60}")


# =====================================================
# MAIN
# =====================================================


def main(target_date=None):
    """Main entry point.
    target_date: datetime.date, defaults to tomorrow.
    """
    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    print("=" * 60)
    print("Real Reservations Data Fetcher")
    print(f"   Target: {target_date} ({target_date.strftime('%A')})")
    print("=" * 60)

    # Get token (shared credentials for both tables)
    print("\n[1/4] Getting Feishu access token...")
    token = get_tenant_access_token(APP_ID, APP_SECRET)

    # ---- Table A: Checkup ----
    print(f"\n[2/4] Fetching Table A - Checkup (Base={BASE_CHECKUP[:20]}...)")
    try:
        field_map_a = get_field_meta(token, BASE_CHECKUP, TABLE_CHECKUP)
        records_a = get_all_records(token, BASE_CHECKUP, TABLE_CHECKUP)
        df_a = records_to_dataframe(records_a, field_map_a)
        result_a = process_table_a(df_a, target_date)
    except Exception as e:
        print(f"\n[WARN] Table A fetch failed: {e}")
        print("   (Please verify the App has collaborator access to this Base)")
        result_a = _empty_checkup_result()

    # ---- Table B: OB Ultrasound ----
    print(f"\n[3/4] Fetching Table B - OB Ultrasound (Base={BASE_OB[:20]}...)")
    try:
        field_map_b = get_field_meta(token, BASE_OB, TABLE_OB)
        records_b = get_all_records(token, BASE_OB, TABLE_OB)
        df_b = records_to_dataframe(records_b, field_map_b)
        result_b = process_table_b(df_b, target_date)
    except Exception as e:
        print(f"\n[WARN] Table B fetch failed: {e}")
        print("   (Please verify the App has collaborator access to this Base)")
        result_b = _empty_ob_result()

    # ---- Output CSVs ----
    print(f"\n[4/4] Saving output files...")
    _save_checkup_csv(result_a, OUTPUT_DIR)
    _save_ob_csv(result_b, OUTPUT_DIR)

    # ---- Console report ----
    _print_report(result_a, result_b, target_date)

    return result_a, result_b


if __name__ == "__main__":
    main()
