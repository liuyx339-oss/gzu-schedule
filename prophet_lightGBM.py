"""
=========================================================
Medical Demand Forecasting System
FINAL PRODUCTION VERSION
Prophet + LightGBM + Hierarchical Forecasting
=========================================================

核心逻辑
---------------------------------------------------------
STEP 1
大分类拆分:
    - 超声
    - 放射

STEP 2
Type Total Forecast
    Prophet + LightGBM Hybrid

STEP 3
关键科室 Forecast
    Prophet + LightGBM Hybrid

STEP 4
Remaining Pool Engine
    total_type - key_departments

STEP 5
非关键科室动态分配
    weekday + hour 动态比例

STEP 6
项目拆分
    weekday + hour + dept 动态比例

STEP 7
Workload Translation
    project_count × standard_minutes

STEP 8
输出：
    1. 小时级预测 CSV
    2. 日度预测 CSV
    3. 热力图
    4. 趋势图

=========================================================
"""

import os
import sys
import warnings
import logging
import shutil
from datetime import date, timedelta

import requests
import numpy as np
import pandas as pd

import matplotlib.pyplot as plt
import seaborn as sns

from prophet import Prophet
from lightgbm import LGBMRegressor

# ---- Forecast functions imported from forecast_core ----
from forecast_core import (
    FORECAST_DAYS, TIMEZONE, VALID_TYPES,
    US_KEY_DEPTS, RAD_KEY_DEPTS,
    add_time_features, build_prophet, hybrid_forecast,
    forecast_total_type, forecast_key_departments,
    build_remaining_pool, allocate_remaining_pool,
    split_order_items, build_standard_minutes, translate_workload,
    run_forecast_pipeline,
)

warnings.filterwarnings("ignore")

logging.getLogger("prophet").setLevel(logging.ERROR)

plt.rcParams["font.sans-serif"] = ["SimHei"]
plt.rcParams["axes.unicode_minus"] = False



# =====================================================
# 明日资源需求日报 CONFIG
# =====================================================

NEW_BASE_TOKEN = "NM6HbB8gKaqtDysTTrRcve0ZnAc"

# 放射项目字段
RAD_FIELDS = ["MRI", "CT", "X-ray", "钼靶", "骨密度"]

# 超声项目字段
US_FIELDS = ["B超", "心彩"]

# weekday → eps_dept_desc 过滤映射
WEEKDAY_DEPT_FILTER = {
    0: "GZU Health Management Center 体检日",  # 周一
    2: "GZU Health Management Center 体检日",  # 周三
    4: "GZU Health Management Center 体检日",  # 周五
    5: "GZU Health Management Center 体检日",  # 周六
    1: "GZU Health Management Center",         # 周二
    3: "GZU Health Management Center",         # 周四
    6: "GZU Health Management Center",         # 周日
}



# =====================================================
# HYBRID FORECAST
# =====================================================


# =====================================================
# TYPE FORECAST
# =====================================================


# =====================================================
# KEY DEPARTMENT FORECAST
# =====================================================


# =====================================================
# REMAINING POOL
# =====================================================


# =====================================================
# NON KEY ALLOCATION
# =====================================================


# =====================================================
# ORDER ITEM SPLIT
# =====================================================


# =====================================================
# STANDARD MINUTES
# =====================================================


# =====================================================
# WORKLOAD
# =====================================================


# =====================================================
# HEATMAP
# =====================================================

def plot_heatmap(
    df,
    value_col,
    title,
    save_path,
):

    temp = df.copy()

    temp["date"] = (
        temp["ds"].dt.date
    )

    temp["hour"] = (
        temp["ds"].dt.hour
    )

    pivot = temp.pivot_table(
        index="hour",
        columns="date",
        values=value_col,
        aggfunc="sum",
    )

    if pivot.empty:
        return

    plt.figure(figsize=(16, 5))

    sns.heatmap(
        pivot,
        cmap="YlOrRd",
    )

    plt.title(title)

    plt.tight_layout()

    plt.savefig(
        save_path,
        dpi=120,
    )

    plt.close()

