"""
=========================================================
体检明日资源需求日报 — 仪表盘生成器
读取 forecast_output 中的 CSV，生成交互式 HTML 仪表盘
=========================================================
"""
import json
import os
import pandas as pd
import numpy as np
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "forecast_output")

HOURLY_CSV = os.path.join(OUTPUT_DIR, "Demand_Forecast_Hourly.csv")
DAILY_CSV = os.path.join(OUTPUT_DIR, "Demand_Forecast_Daily.csv")
REAL_SUMMARY = os.path.join(OUTPUT_DIR, "Real_Daily_Summary.csv")
FORECAST_SUMMARY = os.path.join(OUTPUT_DIR, "Forecast_Daily_Summary.csv")
TEMPLATE_HTML = os.path.join(SCRIPT_DIR, "dashboard_template.html")


def load_data():
    """加载所有 CSV，返回聚合好的数据结构"""
    df_h = pd.read_csv(HOURLY_CSV, encoding="utf-8-sig")
    df_h["ds"] = pd.to_datetime(df_h["ds"])
    df_h["date"] = df_h["ds"].dt.date
    df_h["hour"] = df_h["ds"].dt.hour

    df_d = pd.read_csv(DAILY_CSV, encoding="utf-8-sig")
    df_d["ds"] = pd.to_datetime(df_d["ds"])

    # ---- 日报汇总 ----
    real_data = {}
    forecast_data = {}
    try:
        df_real = pd.read_csv(REAL_SUMMARY, encoding="utf-8-sig")
        if not df_real.empty:
            real_data = df_real.iloc[0].to_dict()
            for k, v in real_data.items():
                if isinstance(v, (np.integer,)):
                    real_data[k] = int(v)
                elif isinstance(v, (np.floating,)):
                    real_data[k] = round(float(v), 1)
    except Exception:
        pass

    try:
        df_forecast = pd.read_csv(FORECAST_SUMMARY, encoding="utf-8-sig")
        if not df_forecast.empty:
            forecast_data = df_forecast.iloc[0].to_dict()
            for k, v in forecast_data.items():
                if isinstance(v, (np.integer,)):
                    forecast_data[k] = int(v)
                elif isinstance(v, (np.floating,)):
                    forecast_data[k] = round(float(v), 1)
    except Exception:
        pass

    # ---- 日度趋势数据 (按 大分类 + date 聚合) ----
    daily_trend = {}
    for cat in ["超声", "放射"]:
        sub = df_d[df_d["大分类"] == cat].copy()
        sub = sub.sort_values("ds")
        daily_trend[cat] = {
            "dates": sub["ds"].dt.strftime("%Y-%m-%d").tolist(),
            "tech_minutes": sub["pred_tech_minutes"].round(1).tolist(),
            "doc_minutes": sub["pred_doc_minutes"].round(1).tolist(),
        }

    # ---- 小时热力图数据: date × hour 矩阵 ----
    heatmap_data = {}
    for cat in ["超声", "放射"]:
        cat_data = {}
        sub = df_h[df_h["大分类"] == cat].copy()
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

    # ---- 小时折线: 每日每小时的明细 (用于选中日期时展示) ----
    hourly_line = {}
    for cat in ["超声", "放射"]:
        sub = df_h[df_h["大分类"] == cat].copy()
        agg = sub.groupby(["date", "hour"]).agg(
            tech_minutes=("pred_tech_minutes", "sum"),
            doc_minutes=("pred_doc_minutes", "sum"),
        ).reset_index()
        agg["date_str"] = agg["date"].astype(str)

        cat_hourly = {}
        for d in sorted(agg["date_str"].unique()):
            day_data = agg[agg["date_str"] == d].set_index("hour")
            cat_hourly[d] = {
                "tech_minutes": [round(float(day_data.at[h, "tech_minutes"]), 1)
                                 if h in day_data.index else 0.0 for h in range(24)],
                "doc_minutes": [round(float(day_data.at[h, "doc_minutes"]), 1)
                                if h in day_data.index else 0.0 for h in range(24)],
            }
        hourly_line[cat] = cat_hourly

    # ---- 可用日期列表 ----
    all_dates = sorted(df_h["date"].dropna().unique().astype(str).tolist())

    # ---- 真实预约数据 ----
    real_reservations = load_real_reservations()

    return {
        "daily_trend": daily_trend,
        "heatmap_data": heatmap_data,
        "hourly_line": hourly_line,
        "all_dates": all_dates,
        "tomorrow_date": str(date.today() + timedelta(days=1)),
        "real_data": real_data,
        "forecast_data": forecast_data,
        "real_reservations": real_reservations,
    }


def load_real_reservations():
    """Load real reservation CSVs (from fetch_real_reservations.py output)."""
    CHECKUP_CSV = os.path.join(OUTPUT_DIR, "Real_Reservations_Checkup.csv")
    OB_CSV = os.path.join(OUTPUT_DIR, "Real_Reservations_OB.csv")
    result = {"checkup": None, "ob": None}
    for label, path in [("checkup", CHECKUP_CSV), ("ob", OB_CSV)]:
        if os.path.exists(path):
            try:
                df = pd.read_csv(path, encoding="utf-8-sig")
                # Remove the TOTAL row
                df = df[df["time_slot"] != "TOTAL"]
                result[label] = df.to_dict(orient="list")
                for k, v in result[label].items():
                    if isinstance(v, dict):
                        result[label][k] = list(v.values())
            except Exception as e:
                print(f"  [WARN] Failed to load {label} reservations: {e}")
    return result


def generate_html(data):
    """读取模板文件，替换数据占位符，输出最终 HTML"""
    output = os.path.join(OUTPUT_DIR, "dashboard.html")

    # 读取模板（纯 HTML + JS，无任何转义）
    with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    # 序列化数据并嵌入
    json_data = json.dumps(data, ensure_ascii=False, default=str)
    html = html.replace("__DATA_PLACEHOLDER__", json_data)

    with open(output, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[OK] Dashboard generated: {output}")
    return output


def main():
    print("=" * 50)
    print("Dashboard Generator for GZU Medical Demand Forecast")
    print("=" * 50)
    data = load_data()
    path = generate_html(data)
    print(f"\nOpen in browser: {path}")


if __name__ == "__main__":
    main()
