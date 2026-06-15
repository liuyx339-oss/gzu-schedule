"""
=========================================================
Forecast Core — 从 prophet_lightGBM.py 提取的可复用预测函数
供月度流水线 (prophet_lightGBM.py) 和每周预测 (weekly_forecast.py) 共用
=========================================================
"""

import numpy as np
import pandas as pd

from prophet import Prophet
from lightgbm import LGBMRegressor

# =====================================================
# RANDOM SEEDS — 固定随机性，确保每次运行结果一致
# =====================================================

SEED = 42
np.random.seed(SEED)

# =====================================================
# CONFIG
# =====================================================

FORECAST_DAYS = 30  # 月度默认30天；每周预测通过参数覆盖

TIMEZONE = "Asia/Shanghai"

VALID_TYPES = [
    "CT",
    "MRI",
    "X - ray",
    "Ultrasound",
    "DXA",
    "Mammogram",
    "Echocardiograms",
    "骨龄测评",
    "床边、术中、穿刺、消融",
]

US_KEY_DEPTS = [
    "GZU OBGYN",
    "GZU Family Medicine",
    "GZU Health Management Center",
    "GZU Health Management Center 体检日",
    "GZU Staff Annual Checkup Clinic",
    "GZU Internal Medicine",
]

RAD_KEY_DEPTS = [
    "GZU Orthopedics",
    "GZU Family Medicine",
    "GZU Health Management Center 体检日",
    "GZU Health Management Center",
    "GZU Staff Annual Checkup Clinic",
]

# =====================================================
# FEATURE ENGINEERING
# =====================================================


def add_time_features(df):
    """为 hourly DataFrame 添加时间特征列。"""
    df = df.copy()

    df["hour"] = df["ds"].dt.hour
    df["weekday"] = df["ds"].dt.dayofweek
    df["month"] = df["ds"].dt.month

    df["is_weekend"] = (
        df["weekday"]
        .isin([5, 6])
        .astype(int)
    )

    df["hour_sin"] = np.sin(
        2 * np.pi * df["hour"] / 24
    )

    df["hour_cos"] = np.cos(
        2 * np.pi * df["hour"] / 24
    )

    return df


# =====================================================
# PROPHET MODEL
# =====================================================


def build_prophet():
    """构建 Prophet 模型（中文节假日 + 时间 regressor）。"""
    model = Prophet(
        growth="linear",
        daily_seasonality=True,
        weekly_seasonality=True,
        yearly_seasonality=False,
        changepoint_prior_scale=0.05,
        seasonality_prior_scale=10,
    )

    model.add_country_holidays(
        country_name="CN"
    )

    model.add_regressor("is_weekend")
    model.add_regressor("hour_sin")
    model.add_regressor("hour_cos")

    return model


# =====================================================
# HYBRID FORECAST (Prophet 60% + LightGBM 40%)
# =====================================================