# =====================================================
# DAILY TREND
# =====================================================

def plot_daily_trend(
    df,
    value_col,
    title,
    save_path,
):

    daily = (
        df.groupby(
            df["ds"].dt.date
        )[value_col]
        .sum()
        .reset_index()
    )

    plt.figure(figsize=(14, 5))

    plt.plot(
        daily["ds"],
        daily[value_col],
        marker="o",
    )

    plt.title(title)

    plt.xticks(rotation=45)

    plt.grid(True)

    plt.tight_layout()

    plt.savefig(
        save_path,
        dpi=120,
    )

    plt.close()

# =====================================================
# 明日资源需求日报
# =====================================================

def _discover_table_id(token, base_token):
    """自动发现飞书 Base 中的表格 ID。
    如果只有一个表，直接返回其 ID；
    如果有多个表，列出所有表名并抛出异常提示用户指定。
    """
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base_token}/tables"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    session = requests.Session()
    session.trust_env = False
    resp = session.get(url, headers=headers, timeout=(10, 60))
    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(
            f"获取表格列表失败: code={data.get('code')}, msg={data.get('msg')}"
        )

    tables = data.get("data", {}).get("items", [])
    if not tables:
        raise RuntimeError(f"Base {base_token} 中未找到任何表格")

    print(f"  [发现] Base 中共 {len(tables)} 个表格:")
    for t in tables:
        print(f"    table_id={t['table_id']}, name={t.get('name', '?')}")

    if len(tables) == 1:
        return tables[0]["table_id"]

    names = [t.get("name", "?") for t in tables]
    raise RuntimeError(
        f"发现多个表格，请在脚本中设置 TABLE_NAME 为以下之一: {names}"
    )


def _fetch_real_data(token, base_token, table_id, target_date):
    """从飞书多维表格拉取预约数据，筛选 appt_dt == target_date 的行。
    返回 DataFrame，列包含 MRI, CT, X-ray, 钼靶, 骨密度, B超, 心彩 的数量。
    """
    import fetch_feishu_data as fdf

    # 获取字段元数据
    field_map = fdf.get_field_meta(token, base_token, table_id)
    print(f"  [字段] 共 {len(field_map)} 个字段:")
    for fid, meta in field_map.items():
        print(f"    {fid}: {meta['name']} (type={meta['type']})")

    # 拉取全部记录
    records = fdf.get_all_records(token, base_token, table_id)
    print(f"  [记录] 共拉取 {len(records)} 条记录")

    # 转换为 DataFrame
    df = fdf.records_to_dataframe(records, field_map)

    # 查找 appt_dt 列（支持模糊匹配）
    appt_col = None
    for col in df.columns:
        if "appt" in col.lower() or "日期" in col or "date" in col.lower():
            appt_col = col
            print(f"  [映射] 使用列 '{col}' 作为 appt_dt")
            break

    if appt_col is None:
        available = list(df.columns)
        raise RuntimeError(
            f"未找到 appt_dt 字段。可用列: {available}"
        )

    # 解析日期并筛选
    df["_date"] = pd.to_datetime(df[appt_col], errors="coerce").dt.date
    df = df[df["_date"] == target_date].copy()
    df = df.drop(columns=["_date"])

    if df.empty:
        print(f"  [WARN] 飞书表格中未找到 {target_date} 的数据")
    else:
        print(f"  [OK] 筛选到 {len(df)} 行 {target_date} 的数据")

    return df


