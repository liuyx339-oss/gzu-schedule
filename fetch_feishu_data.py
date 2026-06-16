#!/usr/bin/env python3
"""
飞书多维表格数据提取与清洗脚本
=============================================
从两个飞书多维表格拉取数据，按业务逻辑清洗后输出 UTF-8 BOM CSV。

数据源:
  文档A（主数据表）: Base=NjSdbaToNavBlksS6AecPd7rnrb, Table=tbl9camXrcKz4qhZ
  文档B（映射表）:   Base=DbRZbYJblam4i1sNSQScx3r3nab, Table=tbl5RNcBp66q3zlc

使用前准备:
  1. 在飞书开放平台 (https://open.feishu.cn) 创建一个企业自建应用
  2. 获取 App ID 和 App Secret
  3. 在应用权限中开启"多维表格"相关权限 (bitable:app)
  4. 将应用添加为两个多维表格文档的协作者（或确保应用有权限访问）

运行方式:
  python fetch_feishu_data.py

环境变量（必须）:
  FEISHU_APP_ID      - 飞书应用 App ID
  FEISHU_APP_SECRET  - 飞书应用 App Secret

也可以通过命令行参数设置:
  --app-id APP_ID        飞书应用 App ID
  --app-secret APP_SECRET  飞书应用 App Secret
"""

from __future__ import annotations

import argparse
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import json
import os
import sys
from datetime import datetime

# ============================================================================
# CONFIG — 在此填写凭据（或通过环境变量设置）
# ============================================================================
APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aaa8d24639b8dcd8")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "b0ayVQKIuUGmvzRu9YCm9gpZHUzniNz1")

# 文档 A（主数据表）
BASE_A = "NjSdbaToNavBlksS6AecPd7rnrb"
TABLE_A = "tbl9camXrcKz4qhZ"

# 文档 B（映射表）
BASE_B = "DbRZbYJblam4i1sNSQScx3r3nab"
TABLE_B = "tbl5RNcBp66q3zlc"

# 输出文件
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_output", "cleaned_output.csv")

# 飞书 API 基础地址
FEISHU_API = "https://open.feishu.cn/open-apis"

# 每页拉取记录数（飞书上限 500）
PAGE_SIZE = 500

# 重试配置
RETRY_COUNT = 5  # 最多重试 5 次
RETRY_BACKOFF = 3  # 重试间隔倍数（3s, 6s, 12s, 24s, 48s）

# 代理配置: True=使用系统代理（如 Clash/V2Ray）, False=直连（飞书国内服务建议直连）
USE_PROXY = False


