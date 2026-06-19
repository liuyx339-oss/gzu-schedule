#!/usr/bin/env python3
"""
GZM Rads Tracking — 看板生成器 v2
================================
直接渲染完整 HTML，不依赖模板替换，确保 JS 数据注入正确。

运行:
  python generate_rads_dashboard.py              # 用最新快照生成
  python generate_rads_dashboard.py --excel xlsx # 从 Excel 导入
  python generate_rads_dashboard.py --open       # 生成并打开浏览器
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

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = PROJECT_DIR / "snapshots"
INDEX_FILE = SNAPSHOT_DIR / "index.json"
OUTPUT_FILE = PROJECT_DIR / "rads_dashboard.html"
MAX_SNAPSHOTS = 5

STAGE_NAMES = ["总更新", "需跟进", "已沟通", "已预约", "已就诊", "已活检", "已手术"]
STAGE_FIELDS = [
    None,  # 总更新 = filter Rads_Level>=3
    "是否需要跟进",
    "HCM/PCM 是否与患者发起沟通",
    "是否进行专科预约",
    "是否完成专科问诊",
    "是否活检",
    "是否转化手术",
]

REASON_FIELDS = {
    "无需跟进原因": ("无需跟进原因", "是否需要跟进", "N"),
    "未成功预约原因": ("未成功预约专科/会诊原因", "是否进行专科预约", "N"),
    "未成功沟通原因": ("未成功发起患者沟通原因", "HCM/PCM 是否与患者发起沟通", "N"),
}


def safe_rate(n, d):
    if d == 0:
        return 0.0
    return round(n / d * 100, 2)


def load_snapshot(sid=None):
    if not INDEX_FILE.exists():
        print("❌ 无快照索引"); sys.exit(1)
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        idx = json.load(f)
    if not idx:
        print("❌ 快照索引为空"); sys.exit(1)
    entry = next((e for e in idx if e["snapshot_id"] == sid), None) if sid else idx[-1]
    if not entry:
        print(f"❌ 未找到: {sid}"); sys.exit(1)
    with open(SNAPSHOT_DIR / entry["filename"], "r", encoding="utf-8") as f:
        return json.load(f)


def import_excel(excel_path):
    import pandas as pd
    print(f"\n📂 读取 Excel: {excel_path}")
    df = pd.read_excel(excel_path)
    print(f"   {len(df)} 行 × {len(df.columns)} 列")
    records = []
    for _, row in df.iterrows():
        rec = {}
        for col in df.columns:
            v = row[col]
            if pd.isna(v):
                rec[col] = ""
            elif isinstance(v, pd.Timestamp):
                rec[col] = v.strftime("%Y/%m/%d")
            else:
                rec[col] = str(v).strip()
        records.append(rec)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snap_file = SNAPSHOT_DIR / f"rads_snapshot_{ts}.json"
    snap = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_id": ts,
        "record_count": len(records),
        "source": os.path.basename(excel_path),
        "records": records,
    }
    with open(snap_file, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print(f"   💾 快照: {snap_file.name}")

    index = json.load(open(INDEX_FILE, "r", encoding="utf-8")) if INDEX_FILE.exists() else []
    index.append({"snapshot_id": ts, "filename": snap_file.name, "record_count": len(records),
                   "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    index = index[-MAX_SNAPSHOTS:]
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    for fp in sorted(SNAPSHOT_DIR.glob("rads_snapshot_*.json")):
        if fp.name not in {e["filename"] for e in index}:
            fp.unlink()
    return snap


def compute_metrics(records):
    # filter level >= 3
    level3 = []
    for r in records:
        try:
            if int(float(r.get("Rads_Level", "0"))) >= 3:
                level3.append(r)
        except (ValueError, TypeError):
            pass

    total = len(level3)

    def yn(field):
        return sum(1 for r in level3 if r.get(field, "").strip().upper() == "Y")

    follow = yn("是否需要跟进")
    contact = yn("HCM/PCM 是否与患者发起沟通")
    book = yn("是否进行专科预约")
    visit = yn("是否完成专科问诊")
    biopsy = yn("是否活检")
    surgery = yn("是否转化手术")

    overall = {
        "总更新人数_Ⅲ级及以上": total,
        "需跟进人数": follow,
        "已沟通人数": contact,
        "已预约人数": book,
        "已就诊人数": visit,
        "已活检人数": biopsy,
        "已手术人数": surgery,
    }
    rates = {
        "需跟进率": safe_rate(follow, total),
        "跟进率": safe_rate(contact, follow),
        "预约转化率": safe_rate(book, contact),
        "就诊转化率": safe_rate(visit, book),
        "活检率": safe_rate(biopsy, visit),
        "手术转化率": safe_rate(surgery, visit),
    }

    # by level
    by_level = defaultdict(lambda: defaultdict(int))
    for r in level3:
        lv = r.get("Rads_Level", "").strip()
        if not lv:
            continue
        by_level[lv]["总更新"] += 1
        for sn, sf in zip(STAGE_NAMES[1:], STAGE_FIELDS[1:]):
            if sf and r.get(sf, "").strip().upper() == "Y":
                by_level[lv][sn] += 1

    sorted_levels = sorted(by_level.keys(), key=lambda x: int(float(x)))
    by_level_out = {}
    for lv in sorted_levels:
        by_level_out[lv] = dict(by_level[lv])

    # by type
    by_type = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    all_types = set()
    for r in level3:
        rt = r.get("Rads_Type", "").strip() or "未分类"
        all_types.add(rt)
        lv = r.get("Rads_Level", "").strip()
        # 小计
        by_type[rt]["总体"]["总更新"] += 1
        for sn, sf in zip(STAGE_NAMES[1:], STAGE_FIELDS[1:]):
            if sf and r.get(sf, "").strip().upper() == "Y":
                by_type[rt]["总体"][sn] += 1
        # 按 level
        if lv:
            by_type[rt][lv]["总更新"] += 1
            for sn, sf in zip(STAGE_NAMES[1:], STAGE_FIELDS[1:]):
                if sf and r.get(sf, "").strip().upper() == "Y":
                    by_type[rt][lv][sn] += 1

    type_totals = {t: by_type[t]["总体"].get("总更新", 0) for t in all_types}
    sorted_types = sorted(all_types, key=lambda t: type_totals[t], reverse=True)
    by_type_out = {}
    for t in sorted_types:
        by_type_out[t] = {k: dict(v) for k, v in by_type[t].items()}

    # reasons
    reasons = {}
    for label, (r_field, f_field, f_val) in REASON_FIELDS.items():
        c = Counter()
        for r in level3:
            if f_field and r.get(f_field, "").strip().upper() != f_val.upper():
                continue
            reason = r.get(r_field, "").strip()
            if reason:
                c[reason] += 1
        reasons[label] = dict(c.most_common())

    return {
        "overall": overall,
        "rates": rates,
        "by_level": by_level_out,
        "by_type": by_type_out,
        "reasons": reasons,
    }


def gather_snapshots():
    if not INDEX_FILE.exists():
        return []
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def build_html(data, snap):
    """直接构建完整 HTML，不依赖模板替换"""
    metrics = data["metrics"]
    ov = metrics["overall"]
    rt = metrics["rates"]
    by_level = metrics["by_level"]
    by_type = metrics["by_type"]
    reasons = metrics["reasons"]
    snaps = [e["snapshot_id"] for e in gather_snapshots()]

    # 漏斗卡片
    cards = [
        ("总更新人数<br>(Ⅲ级及以上)", ov["总更新人数_Ⅲ级及以上"], "", "#748FFC"),
        ("需跟进人数", ov["需跟进人数"], f"需跟进率 {rt['需跟进率']}%", "#4C6EF5"),
        ("已沟通人数", ov["已沟通人数"], f"跟进率 {rt['跟进率']}%", "#40C057"),
        ("已预约人数", ov["已预约人数"], f"预约转化率 {rt['预约转化率']}%", "#FAB005"),
        ("已就诊人数", ov["已就诊人数"], f"就诊转化率 {rt['就诊转化率']}%", "#FA5252"),
        ("已活检人数", ov["已活检人数"], f"活检率 {rt['活检率']}%", "#BE4BDB"),
        ("已手术人数", ov["已手术人数"], f"手术转化率 {rt['手术转化率']}%", "#15AABF"),
    ]
    cards_html = ""
    for i, (label, val, rate, color) in enumerate(cards):
        arrow = '<div class="arrow">→</div>' if i < len(cards) - 1 else ""
        cards_html += f"""<div class="stat-card">
      <div class="label">{label}</div>
      <div class="value" style="color:{color}">{val}</div>
      <div class="rate">{rate}</div>
      {arrow}
    </div>"""

    # 总体&分级表格
    level_keys = list(by_level.keys())
    overview_rows = ""

    # 总体系
    overview_rows += '<tr class="highlight"><td><strong>总体</strong></td>'
    for sn in STAGE_NAMES:
        k = sn.replace("人数", "").replace("_Ⅲ级及以上", "")
        v = ov.get("总更新人数_Ⅲ级及以上" if sn == "总更新" else f"{sn}人数", ov.get(sn, 0))
        overview_rows += f"<td>{v}</td>"
    ofu, otot, ocom, obook, ovisit = ov["需跟进人数"], ov["总更新人数_Ⅲ级及以上"] or 1, ov["已沟通人数"], ov["已预约人数"], ov["已就诊人数"]
    overview_rows += f"<td>{safe_rate(ofu,otot)}%</td>"
    overview_rows += f"<td>{safe_rate(ocom,ofu)}%</td>" if ofu else "<td>-</td>"
    overview_rows += f"<td>{safe_rate(obook,ocom)}%</td>" if ocom else "<td>-</td>"
    overview_rows += f"<td>{safe_rate(ovisit,obook)}%</td>" if obook else "<td>-</td>"
    overview_rows += "</tr>"

    # 各级别行
    for lv in level_keys:
        d = by_level[lv]
        overview_rows += f'<tr><td>Level {lv}</td>'
        for sn in STAGE_NAMES:
            sk = sn.replace("人数", "").replace("_Ⅲ级及以上", "")
            mv = d.get(sk, 0)
            overview_rows += f"<td>{mv}</td>"
        fu, tot = d.get("需跟进", 0), d.get("总更新", 1) or 1
        com, book, visit = d.get("已沟通", 0), d.get("已预约", 0), d.get("已就诊", 0)
        overview_rows += f"<td>{safe_rate(fu,tot)}%</td>"
        overview_rows += f"<td>{safe_rate(com,fu)}%</td>" if fu else "<td>-</td>"
        overview_rows += f"<td>{safe_rate(book,com)}%</td>" if com else "<td>-</td>"
        overview_rows += f"<td>{safe_rate(visit,book)}%</td>" if book else "<td>-</td>"
        overview_rows += "</tr>"

    # Type 表格
    type_rows = ""
    for tname in by_type:
        td = by_type[tname]
        lvs = ["总体"] + sorted([k for k in td if k != "总体"], key=lambda x: int(x))
        first = True
        for lk in lvs:
            if lk not in td:
                continue
            d = td[lk]
            type_rows += "<tr>"
            if first:
                type_rows += f'<td rowspan="{len(lvs)}" class="text-left" style="vertical-align:top"><strong>{tname}</strong></td>'
                first = False
            is_total = lk == "总体"
            cls = 'highlight' if is_total else ''
            label = "<strong>小计</strong>" if is_total else f"Level {lk}"
            type_rows += f'<td class="{cls}">{label}</td>'
            for sn in STAGE_NAMES:
                sk = sn.replace("人数", "").replace("_Ⅲ级及以上", "")
                type_rows += f'<td class="{cls}">{d.get(sk, 0)}</td>'
            fu, tot = d.get("需跟进", 0), d.get("总更新", 1) or 1
            com, book, visit = d.get("已沟通", 0), d.get("已预约", 0), d.get("已就诊", 0)
            type_rows += f'<td class="{cls}">{safe_rate(fu,tot)}%</td>'
            type_rows += f'<td class="{cls}">{safe_rate(com,fu)}%</td>' if fu else f'<td class="{cls}">-</td>'
            type_rows += f'<td class="{cls}">{safe_rate(book,com)}%</td>' if com else f'<td class="{cls}">-</td>'
            type_rows += f'<td class="{cls}">{safe_rate(visit,book)}%</td>' if book else f'<td class="{cls}">-</td>'
            type_rows += "</tr>"

    # 原因饼图数据
    reason_chart_js = ""
    for label, rdict in reasons.items():
        if label == "无需跟进原因":
            cid, ctitle = "chart-no-follow-reason", "无需跟进原因"
        elif label == "未成功预约原因":
            cid, ctitle = "chart-no-book-reason", "未成功预约专科/会诊原因"
        else:
            cid, ctitle = "chart-no-contact-reason", "未成功发起患者沟通原因"
        pie_data = json.dumps([{"name": k, "value": v} for k, v in rdict.items()], ensure_ascii=False)
        reason_chart_js += f"""
    (function() {{
      var c = echarts.init(document.getElementById('{cid}'));
      c.setOption({{
        title: {{ text: '{ctitle}', left: 'center', top: 5, textStyle: {{ fontSize: 14 }} }},
        tooltip: {{ trigger: 'item', formatter: '{{b}}: {{c}}人 ({{d}}%)' }},
        series: [{{
          type: 'pie', radius: ['45%','72%'], center: ['50%','55%'],
          label: {{ formatter: '{{b}}\\n{{d}}%', fontSize: 11 }},
          data: {pie_data}
        }}]
      }});
    }})();"""

    snap_opts = '<option value="latest">最新版本</option>'
    for s in snaps:
        snap_opts += f'<option value="{s}">{s}</option>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GZM Rads Tracking — 结节跟进看板</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  :root {{
    --primary: #4C6EF5; --success: #40C057; --warning: #FAB005; --danger: #FA5252;
    --bg: #f5f6fa; --card-bg: #fff; --text: #212529; --text-secondary: #868e96;
    --border: #e9ecef; --radius: 12px; --shadow: 0 2px 12px rgba(0,0,0,.06);
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }}
  .header h1 {{ font-size: 24px; font-weight: 700; }}
  .btn {{ padding: 8px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 500; transition: all .2s; }}
  .btn-outline {{ background: #fff; border: 1px solid var(--border); color: var(--text); }}
  .btn-outline:hover {{ background: #f8f9fa; }}
  .btn-sm {{ padding: 5px 12px; font-size: 12px; }}
  select {{ padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 14px; background: #fff; min-width: 180px; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
  .badge-up {{ background: #d3f9d8; color: #2b8a3e; }}
  .badge-down {{ background: #ffe3e3; color: #c92a2a; }}
  .stats-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .stat-card {{ background: var(--card-bg); border-radius: var(--radius); padding: 16px 20px; box-shadow: var(--shadow); text-align: center; position: relative; }}
  .stat-card .label {{ font-size: 13px; color: var(--text-secondary); margin-bottom: 4px; }}
  .stat-card .value {{ font-size: 28px; font-weight: 700; }}
  .stat-card .rate {{ font-size: 12px; color: var(--text-secondary); margin-top: 2px; }}
  .stat-card .arrow {{ position: absolute; right: 6px; top: 50%; transform: translateY(-50%); color: #adb5bd; font-size: 18px; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
  @media (max-width: 900px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
  .panel {{ background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; margin-bottom: 16px; }}
  .panel-header {{ padding: 14px 20px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 15px; }}
  .panel-body {{ padding: 16px 20px; }}
  .chart-box {{ width: 100%; height: 380px; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 10px 12px; text-align: center; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  th {{ background: #f8f9fa; font-weight: 600; position: sticky; top: 0; }}
  tr:hover td {{ background: #f8f9ff; }}
  .text-left {{ text-align: left; }}
  .highlight {{ background: #edf2ff !important; }}
  .tabs {{ display: flex; gap: 0; border-bottom: 2px solid var(--border); margin-bottom: 0; }}
  .tab {{ padding: 10px 20px; cursor: pointer; font-size: 14px; color: var(--text-secondary); border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all .15s; }}
  .tab:hover {{ color: var(--text); }}
  .tab.active {{ color: var(--primary); border-bottom-color: var(--primary); font-weight: 600; }}
  .mrn-chip {{ display: inline-block; background: #e7f5ff; color: #1971c2; padding: 1px 8px; border-radius: 10px; margin: 1px; font-size: 11px; }}
  .footer {{ text-align: center; padding: 16px; color: var(--text-secondary); font-size: 12px; }}
  .refresh-indicator {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-secondary); }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h1>📊 GZM Rads Tracking — 结节影像跟踪管理看板</h1>
      <div class="refresh-indicator" style="margin-top:4px">
        <span>🕐 数据更新: <strong>{snap['timestamp']}</strong></span>
        <span style="margin-left:12px">📋 总记录: <strong>{snap['record_count']}</strong></span>
      </div>
    </div>
  </div>

  <div class="tabs">
    <div class="tab active" onclick="switchTab('overview')">📈 总体 & 分级</div>
    <div class="tab" onclick="switchTab('type')">🔬 Rads_Type 结节类型</div>
    <div class="tab" onclick="switchTab('reason')">📋 原因分析</div>
    <div class="tab" onclick="switchTab('compare')">🔄 版本对比</div>
  </div>

  <!-- TAB: 总体 & 分级 -->
  <div id="tab-overview" class="tab-content">
    <div class="stats-row" style="margin-top:16px">
      {cards_html}
    </div>
    <div class="panel">
      <div class="panel-header">总体 & 按 Rads_Level 分级 — 转化漏斗详情</div>
      <div class="panel-body">
        <div class="table-wrap">
          <table>
            <thead><tr><th>维度</th><th>总更新</th><th>需跟进</th><th>已沟通</th><th>已预约</th><th>已就诊</th><th>已活检</th><th>已手术</th><th>需跟进率</th><th>跟进率</th><th>预约转化率</th><th>就诊转化率</th></tr></thead>
            <tbody>{overview_rows}</tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB: Rads_Type -->
  <div id="tab-type" class="tab-content" style="display:none">
    <div class="panel" style="margin-top:16px">
      <div class="panel-header">按 Rads_Type × Rads_Level — 转化漏斗详情</div>
      <div class="panel-body">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Rads_Type</th><th>分级</th><th>总更新</th><th>需跟进</th><th>已沟通</th><th>已预约</th><th>已就诊</th><th>已活检</th><th>已手术</th><th>需跟进率</th><th>跟进率</th><th>预约转化率</th><th>就诊转化率</th></tr></thead>
            <tbody>{type_rows}</tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB: 原因分析 -->
  <div id="tab-reason" class="tab-content" style="display:none">
    <div class="grid-2" style="margin-top:16px">
      <div class="panel"><div class="panel-header">❌ 无需跟进原因分布</div><div class="panel-body"><div class="chart-box" id="chart-no-follow-reason"></div></div></div>
      <div class="panel"><div class="panel-header">🚫 未成功预约专科/会诊原因分布</div><div class="panel-body"><div class="chart-box" id="chart-no-book-reason"></div></div></div>
    </div>
    <div class="grid-2">
      <div class="panel"><div class="panel-header">📞 未成功发起患者沟通原因</div><div class="panel-body"><div class="chart-box" id="chart-no-contact-reason"></div></div></div>
    </div>
  </div>

  <!-- TAB: 版本对比 -->
  <div id="tab-compare" class="tab-content" style="display:none">
    <div class="panel" style="margin-top:16px">
      <div class="panel-header">版本对比（需至少2个快照）</div>
      <div class="panel-body">
        <div class="table-wrap"><table id="table-compare"><tbody><tr><td colspan="6" style="color:#868e96">（接入真实数据并保存两个快照后自动展示）</td></tr></tbody></table></div>
      </div>
    </div>
  </div>

  <div class="footer">GZM Rads Tracking Management — 结节影像跟踪周报 | IOC看板</div>
</div>

<script>
// Tab switch
function switchTab(name) {{
  document.querySelectorAll('.tab').forEach(function(t) {{ t.classList.remove('active'); }});
  document.querySelectorAll('.tab-content').forEach(function(t) {{ t.style.display = 'none'; }});
  document.querySelector('.tab[onclick*=\"'+name+'\"]').classList.add('active');
  document.getElementById('tab-' + name).style.display = 'block';
  // Resize all charts
  setTimeout(function() {{
    ['chart-no-follow-reason','chart-no-book-reason','chart-no-contact-reason'].forEach(function(id) {{
      var el = document.getElementById(id);
      if (el && el._echart_instance_) el._echart_instance_.resize();
    }});
  }}, 100);
}}

// Render reason charts
function renderCharts() {{
  {reason_chart_js}
}}

// Init
document.addEventListener('DOMContentLoaded', function() {{
  renderCharts();
}});
window.addEventListener('resize', function() {{
  ['chart-no-follow-reason','chart-no-book-reason','chart-no-contact-reason'].forEach(function(id) {{
    var el = document.getElementById(id);
    if (el && el._echart_instance_) el._echart_instance_.resize();
  }});
}});
</script>
</body>
</html>"""

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 看板已生成: {OUTPUT_FILE}")
    return OUTPUT_FILE


