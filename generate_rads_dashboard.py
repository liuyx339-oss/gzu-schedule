#!/usr/bin/env python3
"""
GZM Rads Tracking — 看板生成器
================================
读取快照数据，计算转化漏斗指标，渲染为交互式 HTML 看板。

运行方式:
  python generate_rads_dashboard.py                    # 用最新快照生成
  python generate_rads_dashboard.py --snapshot 20260618_143000  # 指定快照
  python generate_rads_dashboard.py --compare           # 对比最近两次快照
  python generate_rads_dashboard.py --open              # 生成并打开浏览器
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Windows 终端编码修复
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ============================================================================
# CONFIG
# ============================================================================
PROJECT_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = PROJECT_DIR / "snapshots"
INDEX_FILE = SNAPSHOT_DIR / "index.json"
TEMPLATE_FILE = PROJECT_DIR / "rads_dashboard_template.html"
OUTPUT_FILE = PROJECT_DIR / "rads_dashboard.html"

# 漏斗阶段字段映射（按先后顺序）
STAGE_FIELDS = [
    ("总更新人数_Ⅲ级及以上", None),                          # 特殊：count of Rads_Level>=3
    ("需跟进人数", "是否需要跟进"),
    ("已沟通人数", "HCM/PCM 是否与患者发起沟通"),
    ("已预约人数", "是否进行专科预约"),
    ("已就诊人数", "是否完成专科问诊"),
    ("已活检人数", "是否活检"),
    ("已手术人数", "是否转化手术"),
]

# Y/N 字段列表
YN_FIELDS = [
    "是否需要跟进",
    "HCM/PCM 是否与患者发起沟通",
    "是否进行专科预约",
    "是否完成专科问诊",
    "是否活检",
    "是否转化手术",
]

# 原因字段
REASON_FIELDS = {
    "无需跟进原因": "无需跟进原因",
    "未成功预约原因": "未成功预约专科/会诊原因",
    "未成功沟通原因": "未成功发起患者沟通原因",
}


def load_latest_snapshot(snapshot_id: str | None = None) -> dict:
    """加载快照"""
    if not INDEX_FILE.exists():
        print("❌ 没有快照索引，请先运行 fetch_rads_data.py")
        sys.exit(1)

    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        index = json.load(f)

    if not index:
        print("❌ 快照索引为空")
        sys.exit(1)

    if snapshot_id:
        entry = next((e for e in index if e["snapshot_id"] == snapshot_id), None)
        if not entry:
            print(f"❌ 未找到快照: {snapshot_id}")
            print(f"   可用: {[e['snapshot_id'] for e in index]}")
            sys.exit(1)
    else:
        entry = index[-1]

    snap_path = SNAPSHOT_DIR / entry["filename"]
    with open(snap_path, "r", encoding="utf-8") as f:
        return json.load(f)


def filter_rads_level_3plus(records: list[dict]) -> list[dict]:
    """筛选 Rads_Level >= 3 的记录（Ⅲ级及以上）"""
    result = []
    for r in records:
        val = r.get("Rads_Level", "").strip()
        try:
            if int(float(val)) >= 3:
                result.append(r)
        except (ValueError, TypeError):
            pass
    return result


def count_yn(records: list[dict], field: str) -> int:
    """统计某 Y/N 字段为 Y 的记录数"""
    cnt = 0
    for r in records:
        if r.get(field, "").strip().upper() == "Y":
            cnt += 1
    return cnt


def count_reason(records: list[dict], reason_field: str, filter_field: str | None = None, filter_val: str = "N") -> dict[str, int]:
    """
    统计原因分布。
    如果指定 filter_field，则只在 filter_field == filter_val 的记录中统计。
    """
    counter: Counter = Counter()
    for r in records:
        if filter_field:
            if r.get(filter_field, "").strip().upper() != filter_val.upper():
                continue
        reason = r.get(reason_field, "").strip()
        if reason:
            counter[reason] += 1
    return dict(counter.most_common())


def safe_rate(numerator: int, denominator: int) -> float:
    """安全计算百分比"""
    if denominator == 0:
        return 0.0
    return round(numerator / denominator * 100, 2)


def compute_metrics(records: list[dict]) -> dict:
    """计算所有漏斗指标"""
    # 过滤 Ⅲ级及以上
    level3 = filter_rads_level_3plus(records)

    # 逐阶段计数
    total = len(level3)
    follow = count_yn(level3, "是否需要跟进")
    contact = count_yn(level3, "HCM/PCM 是否与患者发起沟通")
    book = count_yn(level3, "是否进行专科预约")
    visit = count_yn(level3, "是否完成专科问诊")
    biopsy = count_yn(level3, "是否活检")
    surgery = count_yn(level3, "是否转化手术")

    metrics = {
        "overall": {
            "总更新人数_Ⅲ级及以上": total,
            "需跟进人数": follow,
            "已沟通人数": contact,
            "已预约人数": book,
            "已就诊人数": visit,
            "已活检人数": biopsy,
            "已手术人数": surgery,
        },
        "rates": {
            "需跟进率": safe_rate(follow, total),
            "跟进率": safe_rate(contact, follow),
            "预约转化率": safe_rate(book, contact),
            "就诊转化率": safe_rate(visit, book),
            "活检率": safe_rate(biopsy, visit),
            "手术转化率": safe_rate(surgery, visit),
        },
    }

    # 按 Rads_Level 分组
    by_level: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for r in level3:
        lv = r.get("Rads_Level", "").strip()
        if not lv:
            continue
        by_level[lv]["总更新"] += 1
        for label, field in STAGE_FIELDS[1:]:  # skip the first (it's the total itself)
            if field and r.get(field, "").strip().upper() == "Y":
                by_level[lv][label.replace("人数", "")] += 1
    # 排序 levels
    try:
        sorted_levels = sorted(by_level.keys(), key=lambda x: int(float(x)))
    except Exception:
        sorted_levels = sorted(by_level.keys())
    metrics["by_level"] = {lv: dict(by_level[lv]) for lv in sorted_levels}

    # 按 Rads_Type × Rads_Level 分组
    by_type: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    all_types = set()
    for r in level3:
        rtype = r.get("Rads_Type", "").strip()
        if not rtype:
            rtype = "未分类"
        all_types.add(rtype)
        lv = r.get("Rads_Level", "").strip()
        if not lv:
            continue
        # 总体
        by_type[rtype]["总体"]["总更新"] += 1
        for label, field in STAGE_FIELDS[1:]:
            if field and r.get(field, "").strip().upper() == "Y":
                by_type[rtype]["总体"][label.replace("人数", "")] += 1
        # 按 level
        by_type[rtype][lv]["总更新"] += 1
        for label, field in STAGE_FIELDS[1:]:
            if field and r.get(field, "").strip().upper() == "Y":
                by_type[rtype][lv][label.replace("人数", "")] += 1

    # 按 type 总更新数排序
    type_totals = {t: by_type[t]["总体"].get("总更新", 0) for t in all_types}
    sorted_types = sorted(all_types, key=lambda t: type_totals[t], reverse=True)
    metrics["by_type"] = {t: {k: dict(v) for k, v in by_type[t].items()} for t in sorted_types}

    # 原因分析
    reasons = {}
    for label, field_name in REASON_FIELDS.items():
        if label == "无需跟进原因":
            reasons[label] = count_reason(level3, field_name, "是否需要跟进", "N")
        elif label == "未成功预约原因":
            reasons[label] = count_reason(level3, field_name, "是否进行专科预约", "N")
        elif label == "未成功沟通原因":
            reasons[label] = count_reason(level3, field_name, "HCM/PCM 是否与患者发起沟通", "N")
    metrics["reasons"] = reasons

    return metrics


def _filter_records(records: list[dict], level: str | None = None, rtype: str | None = None) -> list[dict]:
    """按 Rads_Level 和/或 Rads_Type 过滤记录（仅 Ⅲ 级及以上）。"""
    result = []
    for r in records:
        try:
            if int(float(r.get("Rads_Level", "0"))) < 3:
                continue
        except (ValueError, TypeError):
            continue
        if level is not None and r.get("Rads_Level", "").strip() != level:
            continue
        if rtype is not None and r.get("Rads_Type", "").strip() != rtype:
            continue
        result.append(r)
    return result


def _mrn_set(records: list[dict], field: str | None) -> set:
    """获取某 Y/N 字段为 Y 的 MRN 集合。field 为 None 时返回全部 MRN。"""
    if field is None:
        return {r.get("MRN", "") for r in records}
    return {r.get("MRN", "") for r in records if r.get(field, "").strip().upper() == "Y"}


def _stage_diff_row(dimension: str, label: str, old_set: set, new_set: set) -> dict:
    """生成单行对比数据"""
    old_cnt = len(old_set)
    new_cnt = len(new_set)
    added = new_set - old_set
    removed = old_set - new_set
    delta = len(added) - len(removed)
    change_pct = safe_rate(delta, old_cnt) if old_cnt else (100 if new_cnt else 0)
    return {
        "维度": dimension,
        "指标": label,
        "旧版本": old_cnt,
        "新版本": new_cnt,
        "新增MRN": list(added)[:10],
        "移除MRN": list(removed)[:10],
        "净变化": f"+{delta}" if delta > 0 else str(delta),
        "变化率": f"{change_pct:+.1f}%",
    }


def compute_diff(snap_a: dict, snap_b: dict) -> dict | None:
    """对比两个快照，计算各维度新增。snap_a 旧，snap_b 新。"""
    recs_a = snap_a["records"]
    recs_b = snap_b["records"]

    STAGES = [
        ("总更新 (Ⅲ级+)", None),
        ("需跟进", "是否需要跟进"),
        ("已沟通", "HCM/PCM 是否与患者发起沟通"),
        ("已预约", "是否进行专科预约"),
        ("已就诊", "是否完成专科问诊"),
        ("已活检", "是否活检"),
        ("已手术", "是否转化手术"),
    ]

    LEVELS = ["3", "4", "5"]

    diff_rows = []

    # ============================================================
    # Section A: 总体 × Level
    # ============================================================
    for label, field in STAGES:
        recs_old = _filter_records(recs_a)
        recs_new = _filter_records(recs_b)
        old_set = _mrn_set(recs_old, field)
        new_set = _mrn_set(recs_new, field)
        diff_rows.append(_stage_diff_row("总体", label, old_set, new_set))

        # Level 子行
        for lv in LEVELS:
            lv_label = f"  └ Level {lv}"
            recs_old_lv = _filter_records(recs_a, level=lv)
            recs_new_lv = _filter_records(recs_b, level=lv)
            old_set_lv = _mrn_set(recs_old_lv, field)
            new_set_lv = _mrn_set(recs_new_lv, field)
            diff_rows.append(_stage_diff_row(lv_label, label, old_set_lv, new_set_lv))

    # ============================================================
    # Section B: 按 Type × Level
    # ============================================================
    # 收集所有 type
    all_types = set()
    for r in _filter_records(recs_a) + _filter_records(recs_b):
        t = r.get("Rads_Type", "").strip()
        if t:
            all_types.add(t)

    for rtype in sorted(all_types):
        # Type 总体
        for label, field in STAGES:
            recs_old_t = _filter_records(recs_a, rtype=rtype)
            recs_new_t = _filter_records(recs_b, rtype=rtype)
            old_set_t = _mrn_set(recs_old_t, field)
            new_set_t = _mrn_set(recs_new_t, field)
            diff_rows.append(_stage_diff_row(rtype, label, old_set_t, new_set_t))

            # Type × Level 子行
            for lv in LEVELS:
                lv_label = f"  └ Level {lv}"
                recs_old_tl = [r for r in recs_old_t if r.get("Rads_Level", "").strip() == lv]
                recs_new_tl = [r for r in recs_new_t if r.get("Rads_Level", "").strip() == lv]
                old_set_tl = _mrn_set(recs_old_tl, field)
                new_set_tl = _mrn_set(recs_new_tl, field)
                diff_rows.append(_stage_diff_row(lv_label, label, old_set_tl, new_set_tl))

    return {
        "old_snapshot": snap_a["snapshot_id"],
        "new_snapshot": snap_b["snapshot_id"],
        "diff_rows": diff_rows,
    }


def gather_version_list() -> list[dict]:
    """返回快照版本列表"""
    if not INDEX_FILE.exists():
        return []
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_dashboard(snapshot_id: str | None = None, compare: bool = False):
    """主函数：生成看板 HTML"""
    print("=" * 60)
    print("📊 GZM Rads Tracking — 看板生成")
    print("=" * 60)

    # 加载快照
    snap = load_latest_snapshot(snapshot_id)
    print(f"\n📂 快照: {snap['snapshot_id']}")
    print(f"   记录数: {snap['record_count']}")

    # 计算指标
    print("\n🔢 计算指标...")
    metrics = compute_metrics(snap["records"])

    # 打印关键指标
    ov = metrics["overall"]
    rt = metrics["rates"]
    print(f"   总更新(Ⅲ级+): {ov['总更新人数_Ⅲ级及以上']}")
    print(f"   需跟进: {ov['需跟进人数']} ({rt['需跟进率']}%)")
    print(f"   已沟通: {ov['已沟通人数']} ({rt['跟进率']}%)")
    print(f"   已预约: {ov['已预约人数']} ({rt['预约转化率']}%)")
    print(f"   已就诊: {ov['已就诊人数']} ({rt['就诊转化率']}%)")
    print(f"   已活检: {ov['已活检人数']} ({rt['活检率']}%)")
    print(f"   已手术: {ov['已手术人数']} ({rt['手术转化率']}%)")

    print(f"\n   Rads_Level 分组: {list(metrics['by_level'].keys())}")
    for lv, d in metrics["by_level"].items():
        print(f"     Level {lv}: 总更新={d.get('总更新',0)}, 需跟进={d.get('需跟进',0)}")

    print(f"\n   Rads_Type 分组: {list(metrics['by_type'].keys())}")

    # 构建 DATA
    data = {
        "timestamp": snap["timestamp"],
        "record_count": snap["record_count"],
        "snapshot_id": snap["snapshot_id"],
        "metrics": metrics,
        "snapshots": [e["snapshot_id"] for e in gather_version_list()],
    }

    # 版本对比
    if compare:
        print("\n🔄 版本对比...")
        versions = gather_version_list()
        if len(versions) >= 2:
            snap_old = load_latest_snapshot(versions[-2]["snapshot_id"])
            snap_new = snap
            diff = compute_diff(snap_old, snap_new)
            data["diff"] = diff
            print(f"   对比: {versions[-2]['snapshot_id']} vs {versions[-1]['snapshot_id']}")
            for row in diff["diff_rows"]:
                print(f"     {row['指标']}: {row['旧版本']} → {row['新版本']} ({row['净变化']})")
        else:
            print("   ⚠️  需要至少2个快照才能对比")

    # 读取模板
    print(f"\n📄 读取模板: {TEMPLATE_FILE.name}")
    if not TEMPLATE_FILE.exists():
        print(f"❌ 模板文件不存在: {TEMPLATE_FILE}")
        sys.exit(1)

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    # 注入数据
    json_data = json.dumps(data, ensure_ascii=False, default=str)
    html = html.replace("__DATA_PLACEHOLDER__", json_data)

    # 写入输出
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ 看板已生成: {OUTPUT_FILE}")
    return OUTPUT_FILE


def main():
    parser = argparse.ArgumentParser(description="GZM Rads Tracking 看板生成")
    parser.add_argument("--snapshot", type=str, default=None,
                        help="指定快照ID（默认最新）")
    parser.add_argument("--compare", action="store_true",
                        help="对比最近两次快照")
    parser.add_argument("--open", action="store_true",
                        help="生成后在浏览器打开")
    args = parser.parse_args()

    out = generate_dashboard(snapshot_id=args.snapshot, compare=args.compare)

    if args.open:
        webbrowser.open(str(out))


if __name__ == "__main__":
    main()