def _compute_real_workload(df):
    """从真实预约数据计算各项检查数量和工作量分钟数。"""
    result = {
        "date": date.today() + timedelta(days=1),
        "MRI_count": 0, "CT_count": 0, "Xray_count": 0,
        "Mammo_count": 0, "BoneDensity_count": 0,
        "US_count": 0, "Echo_count": 0,
        "rad_tech_minutes": 0.0, "rad_doc_minutes": 0.0,
        "us_scan_minutes": 0.0, "us_doc_minutes": 0.0,
    }

    if df.empty:
        return result

    row = df.iloc[0]

    def _get(col_name):
        """安全获取列值，缺失或非数值返回 0"""
        for c in df.columns:
            if c == col_name or c.strip() == col_name.strip():
                val = row[c]
                try:
                    return int(float(val)) if pd.notna(val) else 0
                except (ValueError, TypeError):
                    return 0
        return 0

    result["MRI_count"] = _get("MRI")
    result["CT_count"] = _get("CT")
    result["Xray_count"] = _get("X-ray")
    result["Mammo_count"] = _get("钼靶")
    result["BoneDensity_count"] = _get("骨密度")
    result["US_count"] = _get("B超")
    result["Echo_count"] = _get("心彩")

    # 放射工作量
    result["rad_tech_minutes"] = (
        result["MRI_count"] * 30
        + result["CT_count"] * 15
        + result["Xray_count"] * 10
        + result["Mammo_count"] * 10
        + result["BoneDensity_count"] * 10
    )

    result["rad_doc_minutes"] = (
        result["MRI_count"] * 15
        + result["CT_count"] * 10
        + result["Xray_count"] * 5
        + result["Mammo_count"] * 10
    )

    # 超声工作量
    result["us_scan_minutes"] = (
        result["US_count"] * 10
        + result["Echo_count"] * 15
    )

    result["us_doc_minutes"] = (
        result["US_count"] * 10
        + result["Echo_count"] * 10
    )

    return result


def _read_forecast_data(forecast_csv_path, target_date):
    """读取 Demand_Forecast_Hourly.csv，按 target_date 和 weekday 过滤。"""
    if not os.path.exists(forecast_csv_path):
        print(f"  [WARN] 预测文件不存在: {forecast_csv_path}")
        print(f"         请先运行 prophet_lightGBM.py 生成预测数据")
        return pd.DataFrame()

    df = pd.read_csv(forecast_csv_path, encoding="utf-8-sig")

    df["ds"] = pd.to_datetime(df["ds"])
    df["date"] = df["ds"].dt.date
    df["weekday"] = df["ds"].dt.dayofweek

    # 筛选目标日期
    df = df[df["date"] == target_date].copy()

    if df.empty:
        print(f"  [WARN] 预测数据中未找到 {target_date} 的数据")
        return df

    # 按 weekday 过滤 eps_dept_desc
    target_weekday = target_date.weekday()
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    expected_dept = WEEKDAY_DEPT_FILTER.get(target_weekday)
    if expected_dept:
        before_count = len(df)
        df = df[df["eps_dept_desc"] == expected_dept].copy()
        after_count = len(df)
        print(
            f"  [过滤] target_date={target_date} ({weekday_names[target_weekday]}), "
            f"eps_dept_desc='{expected_dept}', "
            f"过滤前 {before_count} 行 → 过滤后 {after_count} 行"
        )
    else:
        print(f"  [WARN] weekday={target_weekday} 未在 WEEKDAY_DEPT_FILTER 中配置，跳过部门过滤")

    return df


def _compute_forecast_workload(df):
    """从预测数据按大分类汇总技师和医生分钟数。"""
    result = {
        "date": date.today() + timedelta(days=1),
        "rad_pred_tech_minutes": 0.0,
        "rad_pred_doc_minutes": 0.0,
        "us_pred_tech_minutes": 0.0,
        "us_pred_doc_minutes": 0.0,
    }

    if df.empty:
        return result

    for cat in ["放射", "超声"]:
        cat_df = df[df["大分类"] == cat]
        if cat_df.empty:
            continue
        tech_sum = cat_df["pred_tech_minutes"].sum()
        doc_sum = cat_df["pred_doc_minutes"].sum()
        if cat == "放射":
            result["rad_pred_tech_minutes"] = round(tech_sum, 1)
            result["rad_pred_doc_minutes"] = round(doc_sum, 1)
        else:
            result["us_pred_tech_minutes"] = round(tech_sum, 1)
            result["us_pred_doc_minutes"] = round(doc_sum, 1)

    return result


