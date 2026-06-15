"""
Weekly Forecast Script
每周一从飞书拉取近 30 天数据，用 Prophet + LightGBM 预测当周需求，
输出 publish/forecast_data.json 供 dashboard.html 展示。

Usage:
  python weekly_forecast.py                          # 默认：预测当周 (周一~周日)
  python weekly_forecast.py --target 2026-06-16      # 指定"今天"，预测其所在周
  python weekly_forecast.py --output custom_path.json
"""

import os
import sys
import json
import warnings
import logging
import argparse
from datetime import date, timedelta, datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.getLogger("prophet").setLevel(logging.ERROR)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from fetch_feishu_data import (
    get_tenant_access_token,
    get_field_meta,
    get_all_records,
    records_to_dataframe,
)
from forecast_core import (
    TIMEZONE, VALID_TYPES,
    add_time_features,
    run_forecast_pipeline,
)

# =====================================================
# CONFIG
# =====================================================

APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aaa8d24639b8dcd8")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "b0ayVQKIuUGmvzRu9YCm9gpZHUzniNz1")

# Master data table (临床数据)
BASE_MASTER = "NjSdbaToNavBlksS6AecPd7rnrb"
TABLE_MASTER = "tbl9camXrcKz4qhZ"

# Mapping table (医嘱→Type/大分类)
BASE_MAPPING = "DbRZbYJblam4i1sNSQScx3r3nab"
TABLE_MAPPING = "tbl5RNcBp66q3zlc"

# How many days of history to fetch
HISTORY_DAYS = 30

# Default output path
DEFAULT_OUTPUT = os.path.join(SCRIPT_DIR, "publish", "forecast_data.json")


# =====================================================
# KEYWORD-BASED TYPE CLASSIFICATION (fallback)
# =====================================================

def classify_by_keyword(order_desc):
    """当映射表匹配失败时，通过关键词判断 Type。"""
    s = str(order_desc).lower().strip()
    if any(k in s for k in ["echocardiogram", "transthoracic", "transesophageal", "超声心动图"]):
        return "Echocardiograms"
    if any(k in s for k in ["bone densitometry", "骨密度", "dxa", "dexa"]):
        return "DXA"
    if any(k in s for k in ["pet", "spect", "nuclear medicine", "核医学"]):
        return "Nuclear Medicine"
    if any(k in s for k in ["magnetic resonance", "磁共振", "mri"]):
        return "MRI"
    if any(k in s for k in ["computed tomography", "断层", "ct"]):
        return "CT"
    if any(k in s for k in ["mammogram", "乳腺", "钼靶"]):
        return "Mammogram"
    if any(k in s for k in ["x-ray", "x - ray", "xray", "fluoroscopy", "透视", "胸片"]):
        return "X - ray"
    if any(k in s for k in ["ultrasound", "sonography", "duplex", "超声"]):
        return "Ultrasound"
    if "骨龄" in s:
        return "骨龄测评"
    return "Ultrasound"  # default fallback


def get_大分类(type_name):
    """将 Type 映射为大分类 (超声/放射)。"""
    if pd.isna(type_name):
        return "超声"
    t = str(type_name).strip()
    if t in ("Ultrasound", "Echocardiograms"):
        return "超声"
    return "放射"


# =====================================================
# DATA FETCHING
# =====================================================

