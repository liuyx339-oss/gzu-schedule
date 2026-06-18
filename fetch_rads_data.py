#!/usr/bin/env python3
"""
GZM Rads Tracking — 数据拉取与快照管理
========================================
从飞书多维表格 "GZIOC_结节影像跟踪表" 拉取全量数据，每次保存为快照 JSON，
保留最近 5 次快照，供看板渲染和历史对比使用。

数据源:
  Base: DsqjbgaZxaYT2isKd4EcoC9nnNg
  Table: tblwEBV5JdCoQ2Gd

依赖: lark-cli (已登录 --as user 且有 base:record:retrieve 权限)

运行方式:
  python fetch_rads_data.py                # 正常拉取
  python fetch_rads_data.py --sample 3     # 只拉3条测试
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Windows 终端编码修复
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ============================================================================
# CONFIG
# ============================================================================
BASE_ID = "DsqjbgaZxaYT2isKd4EcoC9nnNg"
TABLE_ID = "tblwEBV5JdCoQ2Gd"
PAGE_SIZE = 500  # 飞书单页最大500条

# 项目目录：脚本所在目录
PROJECT_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = PROJECT_DIR / "snapshots"
INDEX_FILE = SNAPSHOT_DIR / "index.json"
MAX_SNAPSHOTS = 5


def lark_api(method: str, path: str, params: dict | None = None) -> dict:
    """通过 lark-cli 调用飞书 API (--as user)"""
    cmd = ["lark-cli", "api", method, path, "--as", "user", "--format", "json"]
    if params:
        cmd += ["--params", json.dumps(params, ensure_ascii=False)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                            cwd=str(PROJECT_DIR))
    if result.returncode != 0:
        try:
            err = json.loads(result.stderr) if result.stderr else {}
        except json.JSONDecodeError:
            err = {"raw": result.stderr[:500]}
        if err.get("error", {}).get("subtype") == "missing_scope":
            print("❌ 授权缺失！请先运行:")
            print('   lark-cli auth login --scope "bitable:app:readonly bitable:app base:record:retrieve"')
            print("   然后扫码授权后重试。")
            sys.exit(1)
        raise RuntimeError(f"lark-cli 调用失败: {json.dumps(err, ensure_ascii=False)}")
    return json.loads(result.stdout)


def get_field_map() -> dict[str, dict]:
    """获取字段映射: field_id -> {name, type, ui_type, options}"""
    data = lark_api("GET", f"bitable/v1/apps/{BASE_ID}/tables/{TABLE_ID}/fields")
    fields = {}
    for item in data.get("data", {}).get("items", []):
        field_id = item["field_id"]
        prop = item.get("property", {})
        fields[field_id] = {
            "name": item.get("field_name", ""),
            "type": item["type"],
            "ui_type": item.get("ui_type", ""),
            "options": {o["name"]: o["id"] for o in prop.get("options", [])} if prop else {},
        }
    return fields


def fetch_all_records(sample: int = 0) -> list[dict]:
    """分页拉取全部记录，返回原始记录列表。sample>0 时只拉前N条。"""
    all_records = []
    page_token = None
    page = 0

    while True:
        page += 1
        params = {"page_size": PAGE_SIZE}
        if page_token:
            params["page_token"] = page_token

        data = lark_api("GET",
                        f"bitable/v1/apps/{BASE_ID}/tables/{TABLE_ID}/records",
                        params)

        items = data.get("data", {}).get("items", [])
        all_records.extend(items)

        total = data.get("data", {}).get("total", 0)
        has_more = data.get("data", {}).get("has_more", False)
        page_token = data.get("data", {}).get("page_token", "")

        print(f"  📄 第{page}页: 拉取 {len(items)} 条, 累计 {len(all_records)}/{total}")

        if sample and len(all_records) >= sample:
            all_records = all_records[:sample]
            break
        if not has_more:
            break

    return all_records


def parse_record(record: dict, field_map: dict) -> dict:
    """将飞书原始 record 转为 {field_name: value} 的字典"""
    parsed = {}
    fields = record.get("fields", {})
    for field_id, value in fields.items():
        finfo = field_map.get(field_id, {})
        name = finfo.get("name", field_id)
        # 处理不同类型的值
        if isinstance(value, list):
            # 多选 / 单选 / 附件 / 用户 等
            if len(value) == 0:
                parsed[name] = ""
            elif isinstance(value[0], dict):
                parsed[name] = value[0].get("text", value[0].get("name", str(value[0])))
            else:
                parsed[name] = ", ".join(str(v) for v in value)
        elif isinstance(value, dict):
            parsed[name] = value.get("text", value.get("name", json.dumps(value, ensure_ascii=False)))
        elif value is None:
            parsed[name] = ""
        else:
            parsed[name] = str(value)
    return parsed


def save_snapshot(records: list[dict], field_map: dict) -> Path:
    """保存快照并维护索引，返回快照文件路径"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_file = SNAPSHOT_DIR / f"rads_snapshot_{timestamp}.json"

    # 解析所有记录
    parsed_records = [parse_record(r, field_map) for r in records]

    snapshot = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_id": timestamp,
        "record_count": len(parsed_records),
        "base_id": BASE_ID,
        "table_id": TABLE_ID,
        "records": parsed_records,
    }

    with open(snap_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"  💾 快照已保存: {snap_file.name} ({len(parsed_records)} 条记录)")

    # 更新索引
    update_index(timestamp, snap_file.name, len(parsed_records))

    # 清理旧快照
    cleanup_old_snapshots()

    return snap_file


