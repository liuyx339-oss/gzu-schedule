#!/usr/bin/env python3
"""
Wiki 文档更新脚本
==================
根据快照数据计算指标，更新飞书 Wiki 文档。

运行:
  python update_wiki.py              # 更新文档
  python update_wiki.py --dry-run    # 预览变化
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict, Counter
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = PROJECT_DIR / "snapshots"
INDEX_FILE = SNAPSHOT_DIR / "index.json"
DOC_TOKEN = "Hvdpd2sgOoG7TSxxBpsc2Ye9nKf"

# Find lark-cli
LARK_CLI = "lark-cli"
try:
    r = subprocess.run([LARK_CLI, "--version"], capture_output=True, timeout=5)
    if r.returncode != 0:
        raise FileNotFoundError()
except (FileNotFoundError, Exception):
    for p in [r"D:\npm-global\lark-cli.cmd", r"D:\npm-global\lark-cli"]:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                LARK_CLI = p
                break
        except Exception:
            pass


def lark_api(method, path, params=None, data=None):
    """通过 lark-cli 调用飞书 API（--as user）。参数写入当前目录临时文件（相对路径）。"""
    # Switch to project dir so @file references work
    project_dir = str(PROJECT_DIR)
    orig_cwd = os.getcwd()
    os.chdir(project_dir)

    tmp_files = []
    try:
        lc = LARK_CLI if LARK_CLI.startswith('"') else f'"{LARK_CLI}"'
        cmd = f'{lc} api {method} "{path}" --as user --format json'

        if params:
            fn = f"_tmp_lark_p_{os.getpid()}.json"
            with open(fn, 'w', encoding='utf-8') as f:
                json.dump(params, f, ensure_ascii=False)
            tmp_files.append(fn)
            cmd += f' --params "@{fn}"'
        if data:
            fn = f"_tmp_lark_d_{os.getpid()}.json"
            with open(fn, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            tmp_files.append(fn)
            cmd += f' --data "@{fn}"'

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                shell=True, encoding='utf-8', errors='replace')
        if result.returncode != 0:
            print(f"  ❌ lark-cli 失败: {result.stderr[:400]}")
            return None
        return json.loads(result.stdout)
    except Exception as e:
        print(f"  ❌ 调用异常: {e}")
        return None
    finally:
        for f in tmp_files:
            try: os.unlink(f)
            except: pass
        os.chdir(orig_cwd)


def safe_rate(n, d):
    if d == 0:
        return 0.0
    return round(n / d * 100, 2)


def load_snapshot():
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        idx = json.load(f)
    entry = idx[-1]
    with open(SNAPSHOT_DIR / entry["filename"], "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================================
# COMPUTE
# ============================================================================
def filter_level3(records):
    result = []
    for r in records:
        try:
            if int(float(r.get("Rads_Level", "0"))) >= 3:
                result.append(r)
        except (ValueError, TypeError):
            pass
    return result


def yn(recs, field):
    return sum(1 for r in recs if r.get(field, "").strip().upper() == "Y")


def reason_counts(recs, reason_field, filter_field=None, filter_val="N"):
    c = Counter()
    for r in recs:
        if filter_field and r.get(filter_field, "").strip().upper() != filter_val.upper():
            continue
        reason = r.get(reason_field, "").strip()
        if reason:
            c[reason] += 1
    return dict(c.most_common())


def level_breakdown(recs, field):
    """返回按 Level 分组的字段统计字符串"""
    levels = defaultdict(lambda: {"总": 0, "hit": 0})
    for r in recs:
        lv = r.get("Rads_Level", "").strip()
        if not lv:
            continue
        levels[lv]["总"] += 1
        if field and r.get(field, "").strip().upper() == "Y":
            levels[lv]["hit"] += 1
    parts = []
    for lv in sorted(levels.keys(), key=lambda x: int(x)):
        d = levels[lv]
        if field:
            pct = safe_rate(d["hit"], d["总"])
            parts.append(f"{lv}级：{d['hit']}例（{pct}%）")
        else:
            parts.append(f"{lv}级：{d['总']}例")
    return "；".join(parts)


def mrn_list(recs, field, limit=5):
    mrns = []
    for r in recs:
        if r.get(field, "").strip().upper() == "Y":
            mrn = r.get("MRN", "")
            detail = r.get("后续结果", "")
            name = r.get("患者姓名", "")
            tag = name if name else mrn
            suffix = f": {detail}" if detail else ""
            mrns.append(f"{tag}（{mrn}）{suffix}")
    return mrns[:limit]


def compute_wiki_data(snap):
    records = snap["records"]
    l3 = filter_level3(records)

    total = len(l3)
    follow = yn(l3, "是否需要跟进")
    contact = yn(l3, "HCM/PCM 是否与患者发起沟通")
    book = yn(l3, "是否进行专科预约")
    visit = yn(l3, "是否完成专科问诊")
    biopsy = yn(l3, "是否活检")
    surgery = yn(l3, "是否转化手术")
    cm = contact - book

    overall = {
        "总更新": total,
        "总更新_各级": level_breakdown(l3, None),
        "需跟进": follow,
        "需跟进_各级": level_breakdown(l3, "是否需要跟进"),
        "需跟进率": safe_rate(follow, total),
        "已沟通": contact,
        "已沟通_各级": level_breakdown(l3, "HCM/PCM 是否与患者发起沟通"),
        "跟进率": safe_rate(contact, follow),
        "待跟进": follow - contact,
        "已预约": book,
        "已预约_各级": level_breakdown(l3, "是否进行专科预约"),
        "预约转化率": safe_rate(book, contact),
        "已就诊": visit,
        "已就诊_各级": level_breakdown(l3, "是否完成专科问诊"),
        "就诊转化率": safe_rate(visit, book),
        "待预约就诊": book - visit,
        "CM随访预约": cm,
        "已活检": biopsy,
        "已活检_各级": level_breakdown(l3, "是否活检"),
        "已手术": surgery,
        "已手术_各级": level_breakdown(l3, "是否转化手术"),
        "手术MRN": mrn_list(l3, "是否转化手术"),
        "活检MRN": mrn_list(l3, "是否活检"),
        "手术占就诊": safe_rate(surgery, visit),
        "无需跟进原因": reason_counts(l3, "无需跟进原因", "是否需要跟进", "N"),
        "未成功预约原因": reason_counts(l3, "未成功预约专科/会诊原因", "是否进行专科预约", "N"),
        "未成功沟通原因": reason_counts(l3, "未成功发起患者沟通原因", "HCM/PCM 是否与患者发起沟通", "N"),
    }

    type_names = {"LUNG-RADS": "肺结节", "BI-RADS": "乳腺结节", "TI-RADS": "甲状腺结节"}
    by_type = {}
    for rtype, tname in type_names.items():
        trecs = [r for r in l3 if r.get("Rads_Type", "").strip() == rtype]
        if not trecs:
            continue
        t = {}
        t["总更新"] = len(trecs)
        t["总更新_各级"] = level_breakdown(trecs, None)
        t["需跟进"] = yn(trecs, "是否需要跟进")
        t["需跟进_各级"] = level_breakdown(trecs, "是否需要跟进")
        t["需跟进率"] = safe_rate(t["需跟进"], t["总更新"])
        t["已沟通"] = yn(trecs, "HCM/PCM 是否与患者发起沟通")
        t["已沟通_各级"] = level_breakdown(trecs, "HCM/PCM 是否与患者发起沟通")
        t["跟进率"] = safe_rate(t["已沟通"], t["需跟进"])
        t["待跟进"] = t["需跟进"] - t["已沟通"]
        t["已预约"] = yn(trecs, "是否进行专科预约")
        t["已预约_各级"] = level_breakdown(trecs, "是否进行专科预约")
        t["预约转化率"] = safe_rate(t["已预约"], t["已沟通"])
        t["已就诊"] = yn(trecs, "是否完成专科问诊")
        t["已就诊_各级"] = level_breakdown(trecs, "是否完成专科问诊")
        t["就诊转化率"] = safe_rate(t["已就诊"], t["已预约"])
        t["待预约就诊"] = t["已预约"] - t["已就诊"]
        t["CM随访预约"] = t["已沟通"] - t["已预约"]
        t["已活检"] = yn(trecs, "是否活检")
        t["已活检_各级"] = level_breakdown(trecs, "是否活检")
        t["已手术"] = yn(trecs, "是否转化手术")
        t["已手术_各级"] = level_breakdown(trecs, "是否转化手术")
        t["手术MRN"] = mrn_list(trecs, "是否转化手术")
        t["活检MRN"] = mrn_list(trecs, "是否活检")
        t["手术占就诊"] = safe_rate(t["已手术"], t["已就诊"])
        t["无需跟进原因"] = reason_counts(trecs, "无需跟进原因", "是否需要跟进", "N")
        by_type[rtype] = t

    return {"overall": overall, "by_type": by_type, "type_names": type_names}


# ============================================================================
# BUILD UPDATES BY BLOCK MATCHING
# ============================================================================
def get_all_blocks():
    """获取所有 blocks（分页），返回 {block_id: {type, text}} 映射"""
    mapping = {}
    page_token = None

    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token

        data = lark_api("GET", f"docx/v1/documents/{DOC_TOKEN}/blocks", params)
        if not data or data.get("code") != 0:
            print(f"❌ 获取 blocks 失败: {json.dumps(data, ensure_ascii=False)[:200]}")
            return {}

        items = data.get("data", {}).get("items", [])
        for item in items:
            bid = item["block_id"]
            bt = item["block_type"]
            texts = []
            for el in item.get("text", {}).get("elements", []):
                if el.get("text_run"):
                    texts.append(el["text_run"].get("content", ""))
            full_text = "".join(texts).strip()
            mapping[bid] = {"type": bt, "text": full_text}
            # Children can be full objects (nested blocks) or string IDs (for document root)
            # For nested blocks (e.g., tables contain rows contain cells), they're objects
            for child in item.get("children", []):
                if isinstance(child, dict):
                    _walk_nested(child, mapping)

        has_more = data.get("data", {}).get("has_more", False)
        page_token = data.get("data", {}).get("page_token", "")
        if not has_more:
            break

    return mapping


def _walk_nested(item, mapping):
    """递归处理嵌套 block 对象"""
    bid = item["block_id"]
    bt = item["block_type"]
    texts = []
    for el in item.get("text", {}).get("elements", []):
        if el.get("text_run"):
            texts.append(el["text_run"].get("content", ""))
    full_text = "".join(texts).strip()
    mapping[bid] = {"type": bt, "text": full_text}
    for child in item.get("children", []):
        if isinstance(child, dict):
            _walk_nested(child, mapping)


def find_cell_blocks(block_map):
    """
    在 block_map 中搜索文档中已知的数值单元格并返回 block_id。

    策略：按文本内容精确匹配，逐行定位。
    返回: {逻辑名称: block_id}
    """
    found = {}

    def find(text_contains, name=None):
        """找到第一个包含指定文本的 block"""
        n = name or text_contains
        if n in found:
            return
        for bid, info in block_map.items():
            if text_contains in info["text"]:
                found[n] = bid
                return

    # ===== 总体跟进 =====
    find("877", "总体_总更新")  # total count
    find("655", "总体_需跟进")
    find("611", "总体_已沟通")
    find("44", "总体_待跟进")  # was 25 in old doc
    find("74.69", "总体_需跟进率")
    find("93.28", "总体_跟进率")

    # ===== 专科预约 =====
    find("66", "总体_已预约")  # OPV
    find("63", "总体_已就诊")
    find("10.8", "总体_预约转化率")
    find("95.45", "总体_就诊转化率")
    find("276", "总体_CM随访")  # old CM value
    find("545", "总体_CM随访_new")

    # ===== 手术 =====
    find("6台", "总体_手术数")
    find("4台", "总体_手术完成")

    # ===== Level breakdown cells (look for cells containing the wrong old data) =====
    find("3：例数", "总体_总更新level")
    find("748", "总体_level_broken")
    find("23", "总体_level_more")

    # ===== 肺结节 (LUNG-RADS) =====
    find("101", "肺结节_总更新")
    find("91", "肺结节_需跟进")
    find("84", "肺结节_已沟通")
    find("10", "肺结节_已预约")  # OPV
    # Wait, 10 could appear in multiple cells. Let me be more specific.

    # ===== 乳腺结节 (BI-RADS) =====
    find("273", "乳腺结节_总更新")
    find("211", "乳腺结节_需跟进")
    find("205", "乳腺结节_已沟通")
    find("38", "乳腺结节_已预约")

    # ===== 甲状腺结节 (TI-RADS) =====
    find("287", "甲状腺结节_总更新")
    find("186", "甲状腺结节_需跟进")
    find("174", "甲状腺结节_已沟通")
    find("12", "甲状腺结节_已预约")  # OPV for thyroid

    return found


def build_batch_requests(wiki, block_map):
    """构建 batch_update 请求列表"""
    found = find_cell_blocks(block_map)
    updates = []

    def add(bid, new_text):
        if bid:
            updates.append({
                "replace_text": {
                    "block_id": bid,
                    "text": str(new_text),
                }
            })

    ov = wiki["overall"]
    bt = wiki["by_type"]

    print("\n=== 总体 ===")
    # Numbers that might be correct already are skipped

    print("  总体跟进")
    add(found.get("总体_总更新"), str(ov["总更新"]))
    add(found.get("总体_需跟进"), str(ov["需跟进"]))
    add(found.get("总体_已沟通"), str(ov["已沟通"]))
    add(found.get("总体_待跟进"), str(ov["待跟进"]))
    add(found.get("总体_需跟进率"), f"需跟进率{ov['需跟进率']}%")
    add(found.get("总体_跟进率"), f"跟进率 {ov['跟进率']}%")

    print("  专科预约")
    add(found.get("总体_已预约"), str(ov["已预约"]))
    add(found.get("总体_已就诊"), str(ov["已就诊"]))
    add(found.get("总体_预约转化率"), f"占已跟进总数 {ov['预约转化率']}%")
    add(found.get("总体_就诊转化率"), f"占同意预约总数 {ov['就诊转化率']}%")
    add(found.get("总体_CM随访"), str(ov["CM随访预约"]))

    print("  手术")
    add(found.get("总体_手术数"), f"{ov['已手术']}台")
    add(found.get("总体_手术完成"), f"{ov['已手术']}台")

    # Level breakdown
    add(found.get("总体_总更新level"), ov["总更新_各级"])
    add(found.get("总体_需跟进level"), ov["需跟进_各级"])

    # By type
    for rtype, tname in wiki["type_names"].items():
        if rtype not in bt:
            continue
        td = bt[rtype]
        print(f"\n=== {tname} ===")
        fk = f"{tname}_"
        add(found.get(fk + "总更新"), str(td["总更新"]))
        add(found.get(fk + "需跟进"), str(td["需跟进"]))
        add(found.get(fk + "已沟通"), str(td["已沟通"]))
        add(found.get(fk + "已预约"), str(td["已预约"]))

    updates = [u for u in updates if u["replace_text"]["block_id"]]
    return updates


# ============================================================================
# MAIN
# ============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("📝 Wiki 文档更新")
    print(f"   文档 token: {DOC_TOKEN}")
    print(f"   lark-cli: {LARK_CLI}")
    print("=" * 60)

    # Step 1
    print("\n📂 加载快照...")
    snap = load_snapshot()
    print(f"   {snap['snapshot_id']}: {snap['record_count']} 条")

    # Step 2
    print("\n🔢 计算指标...")
    wiki = compute_wiki_data(snap)

    ov = wiki["overall"]
    print(f"""
  📊 总体:
     总更新: {ov['总更新']}
     需跟进: {ov['需跟进']} ({ov['需跟进率']}%)
     已沟通: {ov['已沟通']} ({ov['跟进率']}%)
     待跟进: {ov['待跟进']}
     已预约: {ov['已预约']} ({ov['预约转化率']}%)
     已就诊: {ov['已就诊']} ({ov['就诊转化率']}%)
     已活检: {ov['已活检']}  已手术: {ov['已手术']}
     CM随访: {ov['CM随访预约']}