def _save_real_summary_csv(real_data, output_dir):
    """保存真实预约汇总 CSV。"""
    df = pd.DataFrame([{
        "date": real_data["date"],
        "MRI_count": real_data["MRI_count"],
        "CT_count": real_data["CT_count"],
        "Xray_count": real_data["Xray_count"],
        "Mammo_count": real_data["Mammo_count"],
        "BoneDensity_count": real_data["BoneDensity_count"],
        "US_count": real_data["US_count"],
        "Echo_count": real_data["Echo_count"],
        "rad_tech_minutes": real_data["rad_tech_minutes"],
        "rad_doc_minutes": real_data["rad_doc_minutes"],
        "us_scan_minutes": real_data["us_scan_minutes"],
        "us_doc_minutes": real_data["us_doc_minutes"],
    }])

    path = os.path.join(output_dir, "Real_Daily_Summary.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  [输出] {path}")
    return path


def _save_forecast_summary_csv(forecast_data, output_dir):
    """保存预测需求汇总 CSV。"""
    df = pd.DataFrame([forecast_data])

    path = os.path.join(output_dir, "Forecast_Daily_Summary.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  [输出] {path}")
    return path


def _print_console_report(real_data, forecast_data, target_date):
    """打印格式化的控制台日报。"""
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    wd_name = weekday_names[target_date.weekday()]

    print("\n" + "=" * 50)
    print("明日资源需求日报")
    print("=" * 50)
    print(f"\n日期：{target_date} ({wd_name})")

    # ---- 真实预约 ----
    print(f"\n【真实预约】")
    print(f"\n放射：")
    print(f"MRI：{real_data['MRI_count']}")
    print(f"CT：{real_data['CT_count']}")
    print(f"X-ray：{real_data['Xray_count']}")
    print(f"钼靶：{real_data['Mammo_count']}")
    print(f"骨密度：{real_data['BoneDensity_count']}")
    print(f"\n放射技师工作量：{real_data['rad_tech_minutes']:.0f} min")
    print(f"放射医生工作量：{real_data['rad_doc_minutes']:.0f} min")

    print(f"\n超声：")
    print(f"B超：{real_data['US_count']}")
    print(f"心彩：{real_data['Echo_count']}")
    print(f"\n超声扫描工作量：{real_data['us_scan_minutes']:.0f} min")
    print(f"超声医生工作量：{real_data['us_doc_minutes']:.0f} min")

    # ---- 预测需求 ----
    print(f"\n" + "-" * 50)
    print(f"\n【预测需求】")
    print(f"\n放射：")
    print(f"技师工作量：{forecast_data['rad_pred_tech_minutes']:.0f} min")
    print(f"医生工作量：{forecast_data['rad_pred_doc_minutes']:.0f} min")
    print(f"\n超声：")
    print(f"技师工作量：{forecast_data['us_pred_tech_minutes']:.0f} min")
    print(f"医生工作量：{forecast_data['us_pred_doc_minutes']:.0f} min")

    # ---- 输出文件 ----
    print(f"\n" + "-" * 50)
    print(f"\n已输出：\n")
    print(f"Real_Daily_Summary.csv")
    print(f"Forecast_Daily_Summary.csv")
    print("=" * 50)