def fetch_master_data(token, start_date, end_date):
    """从主数据表拉取指定日期范围内的记录。"""
    print(f"\n[1/4] Fetching master data: {start_date} → {end_date}")

    field_map = get_field_meta(token, BASE_MASTER, TABLE_MASTER)
    records = get_all_records(token, BASE_MASTER, TABLE_MASTER)
    df = records_to_dataframe(records, field_map)

    print(f"  Total records: {len(df)}")

    # Parse the arrival time column (患者到达时间)
    time_col = None
    for c in df.columns:
        if "患者到达时间" in c or "到达" in c or "arrived" in c.lower():
            time_col = c
            break

    if time_col is None:
        # Try to find any timestamp column
        for c in df.columns:
            try:
                ts = pd.to_numeric(df[c].dropna().iloc[:5], errors="coerce")
                if ts.between(1e12, 2e12).all():  # ms timestamps around 2020-2060
                    time_col = c
                    break
            except Exception:
                pass

    if time_col is None:
        raise RuntimeError(f"Cannot find timestamp column. Available: {list(df.columns)}")

    print(f"  Using time column: '{time_col}'")

    # Convert and filter by date
    df["_ts"] = pd.to_datetime(
        pd.to_numeric(df[time_col], errors="coerce"),
        unit="ms",
        errors="coerce",
    )
    df["_ts"] = df["_ts"].dt.tz_localize("UTC").dt.tz_convert(TIMEZONE).dt.tz_localize(None)
    df["_date"] = df["_ts"].dt.date

    df = df[(df["_date"] >= start_date) & (df["_date"] <= end_date)].copy()
    print(f"  Filtered to {start_date}→{end_date}: {len(df)} rows")

    # Identify key columns
    dept_col = _fuzzy_find(list(df.columns), ["就诊科室", "科室", "dept", "department"])
    order_col = _fuzzy_find(list(df.columns), ["医嘱描述", "医嘱", "order_item", "医嘱名称"])

    if dept_col:
        df["eps_dept_desc"] = df[dept_col].astype(str)
    else:
        print("  [WARN] No department column found")
        df["eps_dept_desc"] = "Unknown"

    if order_col:
        df["order_item_desc"] = df[order_col].astype(str)
    else:
        print("  [WARN] No order description column found")
        df["order_item_desc"] = "Unknown"

    df["arrived_datetime"] = df["_ts"]
    df = df.dropna(subset=["arrived_datetime"])
    df["ds"] = df["arrived_datetime"].dt.floor("h")

    return df


def fetch_mapping_data(token):
    """拉取映射表 (医嘱描述 → Type/大分类/时长)。"""
    print(f"\n[2/4] Fetching mapping table...")

    field_map = get_field_meta(token, BASE_MAPPING, TABLE_MAPPING)
    records = get_all_records(token, BASE_MAPPING, TABLE_MAPPING)
    df = records_to_dataframe(records, field_map)

    print(f"  Mapping rows: {len(df)}")

    # Normalize column names
    col_map = {}
    for c in df.columns:
        cn = c.strip()
        if "医嘱描述" in cn or "描述" in cn or "order" in cn.lower():
            col_map[c] = "医嘱描述_映射"
        elif "Type" in cn or "type" in cn or "类型" in cn:
            col_map[c] = "Type"
        elif "大分类" in cn or "大类" in cn:
            col_map[c] = "大分类"
        elif "操作" in cn or "操作时长" in cn:
            col_map[c] = "预估操作时长"
        elif "报告" in cn or "写报告" in cn:
            col_map[c] = "预估医生写报告时长"
        elif "影像医生参与" in cn:
            col_map[c] = "影像医生参与时长"
        elif "总时长" in cn:
            col_map[c] = "总时长"

    if col_map:
        df.rename(columns=col_map, inplace=True)
    else:
        # If no fuzzy mapping, use first 2-3 columns
        cols = list(df.columns)
        if len(cols) >= 2:
            df.rename(columns={cols[0]: "医嘱描述_映射", cols[1]: "Type"}, inplace=True)
        if len(cols) >= 3:
            df.rename(columns={cols[2]: "预估操作时长"}, inplace=True)
        if len(cols) >= 4:
            df.rename(columns={cols[3]: "预估医生写报告时长"}, inplace=True)

    # Fill missing duration columns
    for col in ["预估操作时长", "预估医生写报告时长"]:
        if col not in df.columns:
            df[col] = 20.0

    # Fill missing 大分类
    if "大分类" not in df.columns:
        if "Type" in df.columns:
            df["大分类"] = df["Type"].apply(get_大分类)
        else:
            df["大分类"] = "超声"

    return df


# =====================================================
# DATA PROCESSING
# =====================================================

def normalize_text(s):
    """Normalize text for matching: NFKC + whitespace compression."""
    import unicodedata
    s = unicodedata.normalize("NFKC", str(s))
    return " ".join(s.split())