def hybrid_forecast(hourly_df, forecast_days=FORECAST_DAYS):
    """
    Prophet + LightGBM 混合预测。

    参数:
        hourly_df: DataFrame with columns [ds, cases]
        forecast_days: 预测未来多少天

    返回:
        DataFrame with columns [ds, pred_cases, hour, weekday]
    """
    full_range = pd.date_range(
        start=hourly_df["ds"].min(),
        end=hourly_df["ds"].max(),
        freq="h",
    )

    hourly_df = (
        hourly_df
        .set_index("ds")
        .reindex(full_range)
        .fillna(0)
        .reset_index()
    )

    hourly_df.columns = [
        "ds",
        "cases",
    ]

    hourly_df = add_time_features(
        hourly_df
    )

    # Prophet
    prophet_train = hourly_df[
        [
            "ds",
            "cases",
            "is_weekend",
            "hour_sin",
            "hour_cos",
        ]
    ].rename(
        columns={
            "cases": "y"
        }
    )

    prophet = build_prophet()

    prophet.fit(prophet_train, seed=SEED)

    future_dates = pd.date_range(
        start=hourly_df["ds"].min(),
        periods=(
            len(hourly_df)
            + forecast_days * 24
        ),
        freq="h",
    )

    future_df = pd.DataFrame({
        "ds": future_dates
    })

    future_df = add_time_features(
        future_df
    )

    prophet_pred = prophet.predict(
        future_df[
            [
                "ds",
                "is_weekend",
                "hour_sin",
                "hour_cos",
            ]
        ]
    )

    future_df["prophet_pred"] = (
        prophet_pred["yhat"]
        .clip(lower=0)
    )

    # LightGBM
    train_lgb = hourly_df.copy()

    train_lgb["lag_1"] = (
        train_lgb["cases"].shift(1)
    )

    train_lgb["lag_24"] = (
        train_lgb["cases"].shift(24)
    )

    train_lgb["rolling_6h"] = (
        train_lgb["cases"]
        .rolling(6)
        .mean()
    )

    train_lgb = train_lgb.dropna()

    features = [
        "hour",
        "weekday",
        "month",
        "is_weekend",
        "hour_sin",
        "hour_cos",
        "lag_1",
        "lag_24",
        "rolling_6h",
    ]

    X_train = train_lgb[features]
    y_train = train_lgb["cases"]

    lgb = LGBMRegressor(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=6,
        min_child_samples=20,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=SEED,
        verbose=-1,
    )

    lgb.fit(
        X_train,
        y_train,
    )

    future_lgb = future_df.copy()

    future_lgb["lag_1"] = (
        hourly_df["cases"].iloc[-1]
    )

    future_lgb["lag_24"] = (
        hourly_df["cases"]
        .iloc[-24:]
        .mean()
    )

    future_lgb["rolling_6h"] = (
        hourly_df["cases"]
        .iloc[-6:]
        .mean()
    )

    lgb_pred = lgb.predict(
        future_lgb[features]
    )

    future_lgb["lgb_pred"] = (
        np.clip(lgb_pred, 0, None)
    )

    # Hybrid
    future_lgb["pred_cases"] = (
        future_lgb["prophet_pred"] * 0.6
        + future_lgb["lgb_pred"] * 0.4
    )

    result = future_lgb[
        [
            "ds",
            "pred_cases",
            "hour",
            "weekday",
        ]
    ]

    result = result[
        result["ds"]
        > hourly_df["ds"].max()
    ]

    return result


# =====================================================
# TYPE FORECAST
# =====================================================


def forecast_total_type(df, forecast_days=FORECAST_DAYS):
    """按 Type 分别预测总需求量。"""
    results = []

    for type_name in (
        df["Type"]
        .dropna()
        .unique()
    ):

        sub = df[
            df["Type"] == type_name
        ]

        hourly = (
            sub.groupby("ds")
            .size()
            .reset_index(name="cases")
        )

        if len(hourly) < 48:
            continue

        pred = hybrid_forecast(hourly, forecast_days=forecast_days)

        pred["Type"] = type_name

        results.append(pred)

    return pd.concat(
        results,
        ignore_index=True,
    )


# =====================================================
# KEY DEPARTMENT FORECAST
# =====================================================


def forecast_key_departments(
    df,
    category,
    forecast_days=FORECAST_DAYS,
):
    """按关键科室细分预测。"""
    results = []

    key_depts = (
        US_KEY_DEPTS
        if category == "超声"
        else RAD_KEY_DEPTS
    )

    for dept in key_depts:

        dept_df = df[
            df["eps_dept_desc"]
            == dept
        ]

        if dept_df.empty:
            continue

        for type_name in (
            dept_df["Type"]
            .dropna()
            .unique()
        ):

            sub = dept_df[
                dept_df["Type"]
                == type_name
            ]

            hourly = (
                sub.groupby("ds")
                .size()
                .reset_index(name="cases")
            )

            if len(hourly) < 48:
                continue

            pred = hybrid_forecast(hourly, forecast_days=forecast_days)

            pred["eps_dept_desc"] = dept
            pred["Type"] = type_name

            results.append(pred)

    return pd.concat(
        results,
        ignore_index=True,
    )


# =====================================================
# REMAINING POOL
# =====================================================