def update_index(snapshot_id: str, filename: str, count: int):
    """更新快照索引"""
    index = []
    if INDEX_FILE.exists():
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            index = json.load(f)

    index.append({
        "snapshot_id": snapshot_id,
        "filename": filename,
        "record_count": count,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

    # 只保留最近 MAX_SNAPSHOTS 个索引
    index = index[-MAX_SNAPSHOTS:]

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def cleanup_old_snapshots():
    """清理超出 MAX_SNAPSHOTS 的旧快照文件"""
    if not INDEX_FILE.exists():
        return
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        index = json.load(f)

    snap_files = sorted(SNAPSHOT_DIR.glob("rads_snapshot_*.json"))
    kept_names = {entry["filename"] for entry in index}
    for fp in snap_files:
        if fp.name not in kept_names:
            fp.unlink()
            print(f"  🗑️  清理旧快照: {fp.name}")


def main():
    parser = argparse.ArgumentParser(description="GZM Rads Tracking 数据拉取")
    parser.add_argument("--sample", type=int, default=0,
                        help="只拉取前N条记录（测试用）")
    args = parser.parse_args()

    print("=" * 60)
    print("📊 GZM Rads Tracking — 数据拉取")
    print(f"   Base:  {BASE_ID}")
    print(f"   Table: {TABLE_ID}")
    print("=" * 60)

    # Step 1: 获取字段映射
    print("\n🔍 获取字段结构...")
    field_map = get_field_map()
    named_fields = {f["name"]: fid for fid, f in field_map.items() if f["name"]}
    print(f"   ✅ 共 {len(field_map)} 个字段")

    # 打印关键字段确认
    key_names = ["MRN", "Rads_Level", "Rads_Type", "是否需要跟进",
                 "HCM/PCM 是否与患者发起沟通", "是否进行专科预约",
                 "是否完成专科问诊", "是否活检", "是否转化手术",
                 "无需跟进原因", "未成功预约专科/会诊原因"]
    for name in key_names:
        if name in named_fields:
            print(f"   ✓ {name}")
        else:
            print(f"   ⚠️  {name} — 未找到")

    # Step 2: 拉取全量数据
    print(f"\n📥 拉取记录..." + (" (采样模式)" if args.sample else ""))
    records = fetch_all_records(sample=args.sample)
    print(f"   ✅ 共拉取 {len(records)} 条记录")

    # Step 3: 保存快照
    print(f"\n💾 保存快照...")
    snap_path = save_snapshot(records, field_map)

    # Step 4: 摘要
    print(f"\n📋 快照摘要:")
    print(f"   快照ID:  {snap_path.stem}")
    print(f"   记录数:  {len(records)}")
    print(f"   位置:    {snap_path}")

    if INDEX_FILE.exists():
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            index = json.load(f)
        print(f"   历史快照: {len(index)} 个")
        for entry in index:
            print(f"     • {entry['snapshot_id']} — {entry['record_count']} 条")

    print("\n✅ 完成！运行 generate_rads_dashboard.py 生成看板。")


if __name__ == "__main__":
    main()