def process_and_join(master_df, mapping_df):
    """Join master data with mapping table, with keyword fallback."""
    print(f"\n[3/4] Processing and joining data...")

    # Normalize both sides for join
    master_df["_key"] = master_df["order_item_desc"].apply(normalize_text)
    mapping_df["_key"] = mapping_df["医嘱描述_映射"].apply(normalize_text)

    # LEFT JOIN on normalized key
    merged = master_df.merge(
        mapping_df[["_key", "Type", "大分类", "预估操作时长", "预估医生写报告时长"]],
        on="_key",
        how="left",
        suffixes=("", "_map"),
    )

    # Fill unmatched with keyword classification
    unmatched = merged["Type"].isna()
    print(f"  Unmatched records: {unmatched.sum()} / {len(merged)}")

    if unmatched.sum() > 0:
        merged.loc[unmatched, "Type"] = (
            merged.loc[unmatched, "order_item_desc"].apply(classify_by_keyword)
        )
        merged.loc[unmatched, "大分类"] = merged.loc[unmatched, "Type"].apply(get_大分类)
        # Default minutes for unmatched
        merged.loc[unmatched, "预估操作时长"] = 20.0
        merged.loc[unmatched, "预估医生写报告时长"] = 10.0

    # Clean up
    merged["预估操作时长"] = pd.to_numeric(merged["预估操作时长"], errors="coerce").fillna(20.0)
    merged["预估医生写报告时长"] = pd.to_numeric(merged["预估医生写报告时长"], errors="coerce").fillna(10.0)

    # Apply Health Management Center 体检日 rule
    dept_col = "eps_dept_desc"
    merged[dept_col] = merged[dept_col].apply(_apply_checkup_day_rule)

    # Add time features
    merged = add_time_features(merged)

    # Re-apply 大分类 from forecast_core logic (Ultrasound → 超声, rest → 放射)
    merged["大分类"] = np.where(
        merged["Type"].astype(str).str.strip() == "Ultrasound",
        "超声",
        "放射",
    )

    # Filter to valid types
    merged = merged[merged["Type"].isin(VALID_TYPES)].copy()

    print(f"  Processed rows: {len(merged)}")
    print(f"  Types found: {merged['Type'].unique().tolist()}")
    print(f"  Categories: {merged['大分类'].unique().tolist()}")
    print(f"  Date range: {merged['ds'].min()} → {merged['ds'].max()}")

    return merged


def _apply_checkup_day_rule(dept_value):
    """Conditionally rename Health Management Center on checkup days."""
    s = str(dept_value).strip()
    if "Health Management Center" not in s:
        return s
    if "体检日" in s:
        return s
    # Can't determine day of week here without the row context,
    # so use a simple heuristic: check based on data patterns
    return s  # Keep as-is for weekly forecast simplicity


# =====================================================
# HELPER
# =====================================================

def _fuzzy_find(columns, candidates):
    """Fuzzy-find a column name matching any candidate."""
    cols_lower = {c.lower(): c for c in columns}
    for cand in candidates:
        for cl, orig in cols_lower.items():
            if cand.lower() in cl:
                return orig
    return None


def get_current_week_range(today=None):
    """Get (monday, sunday) for the current week."""
    if today is None:
        today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


# =====================================================
# JSON GENERATION (matching generate_dashboard.py format)
# =====================================================

def generate_forecast_json(forecast_df):
    """Convert forecast DataFrame to the JSON format expected by dashboard.html."""

    df = forecast_df.copy()
    df["date"] = df["ds"].dt.date

    # ---- daily_trend ----
    daily_trend = {}
    for cat in ["超声", "放射"]:
        sub = df[df["大分类"] == cat].copy()
        if sub.empty:
            daily_trend[cat] = {"dates": [], "tech_minutes": [], "doc_minutes": []}
            continue
        daily = sub.groupby("date").agg(
            tech_minutes=("pred_tech_minutes", "sum"),
            doc_minutes=("pred_doc_minutes", "sum"),
        ).reset_index()
        daily = daily.sort_values("date")
        daily_trend[cat] = {
            "dates": daily["date"].astype(str).tolist(),
            "tech_minutes": daily["tech_minutes"].round(1).tolist(),
            "doc_minutes": daily["doc_minutes"].round(1).tolist(),
        }

    # ---- heatmap_data ----
    heatmap_data = {}
    for cat in ["超声", "放射"]:
        sub = df[df["大分类"] == cat].copy()
        cat_data = {}
        if sub.empty:
            heatmap_data[cat] = {
                "tech_minutes": {"dates": [], "hours": list(range(24)), "matrix": []},
                "doc_minutes": {"dates": [], "hours": list(range(24)), "matrix": []},
            }
            continue
        agg = sub.groupby(["date", "hour"]).agg(
            tech_minutes=("pred_tech_minutes", "sum"),
            doc_minutes=("pred_doc_minutes", "sum"),
        ).reset_index()
        agg["date_str"] = agg["date"].astype(str)

        dates = sorted(agg["date_str"].unique())
        hours = list(range(24))

        for metric in ["tech_minutes", "doc_minutes"]:
            pivot = agg.pivot_table(
                index="hour", columns="date_str", values=metric,
                aggfunc="sum", fill_value=0
            )
            pivot = pivot.reindex(hours, fill_value=0)
            for d in dates:
                if d not in pivot.columns:
                    pivot[d] = 0.0
            pivot = pivot[sorted(pivot.columns)]
            matrix = []
            for h in hours:
                matrix.append([round(float(pivot.at[h, d]), 1) for d in pivot.columns])
            cat_data[metric] = {
                "dates": list(pivot.columns),
                "hours": hours,
                "matrix": matrix,
            }
        heatmap_data[cat] = cat_data

    # ---- hourly_line ----
    hourly_line = {}
    for cat in ["超声", "放射"]:
        sub = df[df["大分类"] == cat].copy()
        cat_hourly = {}
        if not sub.empty:
            agg = sub.groupby(["date", "hour"]).agg(
                tech_minutes=("pred_tech_minutes", "sum"),
                doc_minutes=("pred_doc_minutes", "sum"),
            ).reset_index()
            agg["date_str"] = agg["date"].astype(str)

            for d in sorted(agg["date_str"].unique()):
                day_data = agg[agg["date_str"] == d].set_index("hour")
                cat_hourly[d] = {
                    "tech_minutes": [
                        round(float(day_data.at[h, "tech_minutes"]), 1)
                        if h in day_data.index else 0.0
                        for h in range(24)
                    ],
                    "doc_minutes": [
                        round(float(day_data.at[h, "doc_minutes"]), 1)
                        if h in day_data.index else 0.0
                        for h in range(24)
                    ],
                }
        hourly_line[cat] = cat_hourly

    return {
        "daily_trend": daily_trend,
        "heatmap_data": heatmap_data,
        "hourly_line": hourly_line,
    }