def _create_session() -> requests.Session:
    """创建一个带自动重试的 requests Session"""
    retry_strategy = Retry(
        total=RETRY_COUNT,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    # 飞书国内服务，走代理反而可能失败，默认直连
    session.trust_env = USE_PROXY
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# ============================================================================
# 诊断工具 — API 响应时间戳 & 数据预览
# ============================================================================

def _extract_api_server_time(resp: requests.Response) -> str:
    """从飞书 API 响应头中提取服务器时间，用于判断是否拿到了缓存数据。
    飞书响应头通常包含 'Date'（HTTP 标准），部分接口还返回 'X-Tt-Logid'（飞书内部 trace-id）。
    """
    parts = []
    server_date = resp.headers.get("Date", "")
    if server_date:
        parts.append(f"Server-Time: {server_date}")
    log_id = resp.headers.get("X-Tt-Logid", "")
    if log_id:
        parts.append(f"LogId: {log_id}")
    return " | ".join(parts) if parts else "(未获取到服务器时间戳)"


def _preview_records(label: str, records: list[dict], field_map: dict, n: int = 3) -> None:
    """打印前 n 条记录的关键字段值，便于人工对比飞书界面上的最新数据。"""
    if not records:
        print(f"  [{label}] [WARN] 记录集为空，无法预览")
        return

    # 取字段名列表（取前8个，防止表格过宽）
    field_names = [meta.get("name", fid) for fid, meta in field_map.items()]
    display_fields = field_names[:8]

    print(f"  [{label}] 数据预览 (前 {min(n, len(records))} 条, 共 {len(records)} 条):")
    for i, rec in enumerate(records[:n]):
        fields = rec.get("fields", {})
        preview = {}
        for fid, fvalue in fields.items():
            name = field_map.get(fid, {}).get("name", fid)
            if name in display_fields:
                # 截断过长的值
                val_str = str(fvalue)
                if len(val_str) > 60:
                    val_str = val_str[:60] + "..."
                preview[name] = val_str
        print(f"    记录#{i+1}: {preview}")
    print()


# ============================================================================
# 1. 认证 — 获取 tenant_access_token
# ============================================================================
def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    """获取飞书 tenant_access_token，有效期约 2 小时"""
    url = f"{FEISHU_API}/auth/v3/tenant_access_token/internal"
    session = _create_session()
    resp = session.post(
        url,
        json={"app_id": app_id, "app_secret": app_secret},
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=(10, 60),
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: code={data.get('code')}, msg={data.get('msg')}")
    token = data["tenant_access_token"]
    print(f"[OK] 已获取 tenant_access_token (有效期 {data.get('expire', '?')}s)")
    return token


# ============================================================================
# 2. 获取字段元数据（field_id → field_name 映射）
# ============================================================================
def get_field_meta(token: str, base_token: str, table_id: str) -> dict:
    """
    返回 {field_id: field_name} 映射字典。
    飞书 API 用 field_id 作为键，我们需要反向映射到中文名。
    """
    url = f"{FEISHU_API}/bitable/v1/apps/{base_token}/tables/{table_id}/fields"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    params = {"page_size": 100}
    field_map = {}

    session = _create_session()
    while True:
        resp = session.get(url, headers=headers, params=params, timeout=(10, 60))
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取字段元数据失败: code={data.get('code')}, msg={data.get('msg')}")
        # 诊断: 打印API服务器时间
        server_time = _extract_api_server_time(resp)
        print(f"  [字段元数据] {server_time}")

        for item in data.get("data", {}).get("items", []):
            fid = item.get("field_id", "")
            fname = item.get("field_name", "")
            ftype_raw = item.get("type", "")
            # 归一化: 飞书 API 可能返回整数或字符串类型名
            ftype = normalize_field_type(ftype_raw)
            if fid:
                field_map[fid] = {"name": fname, "type": ftype}

        if data.get("data", {}).get("has_more", False):
            params["page_token"] = data["data"]["page_token"]
        else:
            break

    return field_map


# ============================================================================
# 3. 获取表的所有记录
# ============================================================================
def get_all_records(token: str, base_token: str, table_id: str, max_records: int | None = None) -> list[dict]:
    """拉取指定多维表格的全部记录，返回原始 dict 列表。
    max_records: 调试用，超过此数量后停止分页（None=全量拉取）。"""
    url = f"{FEISHU_API}/bitable/v1/apps/{base_token}/tables/{table_id}/records"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    params = {"page_size": PAGE_SIZE}
    all_records = []
    page = 0
    session = _create_session()

    while True:
        resp = session.get(url, headers=headers, params=params, timeout=(10, 120))
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取记录失败: code={data.get('code')}, msg={data.get('msg')}")

        items = data.get("data", {}).get("items", [])
        all_records.extend(items)
        page += 1
        total = data.get("data", {}).get("total", "?")

        # --sample: 达到上限后提前停止
        if max_records and len(all_records) >= max_records:
            all_records = all_records[:max_records]
            print(f"  [--sample] 已截断至 {len(all_records)} 条，跳过剩余页")
            break

        # 诊断: 首页打印API服务器时间
        if page == 1:
            server_time = _extract_api_server_time(resp)
            print(f"  [记录数据] {server_time}")
        print(f"  第 {page} 页: 获取 {len(items)} 条, 累计 {len(all_records)} / {total}")

        if data.get("data", {}).get("has_more", False):
            params["page_token"] = data["data"]["page_token"]
        else:
            break

    return all_records


# ============================================================================
# 4. 将飞书原始记录转换为 pandas DataFrame
# ============================================================================
def records_to_dataframe(records: list[dict], field_map: dict) -> pd.DataFrame:
    """
    将飞书 API 返回的 records 转成 DataFrame。
    列名使用字段的中文名（field_name），值从各字段类型中提取纯文本。
    """
    rows = []
    for rec in records:
        row = {}
        fields = rec.get("fields", {})
        for fid, fvalue in fields.items():
            meta = field_map.get(fid, {})
            col_name = meta.get("name", fid)
            col_type = meta.get("type", "Text")  # 默认按文本处理

            # 根据字段类型提取纯值（同时传入字段名，用于兜底时间戳识别）
            extracted = extract_field_value(fvalue, col_type, field_name=col_name)
            row[col_name] = extracted

        rows.append(row)

    return pd.DataFrame(rows)


# 飞书字段类型映射: 整型 → 字符串名
FEISHU_TYPE_INT_TO_STR = {
    1: "Text",
    2: "Number",
    3: "SingleSelect",
    4: "MultiSelect",
    5: "DateTime",
    7: "Checkbox",
    11: "User",
    13: "Attachment",
    15: "Url",
    17: "LinkRecords",
    19: "GroupChat",
    21: "Phone",
    23: "Location",
    99: "AutoNumber",
}


def normalize_field_type(raw_type) -> str:
    """将飞书字段类型统一转换为字符串名（如 'Text', 'Number'）"""
    if isinstance(raw_type, int):
        return FEISHU_TYPE_INT_TO_STR.get(raw_type, f"Unknown({raw_type})")
    if isinstance(raw_type, str):
        if raw_type.isdigit():
            return FEISHU_TYPE_INT_TO_STR.get(int(raw_type), f"Unknown({raw_type})")
        return raw_type
    return str(raw_type)


# 时间相关列名关键词（用于兜底识别：字段类型非 DateTime 但值是大整数时间戳）
TIME_KEYWORDS = ["时间", "日期", "date", "time", "timestamp"]


def _looks_like_timestamp(value) -> bool:
    """判断一个数值是否看起来像毫秒级 Unix 时间戳。
    范围：2000-01-01 ~ 2099-12-31 的毫秒值 (946684800000 ~ 4102444800000)"""
    if isinstance(value, (int, float)) and 946684800000 <= value <= 4102444800000:
        return True
    if isinstance(value, str):
        try:
            v = int(value)
            return 946684800000 <= v <= 4102444800000
        except (ValueError, OverflowError):
            return False
    return False


def _format_timestamp(ts_value) -> str:
    """将毫秒时间戳转为 YYYY-MM-DD HH:MM:SS 格式"""
    try:
        if isinstance(ts_value, str):
            ts_value = int(ts_value)
        return datetime.fromtimestamp(ts_value / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return str(ts_value)


def extract_field_value(fvalue, col_type: str, field_name: str = "") -> str:
    """
    从飞书多维表格的字段值中提取纯文本表示。
    col_type 为字符串类型名（如 'Text', 'Number', 'SingleSelect' 等）。
    field_name 为字段中文名，用于兜底识别时间戳列。
    """
    if fvalue is None:
        return ""

    # ---- 文本 ----
    if col_type == "Text":
        if isinstance(fvalue, list):
            return "\n".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in fvalue
            )
        return str(fvalue)

    # ---- 数字 ----
    if col_type == "Number":
        # 兜底：字段名含时间关键词且值看起来像毫秒时间戳，自动转换
        if field_name and any(kw in field_name for kw in TIME_KEYWORDS):
            if _looks_like_timestamp(fvalue):
                return _format_timestamp(fvalue)
        return str(fvalue)

    # ---- 单选 ----
    if col_type == "SingleSelect":
        if isinstance(fvalue, dict):
            return fvalue.get("text", "")
        return str(fvalue)

    # ---- 多选 ----
    if col_type == "MultiSelect":
        if isinstance(fvalue, list):
            return ", ".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in fvalue
            )
        return str(fvalue)

    # ---- 日期时间 ----
    if col_type == "DateTime":
        # 飞书 API 返回 Unix 时间戳（毫秒）
        if isinstance(fvalue, (int, float)):
            try:
                return datetime.fromtimestamp(fvalue / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except (OSError, ValueError):
                return str(fvalue)
        # 部分情况下也可能是数字字符串（毫秒时间戳）
        if isinstance(fvalue, str):
            if _looks_like_timestamp(fvalue):
                return _format_timestamp(fvalue)
            return fvalue
        return str(fvalue)

    # ---- 复选框 ----
    if col_type == "Checkbox":
        return "是" if fvalue else "否"

    # ---- 人员 ----
    if col_type == "User":
        if isinstance(fvalue, list):
            return ", ".join(
                item.get("name", "") if isinstance(item, dict) else str(item)
                for item in fvalue
            )
        return str(fvalue)

    # ---- 附件 ----
    if col_type == "Attachment":
        if isinstance(fvalue, list):
            return ", ".join(
                item.get("name", "") if isinstance(item, dict) else str(item)
                for item in fvalue
            )
        return str(fvalue)

    # ---- 超链接 ----
    if col_type == "Url":
        if isinstance(fvalue, dict):
            return fvalue.get("link", fvalue.get("text", ""))
        if isinstance(fvalue, list):
            return ", ".join(
                item.get("link", "") if isinstance(item, dict) else str(item)
                for item in fvalue
            )
        return str(fvalue)

    # ---- 关联 ----
    if col_type == "LinkRecords":
        if isinstance(fvalue, list):
            return ", ".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in fvalue
            )
        return str(fvalue)

    # ---- 电话号码 ----
    if col_type == "Phone":
        return str(fvalue)

    # ---- 地理位置 ----
    if col_type == "Location":
        if isinstance(fvalue, dict):
            return fvalue.get("name", fvalue.get("address", json.dumps(fvalue, ensure_ascii=False)))
        return str(fvalue)

    # ---- 自动编号 ----
    if col_type == "AutoNumber":
        return str(fvalue)

    # ---- 兜底 ----
    # 对未知类型也尝试时间戳检测
    if field_name and any(kw in field_name for kw in TIME_KEYWORDS):
        if _looks_like_timestamp(fvalue):
            return _format_timestamp(fvalue)
    if isinstance(fvalue, (dict, list)):
        return json.dumps(fvalue, ensure_ascii=False)
    return str(fvalue)


# ============================================================================
# 5. 数据处理主逻辑
# ============================================================================
def process_data(df_main: pd.DataFrame, df_mapping: pd.DataFrame) -> pd.DataFrame:
    """
    按需求清洗数据:
      1. 从主数据提取指定列（按存在的提取，支持模糊匹配）
      2. 以 医嘱描述 为键 left join 映射表
      3. 对 就诊科室 做条件修改（GZU Health Management Center + 体检日）
      4. 拼接生成 阵营 列
    """

    # ------------------------------------------------------------------
    # 辅助: 模糊匹配列名
    # ------------------------------------------------------------------
    def _fuzzy_find_column(target: str, available: list[str]) -> str | None:
        """在 available 中查找与 target 最匹配的列名。
        优先级: 精确匹配 > 关键词交集 > 返回 None
        """
        if target in available:
            return target
        # 将目标拆成关键词 (2-gram 以上)
        keywords = [target[i:i+2] for i in range(len(target)-1)]
        best, best_score = None, 0
        for col in available:
            score = sum(1 for kw in keywords if kw in col)
            if score > best_score:
                best_score = score
                best = col
        return best if best_score >= 2 else None  # 至少两个字符词组匹配

    # === Step 1: 提取主数据的目标列（支持模糊匹配） ===
    target_cols = ["就诊科室", "医嘱描述", "开具时间", "执行时间", "患者到达时间", "结果确认时间"]
    available_cols_all = list(df_main.columns)

    # 建立映射: target_name -> actual_col_name (或 None)
    col_mapping: dict[str, str | None] = {}
    for target in target_cols:
        actual = _fuzzy_find_column(target, available_cols_all)
        col_mapping[target] = actual

    found_cols = {t: c for t, c in col_mapping.items() if c is not None}
    missing_cols = [t for t, c in col_mapping.items() if c is None]

    # 精确匹配 & 模糊匹配分别报告
    exact_matches = {t: c for t, c in found_cols.items() if t == c}
    fuzzy_matches = {t: c for t, c in found_cols.items() if t != c}

    if fuzzy_matches:
        print(f"\n[WARN] 以下列名通过模糊匹配找到替代 (文档中的列名可能已被修改):")
        for target, actual in fuzzy_matches.items():
            print(f'    "{target}" → 使用 "{actual}"')

    if missing_cols:
        print(f"\n{'='*60}")
        print(f"[CRITICAL] 严重警告: 主数据表中未找到以下 {len(missing_cols)} 个目标列:")
        for col in missing_cols:
            print(f'    X "{col}"')
        print(f"\n  主数据表当前实际列名 ({len(available_cols_all)} 个):")
        for col in available_cols_all:
            print(f"    · {col}")
        print(f"  请检查飞书文档A中的字段名是否与上述目标列名一致。")
        print(f"{'='*60}")

    print(f"\n[提取] 主数据目标列 ({len(found_cols)}/{len(target_cols)}):")
    for target, actual in found_cols.items():
        flag = " (模糊)" if target != actual else ""
        print(f"  OK {target}{flag} -> {actual}")

    if not found_cols:
        raise RuntimeError("主数据表未找到任何目标列，无法继续。请检查文档A的字段名。")

    # 使用找到的列名构建 DataFrame
    df = df_main[list(found_cols.values())].copy()
    # 将列名统一重命名为目标名 (便于后续代码引用)
    rename_map = {actual: target for target, actual in found_cols.items() if actual != target}
    if rename_map:
        df.rename(columns=rename_map, inplace=True)

    # === Step 2: Left join 映射表 (键 = 医嘱描述) ===
    if "医嘱描述" not in df.columns:
        print("[警告] 主数据中无'医嘱描述'列，无法执行 left join！")
    else:
        join_key = "医嘱描述"
        mapping_key = None

        # 在映射表中找到与 医嘱描述 匹配的列名
        if join_key in df_mapping.columns:
            mapping_key = join_key
        else:
            # 尝试模糊匹配
            for col in df_mapping.columns:
                if "医嘱" in col or "描述" in col or "order" in col.lower():
                    mapping_key = col
                    print(f"[匹配] 映射表中未找到'{join_key}'，使用 '{mapping_key}' 作为关联键")
                    break

        if mapping_key:
            print(f"\n[Join] 以'{join_key}' (主表) <-> '{mapping_key}' (映射表) 执行 LEFT JOIN")

            # ---- 归一化函数: 去除不可见字符差异 ----
            def _normalize(s):
                """去全角/半角空格、不间断空格、换行、制表符，统一 Unicode NFKC"""
                if not isinstance(s, str):
                    return s
                import unicodedata
                import re as _re
                s = unicodedata.normalize("NFKC", s)
                s = s.strip()
                s = _re.sub(r"\s+", " ", s)       # 压缩连续空白为单个空格
                return s

            norm_key = "_join_norm"
            df[norm_key] = df[join_key].apply(_normalize)
            df_mapping[norm_key] = df_mapping[mapping_key].apply(_normalize)

            # 去重映射表（按归一化键保留第一条）
            df_mapping_dedup = df_mapping.drop_duplicates(subset=[norm_key], keep="first")
            print(f"  主表: {len(df)} 行 | 映射表: {len(df_mapping)} 行 (去重后 {len(df_mapping_dedup)} 行)")

            df = df.merge(df_mapping_dedup, left_on=norm_key, right_on=norm_key,
                          how="left", suffixes=("", "_映射"))

            # ---- 统计匹配情况 & 打印未匹配值 ----
            # 映射表独有的列（排除主表已有列和辅助列）
            b_only_cols = [
                c for c in df.columns
                if c not in found_cols and c != norm_key and c != join_key
            ]
            if b_only_cols:
                matched_mask = df[b_only_cols].notna().any(axis=1)
                matched = int(matched_mask.sum())
            else:
                matched = "?"
            print(f"  匹配成功: {matched} 行 / {len(df)} 行")

            if isinstance(matched, int) and matched < len(df):
                unmatched_mask = ~matched_mask
                unmatched_vals = df.loc[unmatched_mask, join_key].unique()
                mapping_norms = set(df_mapping_dedup[norm_key].dropna().unique())

                print(f"\n  [诊断] 未匹配的医嘱描述 ({len(unmatched_vals)} 种值):")
                for i, val in enumerate(sorted(unmatched_vals, key=str)[:20]):
                    if not isinstance(val, str) or not val.strip():
                        print(f"    #{i+1} (空值)")
                        continue
                    val_n = _normalize(val)
                    # 在映射表中查找近似匹配（共同字符比例 > 50%）
                    near = [
                        mv for mv in mapping_norms
                        if isinstance(mv, str) and
                        len(set(val_n) & set(mv)) > max(len(val_n), len(mv)) * 0.5
                    ]
                    if near:
                        print(f'    #{i+1} 主表:  "{val}"')
                        for nv in near[:3]:
                            print(f'        -> 映射表近似: "{nv}"')
                    else:
                        print(f'    #{i+1} 主表:  "{val}"  (映射表中无近似值)')
                if len(unmatched_vals) > 20:
                    print(f"    ... 还有 {len(unmatched_vals) - 20} 种值未显示")

            # ---- 模糊匹配 & 自动归类未匹配行 ----
            if isinstance(matched, int) and matched < len(df):
                import difflib

                # 找出从映射表 merge 过来的列（排除主表已有和辅助列）
                mapping_cols_in_df = [
                    c for c in df.columns
                    if c not in found_cols
                    and c != norm_key
                    and c != join_key
                    and c != mapping_key
                ]

                unmatched_mask = ~matched_mask

                if unmatched_mask.any() and mapping_cols_in_df:
                    # 构建映射表查找字典: {norm_value: {col: value, ...}}
                    mapping_lookup: dict[str, dict] = {}
                    for _, mrow in df_mapping_dedup.iterrows():
                        mv = mrow.get(norm_key)
                        if not isinstance(mv, str) or not mv.strip():
                            continue
                        mapping_lookup[mv] = {c: mrow.get(c) for c in df_mapping_dedup.columns
                                              if c != norm_key}

                    # 去重: 对每种未匹配的医嘱描述只做一次模糊匹配
                    unique_unmatched = df.loc[unmatched_mask, join_key].unique()
                    fuzzy_hits: dict[str, dict] = {}   # {原始值: {col: value}}
                    keyword_hits: dict[str, dict] = {}  # 模糊也没命中，用关键字归类
                    fuzzy_threshold = 0.75

                    for val in unique_unmatched:
                        if not isinstance(val, str) or not val.strip():
                            continue
                        val_n = _normalize(val)

                        # ---- 第 1 层: difflib 模糊匹配 ----
                        best_score, best_key = 0.0, None
                        for mk in mapping_lookup:
                            score = difflib.SequenceMatcher(None, val_n.lower(), mk.lower()).ratio()
                            if score > best_score:
                                best_score = score
                                best_key = mk

                        if best_key and best_score >= fuzzy_threshold:
                            fuzzy_hits[val] = dict(mapping_lookup[best_key])
                            print(f'  [模糊匹配] score={best_score:.2f}  "{val[:60]}"')
                            print(f'           -> "{best_key[:60]}"')
                            continue

                        # ---- 第 2 层: 关键字自动归类 ----
                        val_lower = val_n.lower()
                        category = None

                        # 注意: 先匹配更具体的模式，避免 PET 被 CT 误判、Echo 被 Ultrasound 误判
                        if any(kw in val_lower for kw in [
                            "echocardiogram", "echocardiography", "echo",
                            "transthoracic", "transesophageal", "tte", "tee",
                            "超声心动图", "超声心动",
                        ]):
                            category = "Echocardiograms"
                            defaults = {
                                "Type": "Echocardiograms",
                                "大分类": "超声",
                                "影像医生参与时长": 20,
                                "总时长": 20,
                                "预估医生写报告时长": 10,
                                "预估操作时长": 10,
                            }
                        elif any(kw in val_lower for kw in [
                            "dxa", "dexa", "bone densitometry",
                            "骨密度", "骨密度仪",
                        ]):
                            category = "DXA"
                            defaults = {
                                "Type": "DXA",
                                "大分类": "影像",
                                "影像医生参与时长": 10,
                                "总时长": 25,
                                "预估医生写报告时长": 10,
                                "预估操作时长": 15,
                            }
                        elif any(kw in val_lower for kw in [
                            "pet", "positron emission", "spect",
                            "nuclear medicine", "正电子", "核医学", "核素",
                        ]):
                            category = "Nuclear Medicine"
                            defaults = {
                                "Type": "Nuclear Medicine",
                                "大分类": "影像",
                                "影像医生参与时长": 10,
                                "总时长": 25,
                                "预估医生写报告时长": 10,
                                "预估操作时长": 15,
                            }
                        elif any(kw in val_lower for kw in [
                            "mri", "magnetic resonance", "磁共振", "mr ",
                        ]):
                            category = "MRI"
                            defaults = {
                                "Type": "MRI",
                                "大分类": "影像",
                                "影像医生参与时长": 10,
                                "总时长": 25,
                                "预估医生写报告时长": 10,
                                "预估操作时长": 15,
                            }
                        elif any(kw in val_lower for kw in [
                            "ct ", " ct", "cat scan", "computed tomography",
                            "断层", "ct,", " ct:",
                        ]):
                            category = "CT"
                            defaults = {
                                "Type": "CT",
                                "大分类": "影像",
                                "影像医生参与时长": 10,
                                "总时长": 25,
                                "预估医生写报告时长": 10,
                                "预估操作时长": 15,
                            }
                        elif any(kw in val_lower for kw in [
                            "x - ray", "x-ray", "x ray", "x线", "x光",
                            "fluoroscopy", "mammogram", "mammography",
                            "乳腺", "xr ",
                        ]):
                            # Mammogram 归入 X-ray 大类
                            if any(kw in val_lower for kw in [
                                "mammogram", "mammography", "乳腺",
                            ]):
                                category = "Mammogram"
                            else:
                                category = "X-ray"
                            defaults = {
                                "Type": category,
                                "大分类": "影像",
                                "影像医生参与时长": 10,
                                "总时长": 25,
                                "预估医生写报告时长": 10,
                                "预估操作时长": 15,
                            }
                        elif any(kw in val_lower for kw in [
                            "ultrasound", "ultrasonic", "duplex",
                            "sonography", "sonogram", "超声", "b - scan",
                            "b scan", "b-scan",
                        ]):
                            category = "Ultrasound"
                            defaults = {
                                "Type": "Ultrasound",
                                "大分类": "超声",
                                "影像医生参与时长": 20,
                                "总时长": 20,
                                "预估医生写报告时长": 10,
                                "预估操作时长": 10,
                            }

                        if category:
                            keyword_hits[val] = defaults
                            print(f'  [关键字归类] "{val[:60]}" -> Type={category}')

                    # ---- 辅助: 安全赋值（自动适配列数据类型） ----
                    def _safe_fill(row_mask, col_name, value):
                        """向 DataFrame 列赋值，自动处理字符串列不能接受数字的问题"""
                        if col_name not in df.columns:
                            return False
                        # 如果列是 string dtype 但 value 是数字，转字符串
                        if df[col_name].dtype == "string" and not isinstance(value, str):
                            value = str(value)
                        df.loc[row_mask, col_name] = value
                        return True

                    # ---- 应用模糊匹配结果 ----
                    if fuzzy_hits:
                        for orig_val, fill_vals in fuzzy_hits.items():
                            row_mask = df[join_key] == orig_val
                            for col, col_val in fill_vals.items():
                                _safe_fill(row_mask, col, col_val)
                                _safe_fill(row_mask, col + "_映射", col_val)
                        print(f"\n  模糊匹配兜底: {len(fuzzy_hits)} 种值")

                    # ---- 应用关键字归类结果 ----
                    if keyword_hits:
                        for orig_val, fill_vals in keyword_hits.items():
                            row_mask = df[join_key] == orig_val
                            for col, col_val in fill_vals.items():
                                _safe_fill(row_mask, col, col_val)
                                _safe_fill(row_mask, col + "_映射", col_val)
                        print(f"  关键字归类兜底: {len(keyword_hits)} 种值")

                    # ---- 最终统计 ----
                    if b_only_cols:
                        final_matched = int(df[b_only_cols].notna().any(axis=1).sum())
                        print(f"  最终匹配: {final_matched} 行 / {len(df)} 行 (+{final_matched - int(matched)} 行)")

            # 清理归一化辅助列
            df.drop(columns=[norm_key], inplace=True)
            for c in [c for c in df.columns if c == norm_key]:
                df.drop(columns=[c], inplace=True)

            # 如果两个键列名不同，删除冗余的映射表键列
            if mapping_key != join_key and mapping_key in df.columns:
                df.drop(columns=[mapping_key], inplace=True)
        else:
            print("[警告] 映射表中未找到可匹配的列，跳过 join。映射表列名:", list(df_mapping.columns))

    # === Step 3: 条件修改 就诊科室 ===
    if "就诊科室" in df.columns and "患者到达时间" in df.columns:
        dept_col = "就诊科室"
        arrive_col = "患者到达时间"

        # 解析时间列（兜底毫秒时间戳字符串/数字）
        raw_vals = df[arrive_col]
        # 检测是否为毫秒时间戳格式（纯数字字符串或整数）
        sample_non_null = raw_vals.dropna()
        if len(sample_non_null) > 0:
            first_val = sample_non_null.iloc[0]
            looks_ms = _looks_like_timestamp(first_val)
        else:
            looks_ms = False

        if looks_ms:
            # 飞书 DateTime 字段: 毫秒时间戳（可能是数字字符串）
            numeric_vals = pd.to_numeric(raw_vals, errors="coerce")
            time_series = pd.to_datetime(numeric_vals, unit="ms", errors="coerce")
        else:
            time_series = pd.to_datetime(raw_vals, errors="coerce")

        n_na = time_series.isna().sum()
        if n_na > 0:
            print(f"\n  [诊断-时间解析] '{arrive_col}' 共 {len(time_series)} 行, {n_na} 行解析失败 (NaT), looks_ms={looks_ms}")
            if n_na > 0:
                na_mask = time_series.isna()
                sample_bad = raw_vals[na_mask].dropna().unique()[:3]
                print(f"    无法解析的值示例: {list(sample_bad)}")

        # 判断条件
        has_gzu_hmc = df[dept_col].fillna("").str.contains(
            "GZU Health Management Center", case=False, na=False
        )
        is_checkup_day = time_series.dt.dayofweek.isin([0, 2, 4, 5])  # Mon/Wed/Fri/Sat

        # 诊断: 打印各级过滤统计
        n_gzu = has_gzu_hmc.sum()
        n_day = is_checkup_day.sum() if n_gzu > 0 else 0
        mask = has_gzu_hmc & is_checkup_day
        print(f"\n  [科室修改-诊断] GZU Health Management Center: {n_gzu} 行")
        if n_gzu > 0:
            # 这些 GZU 行的时间解析情况
            gzu_na = time_series[has_gzu_hmc].isna().sum()
            print(f"    其中时间解析失败: {gzu_na} 行")
            gzu_days = time_series[has_gzu_hmc].dt.dayofweek.dropna()
            if len(gzu_days) > 0:
                day_counts = gzu_days.value_counts().sort_index()
                day_names = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}
                day_info = ", ".join(f"{day_names.get(k, k)}={v}" for k, v in day_counts.items())
                print(f"    星期分布: {day_info}")
            print(f"    命中体检日(一/三/五/六): {n_day} 行")
        df.loc[mask, dept_col] = "GZU Health Management Center 体检日"
        print(f"\n[科室修改] 将 {mask.sum()} 行 'GZU Health Management Center' → 'GZU Health Management Center 体检日'")
        print(f"  (条件: 科室含'GZU Health Management Center'且到达时间为周一/三/五/六)")

    # === Step 4: 生成 阵营 列 ===
    type_col = None
    for col in df.columns:
        if col == "Type" or col.lower() == "type":
            type_col = col
            break

    if "就诊科室" in df.columns and type_col:
        df["阵营"] = df["就诊科室"].fillna("").astype(str) + "_" + df[type_col].fillna("").astype(str)
        print(f"\n[阵营] 已生成'阵营'列 (就诊科室 + _ + {type_col})")
        # 统计各阵营记录数
        camp_counts = df["阵营"].value_counts()
        print(f"  共 {len(camp_counts)} 个不同阵营，示例:")
        for camp, cnt in camp_counts.head(10).items():
            print(f"    {camp}: {cnt} 行")
    else:
        print("[警告] 无法生成'阵营'列: 缺少'就诊科室'或'Type'列")

    return df