def scale_key_to_total(total_forecast, key_forecast):
    """将关键科室预测等比缩放到不超过 Type 总量。

    如果某 (ds, Type) 的关键科室之和 > Type 总量，等比缩放使之和 = 总量。
    如果关键科室之和 ≤ 总量，不做处理，剩余分配给非关键科室。
    """
    key_sum = (
        key_forecast
        .groupby(["ds", "Type"])["pred_cases"]
        .sum()
        .reset_index(name="key_total")
    )
    merged = pd.merge(
        total_forecast[["ds", "Type", "pred_cases"]].rename(columns={"pred_cases": "type_total"}),
        key_sum,
        on=["ds", "Type"],
        how="left",
    )
    merged["key_total"] = merged["key_total"].fillna(0)

    overflow = merged[merged["key_total"] > merged["type_total"]].copy()
    if len(overflow) > 0:
        overflow["scale"] = overflow["type_total"] / overflow["key_total"]
        scaled = key_forecast.merge(
            overflow[["ds", "Type", "scale"]],
            on=["ds", "Type"],
            how="inner",
        )
        if len(scaled) > 0:
            key_forecast = key_forecast.copy()
            key_forecast.loc[scaled.index, "pred_cases"] *= scaled["scale"].values
        n_dates = overflow["ds"].nunique()
        print(f"  [Scale] {len(overflow)} (ds,Type) overflow across {n_dates} dates "
              f"— key depts scaled to match total")
    return key_forecast


def build_remaining_pool(
    total_forecast,
    key_forecast,
):
    """计算关键科室分配后的剩余需求池。"""
    key_sum = (
        key_forecast
        .groupby(
            ["ds", "Type"]
        )["pred_cases"]
        .sum()
        .reset_index()
    )

    merged = pd.merge(
        total_forecast,
        key_sum,
        on=["ds", "Type"],
        how="left",
        suffixes=(
            "_total",
            "_key",
        )
    )

    merged["pred_cases_key"] = (
        merged["pred_cases_key"]
        .fillna(0)
    )

    merged["remaining_cases"] = (
        merged["pred_cases_total"]
        - merged["pred_cases_key"]
    )

    merged["remaining_cases"] = (
        merged["remaining_cases"]
        .clip(lower=0)
    )

    return merged


# =====================================================
# NON KEY ALLOCATION
# =====================================================


def allocate_remaining_pool(
    df,
    remaining_pool,
    category,
):
    """将剩余需求池按历史比例分配给非关键科室。"""
    key_depts = (
        US_KEY_DEPTS
        if category == "超声"
        else RAD_KEY_DEPTS
    )

    non_key_df = df[
        ~df["eps_dept_desc"]
        .isin(key_depts)
    ].copy()

    dept_mix = (
        non_key_df.groupby(
            [
                "weekday",
                "hour",
                "Type",
                "eps_dept_desc",
            ]
        )
        .size()
        .reset_index(name="cases")
    )

    total = (
        dept_mix.groupby(
            [
                "weekday",
                "hour",
                "Type",
            ]
        )["cases"]
        .sum()
        .reset_index(name="total")
    )

    dept_mix = pd.merge(
        dept_mix,
        total,
        on=[
            "weekday",
            "hour",
            "Type",
        ]
    )

    dept_mix["dept_ratio"] = (
        dept_mix["cases"]
        / dept_mix["total"]
    )

    dept_mix = dept_mix[
        [
            "weekday",
            "hour",
            "Type",
            "eps_dept_desc",
            "dept_ratio",
        ]
    ]

    merged = pd.merge(
        remaining_pool,
        dept_mix,
        on=[
            "weekday",
            "hour",
            "Type",
        ],
        how="left",
    )

    merged["dept_ratio"] = (
        merged["dept_ratio"]
        .fillna(0)
    )

    merged["pred_cases"] = (
        merged["remaining_cases"]
        * merged["dept_ratio"]
    )

    return merged


# =====================================================
# ORDER ITEM SPLIT
# =====================================================


