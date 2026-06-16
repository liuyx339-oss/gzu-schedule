"""
Weekly Forecast Script
从月度预测 CSV 提取当周数据，生成 publish/forecast_data.json。
不做 Feishu 拉取，不跑 Prophet——月度流水线已经生成了全部数据。

Usage:
  python weekly_forecast.py                          # 默认：预测当周 (周一~周日)
  python weekly_forecast.py --target 2026-06-16      # 指定"今天"，预测其所在周
  python weekly_forecast.py --output custom_path.json
"""

import os
import sys
import json
import argparse
from datetime import date, timedelta

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

MONTHLY_CSV = os.path.join(SCRIPT_DIR, "publish", "monthly_forecast_hourly.csv")
DEFAULT_OUTPUT = os.path.join(SCRIPT_DIR, "publish", "forecast_data.json")


def generate_forecast_json(df):
    """Convert forecast DataFrame slice to the JSON format expected by dashboard.html."""

    df = df.copy()
    df["date"] = df["ds"].dt.date
    df["hour"] = df["ds"].dt.hour
    df["date_str"] = df["date"].astype(str)

    # ---- daily_trend ----
    daily_trend = {}
    for cat in ["超声", "放射"]:
        sub = df[df["大分类"] == cat]
        if sub.empty:
            daily_trend[cat] = {"dates": [], "tech_minutes": [], "doc_minutes": []}
            continue
        daily = (
            sub.groupby("date")
            .agg(
                tech_minutes=("pred_tech_minutes", "sum"),
                doc_minutes=("pred_doc_minutes", "sum"),
            )
            .reset_index()
            .sort_values("date")
        )
        daily_trend[cat] = {
            "dates": daily["date"].astype(str).tolist(),
            "tech_minutes": daily["tech_minutes"].round(1).tolist(),
            "doc_minutes": daily["doc_minutes"].round(1).tolist(),
        }

    # ---- heatmap_data ----
    heatmap_data = {}
    for cat in ["超声", "放射"]:
        sub = df[df["大分类"] == cat]
        if sub.empty:
            heatmap_data[cat] = {
                "tech_minutes": {"dates": [], "hours": list(range(24)), "matrix": []},
                "doc_minutes": {"dates": [], "hours": list(range(24)), "matrix": []},
            }
            continue
        agg = (
            sub.groupby(["date_str", "hour"])
            .agg(
                tech_minutes=("pred_tech_minutes", "sum"),
                doc_minutes=("pred_doc_minutes", "sum"),
            )
            .reset_index()
        )
        dates = sorted(agg["date_str"].unique())
        hours = list(range(24))
        cat_data = {}
        for metric in ["tech_minutes", "doc_minutes"]:
            mkey = "tech_minutes" if metric == "tech_minutes" else "doc_minutes"
            pivot = agg.pivot_table(
                index="hour", columns="date_str", values=metric, aggfunc="sum", fill_value=0
            )
            pivot = pivot.reindex(hours, fill_value=0)
            for d in dates:
                if d not in pivot.columns:
                    pivot[d] = 0.0
            pivot = pivot[sorted(pivot.columns)]
            matrix = [[round(float(pivot.at[h, d]), 1) for d in pivot.columns] for h in hours]
            cat_data[mkey] = {
                "dates": list(pivot.columns),
                "hours": hours,
                "matrix": matrix,
            }
        heatmap_data[cat] = cat_data

    # ---- hourly_line ----
    hourly_line = {}
    for cat in ["超声", "放射"]:
        sub = df[df["大分类"] == cat]
        cat_hourly = {}
        if not sub.empty:
            agg = (
                sub.groupby(["date_str", "hour"])
                .agg(
                    tech_minutes=("pred_tech_minutes", "sum"),
                    doc_minutes=("pred_doc_minutes", "sum"),
                )
                .reset_index()
            )
            for d in sorted(agg["date_str"].unique()):
                day_data = agg[agg["date_str"] == d].set_index("hour")
                cat_hourly[d] = {
                    "tech_minutes": [
                        round(float(day_data.at[h, "tech_minutes"]), 1)
                        if h in day_data.index
                        else 0.0
                        for h in range(24)
                    ],
                    "doc_minutes": [
                        round(float(day_data.at[h, "doc_minutes"]), 1)
                        if h in day_data.index
                        else 0.0
                        for h in range(24)
                    ],
                }
        hourly_line[cat] = cat_hourly

    return {
        "daily_trend": daily_trend,
        "heatmap_data": heatmap_data,
        "hourly_line": hourly_line,
    }


def main():
    parser = argparse.ArgumentParser(description="Weekly Forecast — extract from monthly CSV")
    parser.add_argument("--target", type=str, default=None)
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT)
    parser.add_argument("--csv", type=str, default=MONTHLY_CSV)
    args = parser.parse_args()

    target_date = date.fromisoformat(args.target) if args.target else date.today()
    monday = target_date - timedelta(days=target_date.weekday())
    sunday = monday + timedelta(days=6)

    print("=" * 50)
    print("WEEKLY FORECAST (from monthly CSV)")
    print(f"  Target: {target_date}")
    print(f"  Week:   {monday} → {sunday}")
    print(f"  CSV:    {args.csv}")
    print(f"  Output: {args.output}")
    print("=" * 50)

    # Read monthly forecast
    if not os.path.exists(args.csv):
        print(f"[ERROR] CSV not found: {args.csv}")
        print("  Run prophet_lightGBM.py or run_pipeline.py first.")
        sys.exit(1)

    df = pd.read_csv(args.csv, encoding="utf-8-sig")
    df["ds"] = pd.to_datetime(df["ds"])

    # Slice current week
    week_df = df[(df["ds"].dt.date >= monday) & (df["ds"].dt.date <= sunday)].copy()

    if week_df.empty:
        print(f"[WARN] No forecast data for {monday}→{sunday}")
        print("  Monthly CSV may be stale. Run pipeline to regenerate.")
        sys.exit(1)

    print(f"  Rows: {len(week_df)}")
    dates_found = sorted(week_df["ds"].dt.date.unique())
    print(f"  Dates: {dates_found[0]} → {dates_found[-1]} ({len(dates_found)} days)")

    # Generate JSON
    result = generate_forecast_json(week_df)

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)

    print(f"\n[OK] Written {os.path.getsize(args.output):,} bytes to {args.output}")
    for cat in ["超声", "放射"]:
        n = len(result["daily_trend"][cat]["dates"])
        print(f"  {cat}: {n} days")

    # CI: also commit the output if running in GitHub Actions
    if os.environ.get("GITHUB_ACTIONS"):
        print("\n[CI] Running in GitHub Actions — forecast data ready for deploy")
    else:
        print("\nDone. Remember to git push if running locally.")


if __name__ == "__main__":
    main()