def _generate_daily_report(target_date=None):
    """生成明日资源需求日报。

    参数:
        target_date: datetime.date 或 None（默认为明天）
    """
    if target_date is None:
        target_date = date.today() + timedelta(days=1)

    print("\n" + "=" * 50)
    print("生成明日资源需求日报")
    print(f"目标日期: {target_date}")
    print("=" * 50)

    # ---- 飞书凭据 ----
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id:
        app_id = "cli_aaa8d24639b8dcd8"
    if not app_secret:
        app_secret = "b0ayVQKIuUGmvzRu9YCm9gpZHUzniNz1"

    # ---- 导入飞书模块 ----
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    # ---- 1. 获取 Token & 拉取真实预约数据 ----
    print(f"\n[1/4] 获取飞书访问令牌 & 拉取真实预约数据 (Base={NEW_BASE_TOKEN})...")
    real_data = {
        "date": target_date,
        "MRI_count": 0, "CT_count": 0, "Xray_count": 0,
        "Mammo_count": 0, "BoneDensity_count": 0,
        "US_count": 0, "Echo_count": 0,
        "rad_tech_minutes": 0.0, "rad_doc_minutes": 0.0,
        "us_scan_minutes": 0.0, "us_doc_minutes": 0.0,
    }

    try:
        import fetch_feishu_data as fdf
        token = fdf.get_tenant_access_token(app_id, app_secret)
        table_id = _discover_table_id(token, NEW_BASE_TOKEN)
        df_real = _fetch_real_data(token, NEW_BASE_TOKEN, table_id, target_date)
        real_data = _compute_real_workload(df_real)
    except Exception as e:
        print(f"[WARN] 真实数据获取失败: {e}")
        print("       将使用零值作为真实数据")
        real_data["date"] = target_date

    # ---- 2. 读取预测数据 ----
    print(f"\n[2/4] 读取预测数据...")
    forecast_csv = os.path.join(
        script_dir, "pipeline_output", "Demand_Forecast_Hourly.csv"
    )
    forecast_data = {
        "date": target_date,
        "rad_pred_tech_minutes": 0.0,
        "rad_pred_doc_minutes": 0.0,
        "us_pred_tech_minutes": 0.0,
        "us_pred_doc_minutes": 0.0,
    }

    try:
        df_forecast = _read_forecast_data(forecast_csv, target_date)
        forecast_data = _compute_forecast_workload(df_forecast)
    except Exception as e:
        print(f"[WARN] 预测数据读取失败: {e}")
        print("       将使用零值作为预测数据")

    # ---- 3. 保存输出文件 ----
    print(f"\n[3/4] 保存输出文件...")
    output_dir = os.path.join(script_dir, "pipeline_output")
    os.makedirs(output_dir, exist_ok=True)

    _save_real_summary_csv(real_data, output_dir)
    _save_forecast_summary_csv(forecast_data, output_dir)

    # ---- 控制台报告 ----
    _print_console_report(real_data, forecast_data, target_date)