# ============================================================================
# 5.5 过滤当月数据
# ============================================================================
def filter_out_current_month(df: pd.DataFrame, date_column: str = "开具时间") -> pd.DataFrame:
    """
    过滤掉当前月份的所有数据。

    例如: 今天是 2026-06-02，则移除所有日期为 2026-06 的行，
    确保在当月进行预测时，最近的数据只到上月最后一天 (如 2026-05-31)。

    参数:
        df: 主数据 DataFrame
        date_column: 用于判断月份的日期列名（默认 "开具时间"）
    返回:
        过滤后的 DataFrame
    """
    if date_column not in df.columns:
        print(f"\n[当月过滤] [WARN] 未找到日期列 '{date_column}'，跳过当月数据过滤")
        print(f"  可用列: {list(df.columns)}")
        return df

    today = datetime.now()
    current_year_month = today.strftime("%Y-%m")  # 如 "2026-06"

    # 解析日期列并提取年月
    date_series = pd.to_datetime(df[date_column], errors="coerce")
    year_month_series = date_series.dt.strftime("%Y-%m")

    original_count = len(df)
    mask_current_month = year_month_series == current_year_month
    n_removed = int(mask_current_month.sum())
    n_na = int(date_series.isna().sum())

    if n_removed > 0:
        df = df[~mask_current_month].copy()
        print(f"\n[当月过滤] 已移除 {n_removed} 行当月数据 ({current_year_month})")
        print(f"  过滤前: {original_count} 行 → 过滤后: {len(df)} 行")
    else:
        print(f"\n[当月过滤] 未发现当月数据 ({current_year_month})，无需过滤")

    if n_na > 0:
        print(f"  (注: {n_na} 行日期解析失败/为空，已保留)")

    return df


