"""
Daily Comparison Script
每日对比飞书机器人真实预约 vs 月度流水线预测，
输出 publish/comparison_data.json 供 dashboard.html 展示。

Usage:
  python daily_compare.py                          # 默认：对比明天
  python daily_compare.py --target 2026-06-16     # 指定对比日期
"""

import os
import sys
import json
import argparse
from datetime import date, timedelta

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Input files
DAILY_DATA_JSON = os.path.join(SCRIPT_DIR, "publish", "daily_data.json")
MONTHLY_FORECAST_CSV = os.path.join(SCRIPT_DIR, "publish", "monthly_forecast_hourly.csv")

# Output file
COMPARISON_JSON = os.path.join(SCRIPT_DIR, "publish", "comparison_data.json")

# Modality minute estimates (tech_minutes, doc_minutes)
# Same as feishu_bot.py ESTIMATES
ESTIMATES = {
    "MRI": (30, 20),
    "CT": (15, 10),
    "X-ray": (10, 5),
    "Mammo": (10, 5),
    "BoneDensity": (10, 5),
    "B-ultrasound": (20, 10),
    "Echo": (20, 10),
}


def extract_bot_data(daily_data):
    """从飞书机器人数据提取3类别的技师/医生分钟数。

    Returns:
        dict with keys: ob_ultrasound, hc_ultrasound, hc_radiology
        Each value: {"tech_minutes": float, "doc_minutes": float, "persons": int}
    """
    result = {
        "ob_ultrasound": {"tech_minutes": 0.0, "doc_minutes": 0.0, "persons": 0},
        "hc_ultrasound": {"tech_minutes": 0.0, "doc_minutes": 0.0, "persons": 0},
        "hc_radiology": {"tech_minutes": 0.0, "doc_minutes": 0.0, "persons": 0},
    }

    # ---- Category A: OB ultrasound ----
    ob = daily_data.get("ob", {})
    ob_total = 0
    for cls_name in ["OB", "NT", "Anatomy"]:
        ob_total += int(ob.get("total_counts", {}).get(cls_name, 0) or 0)
    result["ob_ultrasound"]["persons"] = ob_total
    result["ob_ultrasound"]["tech_minutes"] = ob_total * 20   # B-ultrasound ESTIMATES
    result["ob_ultrasound"]["doc_minutes"] = ob_total * 10

    # ---- Category B & C: Health check (体检) ----
    checkup = daily_data.get("checkup", {})
    tc = checkup.get("total_counts", {})

    bus = int(tc.get("B-ultrasound", 0) or 0)
    echo = int(tc.get("Echo", 0) or 0)
    mri = int(tc.get("MRI", 0) or 0)
    ct = int(tc.get("CT", 0) or 0)
    xray = int(tc.get("X-ray", 0) or 0)
    mammo = int(tc.get("Mammo", 0) or 0)
    bone = int(tc.get("BoneDensity", 0) or 0)

    # Category B: 体检超声 = B超 + 心彩
    result["hc_ultrasound"]["persons"] = bus + echo
    result["hc_ultrasound"]["tech_minutes"] = bus * ESTIMATES["B-ultrasound"][0] + echo * ESTIMATES["Echo"][0]
    result["hc_ultrasound"]["doc_minutes"] = bus * ESTIMATES["B-ultrasound"][1] + echo * ESTIMATES["Echo"][1]

    # Category C: 体检放射 = 非超声项目
    result["hc_radiology"]["persons"] = mri + ct + xray + mammo + bone
    result["hc_radiology"]["tech_minutes"] = (
        mri * ESTIMATES["MRI"][0] +
        ct * ESTIMATES["CT"][0] +
        xray * ESTIMATES["X-ray"][0] +
        mammo * ESTIMATES["Mammo"][0] +
        bone * ESTIMATES["BoneDensity"][0]
    )
    result["hc_radiology"]["doc_minutes"] = (
        mri * ESTIMATES["MRI"][1] +
        ct * ESTIMATES["CT"][1] +
        xray * ESTIMATES["X-ray"][1] +
        mammo * ESTIMATES["Mammo"][1] +
        bone * ESTIMATES["BoneDensity"][1]
    )

    return result