def main():
    parser = argparse.ArgumentParser(description="GZM Rads Tracking 看板生成 v2")
    parser.add_argument("--snapshot", type=str, default=None)
    parser.add_argument("--excel", type=str, default=None)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    if args.excel:
        snap = import_excel(args.excel)
    else:
        snap = load_snapshot(args.snapshot)

    print(f"\n📂 快照: {snap['snapshot_id']}")
    print(f"   记录数: {snap['record_count']}")

    print("\n🔢 计算指标...")
    metrics = compute_metrics(snap["records"])

    ov = metrics["overall"]
    rt = metrics["rates"]
    print(f"   总更新(Ⅲ级+): {ov['总更新人数_Ⅲ级及以上']}")
    print(f"   需跟进: {ov['需跟进人数']} ({rt['需跟进率']}%)")
    print(f"   已沟通: {ov['已沟通人数']} ({rt['跟进率']}%)")
    print(f"   已预约: {ov['已预约人数']} ({rt['预约转化率']}%)")
    print(f"   已就诊: {ov['已就诊人数']} ({rt['就诊转化率']}%)")
    print(f"   已活检: {ov['已活检人数']} ({rt['活检率']}%)")
    print(f"   已手术: {ov['已手术人数']} ({rt['手术转化率']}%)")
    print(f"\n   Rads_Level: {list(metrics['by_level'].keys())}")
    for lv, d in metrics["by_level"].items():
        print(f"     Level {lv}: 总更新={d.get('总更新',0)}, 需跟进={d.get('需跟进',0)}")
    print(f"\n   Rads_Type: {list(metrics['by_type'].keys())}")

    data = {
        "timestamp": snap["timestamp"],
        "record_count": snap["record_count"],
        "snapshot_id": snap["snapshot_id"],
        "metrics": metrics,
        "snapshots": [e["snapshot_id"] for e in gather_snapshots()],
    }

    out = build_html(data, snap)

    if args.open:
        webbrowser.open(str(out))


if __name__ == "__main__":
    main()