def split_order_items(
    df,
    dept_forecast,
):
    """将科室级预测拆分为具体检查项目。"""
    mix = (
        df.groupby(
            [
                "weekday",
                "hour",
                "eps_dept_desc",
                "Type",
                "order_item_desc",
            ]
        )
        .size()
        .reset_index(name="cases")
    )

    total = (
        mix.groupby(
            [
                "weekday",
                "hour",
                "eps_dept_desc",
                "Type",
            ]
        )["cases"]
        .sum()
        .reset_index(name="total")
    )

    mix = pd.merge(
        mix,
        total,
        on=[
            "weekday",
            "hour",
            "eps_dept_desc",
            "Type",
        ],
    )

    mix["item_ratio"] = (
        mix["cases"]
        / mix["total"]
    )

    mix = mix[
        [
            "weekday",
            "hour",
            "eps_dept_desc",
            "Type",
            "order_item_desc",
            "item_ratio",
        ]
    ]

    merged = pd.merge(
        dept_forecast,
        mix,
        on=[
            "weekday",
            "hour",
            "eps_dept_desc",
            "Type",
        ],
        how="left",
    )

    merged["item_ratio"] = (
        merged["item_ratio"]
        .fillna(0)
    )

    merged["pred_project_cases"] = (
        merged["pred_cases"]
        * merged["item_ratio"]
    )

    return merged


# =====================================================
# STANDARD MINUTES
# =====================================================


def build_standard_minutes(df):
    """从历史数据构建每项检查的标准时长（中位数）。"""
    standard = (
        df.groupby(
            [
                "Type",
                "order_item_desc",
            ]
        )[
            [
                "预估操作时长",
                "预估医生写报告时长",
            ]
        ]
        .median()
        .reset_index()
    )

    standard.columns = [
        "Type",
        "order_item_desc",
        "tech_minutes",
        "doc_minutes",
    ]

    return standard


# =====================================================
# WORKLOAD TRANSLATION
# =====================================================


def translate_workload(
    project_df,
    standard_df,
):
    """将项目数量转换为技师/医生工作分钟数。"""
    merged = pd.merge(
        project_df,
        standard_df,
        on=[
            "Type",
            "order_item_desc",
        ],
        how="left",
    )

    merged["pred_tech_minutes"] = (
        merged["pred_project_cases"]
        * merged["tech_minutes"]
    )

    merged["pred_doc_minutes"] = (
        merged["pred_project_cases"]
        * merged["doc_minutes"]
    )

    return merged


# =====================================================
# FULL PIPELINE RUNNER
# =====================================================


def run_forecast_pipeline(df, forecast_days=FORECAST_DAYS):
    """
    运行完整的分层预测流水线，返回 workload DataFrame。

    输入 df 必须包含列:
        ds (datetime), hour, weekday,
        eps_dept_desc, Type, order_item_desc,
        大分类, 预估操作时长, 预估医生写报告时长

    返回:
        forecast_only_df with columns:
        ds, pred_cases, hour, weekday, eps_dept_desc, Type,
        pred_cases_total, pred_cases_key, remaining_cases, dept_ratio,
        order_item_desc, item_ratio, pred_project_cases,
        tech_minutes, doc_minutes, pred_tech_minutes, pred_doc_minutes, 大分类
    """
    standard_df = build_standard_minutes(df)

    all_results = []

    for category in ["超声", "放射"]:

        print(f"\nForecasting {category} (forecast_days={forecast_days})")

        category_df = df[
            df["大分类"] == category
        ].copy()

        # STEP 1: Type Total Forecast
        total_forecast = forecast_total_type(
            category_df, forecast_days=forecast_days
        )

        # STEP 2: Key Department Forecast
        key_forecast = forecast_key_departments(
            category_df, category, forecast_days=forecast_days
        )

        # STEP 2.5: Scale key departments to total Type forecast
        key_forecast = scale_key_to_total(total_forecast, key_forecast)

        # STEP 3: Remaining Pool
        remaining_pool = build_remaining_pool(
            total_forecast, key_forecast
        )

        # STEP 4: Non-key Allocation
        non_key_forecast = allocate_remaining_pool(
            category_df, remaining_pool, category
        )

        # Combine key + non-key
        combined = pd.concat(
            [key_forecast, non_key_forecast],
            ignore_index=True,
        )

        # STEP 5: Order Item Split
        project_forecast = split_order_items(
            category_df, combined
        )

        # STEP 6: Workload Translation
        workload = translate_workload(
            project_forecast, standard_df
        )

        workload["大分类"] = category

        all_results.append(workload)

    final_df = pd.concat(
        all_results,
        ignore_index=True,
    )

    # Only keep forecast period (after last historical data point)
    forecast_start = df["ds"].max()
    forecast_only_df = final_df[
        final_df["ds"] > forecast_start
    ].copy()

    return forecast_only_df