def extract_forecast_data(forecast_df, target_date):
    """从月度预测 DataFrame 提取3类别的技师/医生分钟数。

    forecast_df columns: ds, pred_cases, hour, weekday, eps_dept_desc, Type,
                         ..., pred_tech_minutes, pred_doc_minutes, 大分类
    """
    result = {
        "ob_ultrasound": {"tech_minutes": 0.0, "doc_minutes": 0.0},
        "hc_ultrasound": {"tech_minutes": 0.0, "doc_minutes": 0.0},
        "hc_radiology": {"tech_minutes": 0.0, "doc_minutes": 0.0},
    }

    if forecast_df.empty:
        return result

    # Filter to target date
    df = forecast_df.copy()
    df["ds"] = pd.to_datetime(df["ds"])
    df["date"] = df["ds"].dt.date
    day_df = df[df["date"] == target_date]

    if day_df.empty:
        print(f"  [WARN] No forecast data for {target_date}")
        return result

    # Category A: OB ultrasound
    # eps_dept_desc == "GZU OBGYN" AND Type == "Ultrasound"
    ob_df = day_df[(day_df["eps_dept_desc"] == "GZU OBGYN") &
                   (day_df["Type"].astype(str).str.strip() == "Ultrasound")]
    result["ob_ultrasound"]["tech_minutes"] = round(float(ob_df["pred_tech_minutes"].sum()), 1)
    result["ob_ultrasound"]["doc_minutes"] = round(float(ob_df["pred_doc_minutes"].sum()), 1)

    # Health Management Center — CSV 已在 forecast_core 中按 weekday 清理过
    # 每天只会有正确的变体（体检日/非体检日），直接用两个筛即可
    hc_depts = ["GZU Health Management Center", "GZU Health Management Center 体检日"]
    hc_df = day_df[day_df["eps_dept_desc"].isin(hc_depts)]
    if not hc_df.empty:
        actual_dept = hc_df["eps_dept_desc"].iloc[0]
        print(f"  [HC] weekday={target_date.weekday()}, using dept='{actual_dept}', rows={len(hc_df)}")

    # Category B: 体检超声
    us_types = ["Ultrasound", "Echocardiograms"]
    hc_us_df = hc_df[hc_df["Type"].astype(str).str.strip().isin(us_types)]
    result["hc_ultrasound"]["tech_minutes"] = round(float(hc_us_df["pred_tech_minutes"].sum()), 1)
    result["hc_ultrasound"]["doc_minutes"] = round(float(hc_us_df["pred_doc_minutes"].sum()), 1)

    # Category C: 体检放射 (non-Ultrasound, non-Echocardiograms)
    hc_rad_df = hc_df[~hc_df["Type"].astype(str).str.strip().isin(us_types)]
    result["hc_radiology"]["tech_minutes"] = round(float(hc_rad_df["pred_tech_minutes"].sum()), 1)
    result["hc_radiology"]["doc_minutes"] = round(float(hc_rad_df["pred_doc_minutes"].sum()), 1)

    return result


def build_comparison(bot_data, forecast_data, target_date):
    """构建最终的对比 JSON。"""
    categories = {}
    cat_configs = [
        ("ob_ultrasound", "OB超声"),
        ("hc_ultrasound", "体检超声"),
        ("hc_radiology", "体检放射"),
    ]

    for key, label in cat_configs:
        bot = bot_data.get(key, {})
        fc = forecast_data.get(key, {})
        bot_tech = bot.get("tech_minutes", 0.0)
        bot_doc = bot.get("doc_minutes", 0.0)
        fc_tech = fc.get("tech_minutes", 0.0)
        fc_doc = fc.get("doc_minutes", 0.0)

        # Compute diff percentages (avoid division by zero)
        tech_diff_pct = round((fc_tech - bot_tech) / bot_tech * 100, 1) if bot_tech > 0 else None
        doc_diff_pct = round((fc_doc - bot_doc) / bot_doc * 100, 1) if bot_doc > 0 else None

        categories[key] = {
            "label": label,
            "bot_tech_minutes": bot_tech,
            "bot_doc_minutes": bot_doc,
            "forecast_tech_minutes": fc_tech,
            "forecast_doc_minutes": fc_doc,
            "bot_persons": bot.get("persons", 0),
            "tech_diff_pct": tech_diff_pct,
            "doc_diff_pct": doc_diff_pct,
        }

    return {
        "target_date": str(target_date),
        "categories": categories,
    }