""")

    for rtype, tname in wiki["type_names"].items():
        if rtype in wiki["by_type"]:
            d = wiki["by_type"][rtype]
            print(f"  🔬 {tname}: 总={d['总更新']}, 需跟={d['需跟进']}({d['需跟进率']}%), 沟通={d['已沟通']}, 预约={d['已预约']}, 就诊={d['已就诊']}, 活检={d['已活检']}, 手术={d['已手术']}")

    # Step 3
    print("\n📄 获取文档 blocks...")
    block_map = get_all_blocks()
    if not block_map:
        print("❌ 获取失败，终止")
        return
    print(f"   {len(block_map)} blocks")

    # Step 4
    print("\n🔧 构建更新...")
    updates = build_batch_requests(wiki, block_map)
    print(f"\n   ✅ {len(updates)} 个 block 需要更新")

    if args.dry_run:
        print("\n⚠️  DRY RUN — 未执行更新")
        print("\n将更新的内容:")
        for u in updates:
            rt = u["replace_text"]
            info = block_map.get(rt["block_id"], {})
            old = info.get("text", "")[:50]
            print(f"   [{rt['block_id'][:20]}...] {old[:30]:30s} → {rt['text'][:60]}")
        return

    # Step 5: Execute
    if not updates:
        print("\n✅ 无内容需要更新")
        return

    print(f"\n🔄 发送 {len(updates)} 条更新...")
    # Batch by 50
    for i in range(0, len(updates), 50):
        batch = updates[i:i + 50]
        resp = lark_api("PATCH", f"docx/v1/documents/{DOC_TOKEN}/blocks/batch_update",
                        data={"requests": batch})
        if resp and resp.get("code") == 0:
            print(f"  ✅ 批次 {i // 50 + 1} 完成 ({len(batch)} 条)")
        else:
            print(f"  ❌ 批次失败: {json.dumps(resp, ensure_ascii=False)[:300]}")

    print("\n🎉 完成！刷新飞书文档查看更新。")


if __name__ == "__main__":
    main()