# =====================================================
# MAIN
# =====================================================

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", type=str, default=None, help="Target month (YYYY-MM)")
    args, _ = parser.parse_known_args()  # ignore unknown args from run_pipeline

    print("=" * 60)
    print("MEDICAL DEMAND FORECAST SYSTEM")
    print("=" * 60)

    base_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    csv_path = os.path.join(
        base_dir,
        "pipeline_output",
        "cleaned_output.csv",
    )

    df = pd.read_csv(
        csv_path,
        encoding="utf-8-sig",
    )

    # 列名映射：中文列名 → 脚本内部使用的英文名
    df.rename(columns={
        "患者到达时间": "arrived_datetime",
        "就诊科室": "eps_dept_desc",
        "医嘱描述": "order_item_desc",
    }, inplace=True)

    df = df[
        df["Type"]
        .isin(VALID_TYPES)
    ].copy()

    # 飞书毫秒时间戳是标准 Unix epoch (UTC)，转为北京时间
    df["arrived_datetime"] = (
        pd.to_datetime(
            df["arrived_datetime"],
            unit="ms",
            errors="coerce",
        )
        .dt.tz_localize("UTC")
        .dt.tz_convert(TIMEZONE)
        .dt.tz_localize(None)
    )

    df = df.dropna(
        subset=["arrived_datetime"]
    )

    df["ds"] = (
        df["arrived_datetime"]
        .dt.floor("h")
    )

    df = add_time_features(df)

    df["大分类"] = np.where(
        df["Type"] == "Ultrasound",
        "超声",
        "放射",
    )

    # ---- 确定预测天数 + 目标月过滤 ----
    last_data_date = df["ds"].max()
    forecast_start = None
    if args.month:
        parts = args.month.split("-")
        target_year, target_month = int(parts[0]), int(parts[1])
        import calendar
        last_day = calendar.monthrange(target_year, target_month)[1]
        target_start = pd.Timestamp(year=target_year, month=target_month, day=1, hour=0)
        target_end   = pd.Timestamp(year=target_year, month=target_month, day=last_day, hour=23)
        # Prophet须从last_data连续预测到target_end，天数 = gap + 目标月
        gap_days = max(0, (target_start - last_data_date).days)
        forecast_days = gap_days + last_day
        forecast_start = target_start
        print(f"[Target] month={args.month}, data_until={last_data_date}")
        print(f"         gap={gap_days}d + target={last_day}d = {forecast_days}d total predict")
        print(f"         output filter: {forecast_start} -> {target_end}")
    else:
        forecast_days = FORECAST_DAYS

    # ---- Run forecast pipeline (imported from forecast_core) ----
    forecast_only_df = run_forecast_pipeline(df, forecast_days=forecast_days,
                                              forecast_start=forecast_start)
    # =================================================
    # OUTPUT
    # =================================================

    output_dir = os.path.join(
        base_dir,
        "pipeline_output",
    )

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    # Hourly CSV
    hourly_path = os.path.join(
        output_dir,
        "Demand_Forecast_Hourly.csv",
    )

    forecast_only_df.to_csv(
        hourly_path,
        index=False,
        encoding="utf-8-sig",
    )

    # Daily CSV
    daily_df = (
        forecast_only_df.groupby(
            [
                forecast_only_df["ds"].dt.date,
                "大分类",
            ]
        )[
            [
                "pred_tech_minutes",
                "pred_doc_minutes",
            ]
        ]
        .sum()
        .reset_index()
    )

    daily_path = os.path.join(
        output_dir,
        "Demand_Forecast_Daily.csv",
    )

    daily_df.to_csv(
        daily_path,
        index=False,
        encoding="utf-8-sig",
    )

    # =================================================
    # VISUALIZATION
    # =================================================

    for category in [
        "超声",
        "放射",
    ]:

        sub = forecast_only_df[
            forecast_only_df["大分类"]
            == category
        ]

        # Heatmap
        plot_heatmap(
            sub,
            "pred_tech_minutes",
            f"{category}_Tech_Heatmap",
            os.path.join(
                output_dir,
                f"{category}_Tech_Heatmap.png",
            ),
        )

        plot_heatmap(
            sub,
            "pred_doc_minutes",
            f"{category}_Doc_Heatmap",
            os.path.join(
                output_dir,
                f"{category}_Doc_Heatmap.png",
            ),
        )

        # Trend
        plot_daily_trend(
            sub,
            "pred_tech_minutes",
            f"{category}_Tech_Trend",
            os.path.join(
                output_dir,
                f"{category}_Tech_Trend.png",
            ),
        )

        plot_daily_trend(
            sub,
            "pred_doc_minutes",
            f"{category}_Doc_Trend",
            os.path.join(
                output_dir,
                f"{category}_Doc_Trend.png",
            ),
        )

    print("\nForecast Completed.")

    print("\nOutput Folder:")
    print(output_dir)


    # =================================================
    # COPY MONTHLY FORECAST TO publish/ FOR CI USE
    # =================================================
    publish_dir = os.path.join(base_dir, "publish")
    os.makedirs(publish_dir, exist_ok=True)
    monthly_csv_dst = os.path.join(publish_dir, "monthly_forecast_hourly.csv")
    shutil.copy2(hourly_path, monthly_csv_dst)
    print(f"\n[OK] Copied monthly forecast to {monthly_csv_dst}")
    # ----------------------------------------------------------------
    # 明日资源需求日报
    # ----------------------------------------------------------------
    try:
        _generate_daily_report()
    except Exception as e:
        print(f"\n[WARN] 日报生成失败: {e}")

# =====================================================
# ENTRY
# =====================================================

if __name__ == "__main__":

    main()