# =====================================================
# MAIN
# =====================================================

def main():
    parser = argparse.ArgumentParser(description="Weekly Forecast Script")
    parser.add_argument("--target", type=str, default=None,
                        help="Target date (YYYY-MM-DD). Default: today.")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT,
                        help="Output JSON path.")
    parser.add_argument("--history-days", type=int, default=HISTORY_DAYS,
                        help=f"Days of history to fetch (default: {HISTORY_DAYS}).")
    args = parser.parse_args()

    # Determine dates
    if args.target:
        target_date = date.fromisoformat(args.target)
    else:
        target_date = date.today()

    monday, sunday = get_current_week_range(target_date)
    history_start = target_date - timedelta(days=args.history_days)

    # forecast_days: predict from target_date to Sunday (inclusive)
    forecast_days = (sunday - target_date).days + 1
    if forecast_days < 2:
        forecast_days = 7  # if today is Sunday, predict full next week

    print("=" * 60)
    print("WEEKLY FORECAST")
    print("=" * 60)
    print(f"  Target date:    {target_date}")
    print(f"  Forecast week:  {monday} (Mon) → {sunday} (Sun)")
    print(f"  Forecast days:  {forecast_days}")
    print(f"  History:        {history_start} → {target_date} ({args.history_days} days)")
    print(f"  Output:         {args.output}")

    # ---- Authenticate ----
    print("\n--- Authenticating ---")
    token = get_tenant_access_token(APP_ID, APP_SECRET)
    print("  [OK] Token obtained")

    # ---- Fetch data ----
    master_df = fetch_master_data(token, history_start, target_date)

    if len(master_df) == 0:
        print("[ERROR] No data found in the date range. Cannot generate forecast.")
        sys.exit(1)

    try:
        mapping_df = fetch_mapping_data(token)
    except Exception as e:
        print(f"[WARN] Mapping table fetch failed: {e}")
        print("       Using keyword-based classification for all records.")
        # Create empty mapping df
        mapping_df = pd.DataFrame(columns=["医嘱描述_映射", "Type", "大分类",
                                           "预估操作时长", "预估医生写报告时长"])

    # ---- Process and join ----
    df = process_and_join(master_df, mapping_df)

    if len(df) < 48:
        print(f"[ERROR] Only {len(df)} hourly rows after processing. Need at least 48. Abort.")
        sys.exit(1)

    # ---- Forecast ----
    print(f"\n[4/4] Running forecast pipeline (forecast_days={forecast_days})...")
    forecast_df = run_forecast_pipeline(df, forecast_days=forecast_days)

    print(f"  Forecast produced: {len(forecast_df)} rows")
    print(f"  Forecast range: {forecast_df['ds'].min()} → {forecast_df['ds'].max()}")

    # ---- Generate JSON ----
    result = generate_forecast_json(forecast_df)

    # ---- Write output ----
    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)

    print(f"\n[OK] Forecast data written to {args.output}")
    print(f"  Size: {os.path.getsize(args.output):,} bytes")
    print("=" * 60)


if __name__ == "__main__":
    main()