def main():
    parser = argparse.ArgumentParser(description="Daily Comparison Script")
    parser.add_argument("--target", type=str, default=None,
                        help="Target date (YYYY-MM-DD). Default: tomorrow.")
    parser.add_argument("--bot-data", type=str, default=DAILY_DATA_JSON,
                        help="Path to daily_data.json (bot output).")
    parser.add_argument("--forecast-csv", type=str, default=MONTHLY_FORECAST_CSV,
                        help="Path to monthly forecast CSV.")
    parser.add_argument("--output", type=str, default=COMPARISON_JSON,
                        help="Output JSON path.")
    args = parser.parse_args()

    if args.target:
        target_date = date.fromisoformat(args.target)
    else:
        target_date = date.today() + timedelta(days=1)

    print("=" * 50)
    print("DAILY COMPARISON: Bot Real vs Monthly Forecast")
    print(f"  Target date: {target_date}")
    print("=" * 50)

    # ---- 1. Read bot data ----
    print(f"\n[1/3] Reading bot data: {args.bot_data}")
    try:
        with open(args.bot_data, "r", encoding="utf-8") as f:
            daily_data = json.load(f)
        print(f"  [OK] Loaded. Checkup total_persons={daily_data.get('checkup', {}).get('total_persons', 0)}, "
              f"OB counts={daily_data.get('ob', {}).get('total_counts', {})}")
    except FileNotFoundError:
        print(f"  [WARN] daily_data.json not found. Using zero values.")
        daily_data = {"checkup": {}, "ob": {}}
    except Exception as e:
        print(f"  [ERROR] Failed to load: {e}")
        daily_data = {"checkup": {}, "ob": {}}

    bot_data = extract_bot_data(daily_data)

    # ---- 2. Read forecast data ----
    print(f"\n[2/3] Reading forecast CSV: {args.forecast_csv}")
    try:
        forecast_df = pd.read_csv(args.forecast_csv, encoding="utf-8-sig")
        print(f"  [OK] Loaded {len(forecast_df)} rows")
        print(f"  Columns: {list(forecast_df.columns)[:8]}...")
    except FileNotFoundError:
        print(f"  [WARN] monthly_forecast_hourly.csv not found. Using zero values.")
        forecast_df = pd.DataFrame()
    except Exception as e:
        print(f"  [ERROR] Failed to load: {e}")
        forecast_df = pd.DataFrame()

    forecast_data = extract_forecast_data(forecast_df, target_date)

    # ---- 3. Build and write comparison ----
    print(f"\n[3/3] Building comparison JSON...")
    comparison = build_comparison(bot_data, forecast_data, target_date)

    for key, cat in comparison["categories"].items():
        print(f"  {cat['label']}:")
        print(f"    Bot:      tech={cat['bot_tech_minutes']}min, doc={cat['bot_doc_minutes']}min ({cat['bot_persons']}人)")
        print(f"    Forecast: tech={cat['forecast_tech_minutes']}min, doc={cat['forecast_doc_minutes']}min")
        if cat['tech_diff_pct'] is not None:
            print(f"    Tech diff: {cat['tech_diff_pct']:+.1f}%")
        if cat['doc_diff_pct'] is not None:
            print(f"    Doc diff:  {cat['doc_diff_pct']:+.1f}%")

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2)

    print(f"\n[OK] Comparison written to {args.output}")
    print("=" * 50)


if __name__ == "__main__":
    main()