# ============================================================================
# 6. Main
# ============================================================================
def main():
    # --- 解析命令行参数 ---
    parser = argparse.ArgumentParser(
        description="飞书多维表格数据提取与清洗",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python fetch_feishu_data.py                          # 默认直接拉取
  python fetch_feishu_data.py --wait-refresh 60        # 等待 60 秒再拉取 (飞书API缓存可能有延迟)
  python fetch_feishu_data.py --check-freshness        # 连续拉取两次，对比数据是否一致
  python fetch_feishu_data.py --wait-refresh 30 --check-freshness  # 组合使用
  python fetch_feishu_data.py --sample 2000                     # 调试模式：只拉取2000条，几秒完成
        """,
    )
    parser.add_argument(
        "--wait-refresh",
        type=int,
        default=0,
        metavar="SECONDS",
        help="拉取前等待指定秒数。飞书API存在短暂缓存（最终一致性），更新文档后等待 30-120 秒再拉取可确保拿到最新数据。",
    )
    parser.add_argument(
        "--check-freshness",
        action="store_true",
        help="连续拉取两次数据并对比记录数/关键字段。若不一致则提示可能存在API缓存延迟。",
    )
    parser.add_argument(
        "--app-id",
        type=str,
        default=None,
        metavar="APP_ID",
        help="飞书应用 App ID (优先级高于环境变量 FEISHU_APP_ID)",
    )
    parser.add_argument(
        "--app-secret",
        type=str,
        default=None,
        metavar="APP_SECRET",
        help="飞书应用 App Secret (优先级高于环境变量 FEISHU_APP_SECRET)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        metavar="N",
        help="调试模式：只拉取前 N 条记录（不拉全量，快速验证逻辑）",
    )
    args = parser.parse_args()

    # 凭据优先级: 命令行参数 > 环境变量 > (无默认值，必须提供)
    app_id = args.app_id or APP_ID
    app_secret = args.app_secret or APP_SECRET

    print("=" * 60)
    print("  飞书多维表格数据提取与清洗")
    print("=" * 60)

    # --- 检查凭据 ---
    if not app_id or not app_secret:
        print("\n[ERROR] 错误: 缺少飞书应用凭据！")
        print("   请通过以下任一方式提供:")
        print("   1. 环境变量:  set FEISHU_APP_ID=xxx && set FEISHU_APP_SECRET=xxx")
        print("   2. 命令行参数: python fetch_feishu_data.py --app-id xxx --app-secret xxx\n")
        print("   获取方式: 飞书开放平台 → 企业自建应用 → 凭证与基础信息")
        sys.exit(1)

    # --- 等待刷新（如用户指定了 wait-refresh） ---
    if args.wait_refresh > 0:
        print(f"\n[WAIT] 等待 {args.wait_refresh} 秒以确保飞书API缓存刷新...")
        for remaining in range(args.wait_refresh, 0, -1):
            print(f"  剩余 {remaining} 秒...", end="\r")
            time.sleep(1)
        print("  等待完成，开始拉取数据。                    ")

    # --- 获取 Token ---
    print("\n[1/5] 获取访问令牌...")
    token = get_tenant_access_token(app_id, app_secret)

    # --- 拉取主数据表（文档 A）---
    print(f"\n[2/5] 拉取主数据表 (Base={BASE_A}, Table={TABLE_A})...")
    field_map_a = get_field_meta(token, BASE_A, TABLE_A)
    print(f"  字段数: {len(field_map_a)}")
    for fid, meta in field_map_a.items():
        print(f"    {fid}: {meta['name']} (type={meta['type']})")

    records_a = get_all_records(token, BASE_A, TABLE_A, max_records=args.sample)
    print(f"  主数据总计: {len(records_a)} 条记录")
    _preview_records("文档A-主数据", records_a, field_map_a, n=3)
    df_main = records_to_dataframe(records_a, field_map_a)
    print(f"  DataFrame 形状: {df_main.shape}")
    print(f"  列名: {list(df_main.columns)}")

    # --- 过滤当月数据 ---
    df_main = filter_out_current_month(df_main)

    # --- 拉取映射表（文档 B）---
    print(f"\n[3/5] 拉取映射表 (Base={BASE_B}, Table={TABLE_B})...")
    field_map_b = get_field_meta(token, BASE_B, TABLE_B)
    print(f"  字段数: {len(field_map_b)}")
    for fid, meta in field_map_b.items():
        print(f"    {fid}: {meta['name']} (type={meta['type']})")

    records_b = get_all_records(token, BASE_B, TABLE_B, max_records=args.sample)
    print(f"  映射表总计: {len(records_b)} 条记录")
    _preview_records("文档B-映射表", records_b, field_map_b, n=3)
    df_mapping = records_to_dataframe(records_b, field_map_b)
    print(f"  DataFrame 形状: {df_mapping.shape}")
    print(f"  列名: {list(df_mapping.columns)}")

    # --- 数据新鲜度验证（可选） ---
    if args.check_freshness:
        print(f"\n{'='*60}")
        print("[CHECK] 数据新鲜度验证: 重新拉取文档A进行对比...")
        print(f"{'='*60}")
        # 重新获取 token（旧token可能仍然有效，但为了干净起见用新的）
        token2 = get_tenant_access_token(app_id, app_secret)
        records_a2 = get_all_records(token2, BASE_A, TABLE_A)
        print(f"  第二次拉取: {len(records_a2)} 条记录")

        if len(records_a2) != len(records_a):
            print(f"\n  [WARN] 两次拉取记录数不一致!")
            print(f"     第一次: {len(records_a)} 条")
            print(f"     第二次: {len(records_a2)} 条")
            print(f"     差值: {abs(len(records_a2) - len(records_a))} 条")
            print(f"   -> 飞书API可能存在缓存延迟，建议增加 --wait-refresh 参数后重试")
        else:
            # 记录数一致，检查前几条的关键字段值是否相同
            key_fids = list(field_map_a.keys())[:5]  # 取前5个字段ID
            mismatch_count = 0
            for i, (r1, r2) in enumerate(zip(records_a[:10], records_a2[:10])):
                f1 = r1.get("fields", {})
                f2 = r2.get("fields", {})
                for fid in key_fids:
                    if f1.get(fid) != f2.get(fid):
                        field_name = field_map_a.get(fid, {}).get("name", fid)
                        if mismatch_count < 3:  # 只显示前3个不一致
                            print(f"  记录#{i+1} 字段'{field_name}'不一致")
                        mismatch_count += 1
            if mismatch_count == 0:
                print(f"  [OK] 两次拉取数据一致，数据新鲜度OK")
            else:
                print(f"  [WARN] 发现 {mismatch_count} 处字段值不一致 (共检查 {len(records_a[:10])*len(key_fids)} 个字段)")
                print(f"   -> 飞书API可能仍在返回旧数据，建议增加 --wait-refresh 参数后重试")
        print(f"{'='*60}")

    # --- 数据处理 ---
    print(f"\n[4/5] 数据处理...")
    df_result = process_data(df_main, df_mapping)

    # --- 导出 CSV (UTF-8 BOM) ---
    print(f"\n[5/5] 导出结果到 {OUTPUT_FILE} (UTF-8 BOM)...")
    df_result.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    print(f"\n{'=' * 60}")
    print(f"  [OK] 完成! 输出文件: {OUTPUT_FILE}")
    print(f"     行数: {len(df_result)}")
    print(f"     列数: {len(df_result.columns)}")
    print(f"     列名: {list(df_result.columns)}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
