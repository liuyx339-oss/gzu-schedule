# GZU 放射/超声 排班优化系统 — 完整技术文档

> 更新日期: 2026-06-19

---

## 一、项目构成

### 1.1 文件清单

| 文件 | 类型 | 用途 |
|------|------|------|
| `run_pipeline.py` | **总控入口** | subprocess 串联全流程 |
| `fetch_feishu_data.py` | 数据拉取 | 从飞书 Bitable 拉历史临床数据 → `cleaned_output.csv` |
| `forecast_core.py` | 预测引擎 | Prophet 60% + LightGBM 40% 混合预测，6步分层流水线 |
| `prophet_lightGBM.py` | 预测入口 | 月度预测主脚本，读 CSV → 调 forecast_core → 输出按月份命名的预测文件 |
| `weekly_forecast.py` | 周预测 | 从月度预测 CSV 切当周 Mon-Sun → 生成 `forecast_data.json` |
| `schedule.py` | **排班引擎** | CP-SAT 三阶段排班，~3000行，含全部约束逻辑 |
| `generate_dashboard.py` | 仪表盘 | 读预测 CSV 注入 HTML 模板 |
| `feishu_bot.py` | 机器人 | 每日拉飞书预约表 → 推送群卡片 |
| `daily_compare.py` | 对比 | Bot 真实预约 vs 月度预测，3类别 |
| `deploy.py` | 部署 | 打包排班 Excel 到 `deploy_package/` |
| `regenerate_dashboard.py` | 工具 | 从 Excel 重新生成排班 HTML |

### 1.2 自动化

| 工作流 | 触发 | 北京时间 |
|------|------|------|
| `daily-report.yml` | cron + 时间门控 | 16:30 |
| `weekly-forecast.yml` | 每周一 cron | 08:00 |
| `monthly-schedule.yml` | 每月25日 cron | 16:30 (备用) |

### 1.3 线上页面

| 页面 | 链接 | 说明 |
|------|------|------|
| 首页 | `/` | 2卡片导航 |
| 需求日报 | `/dashboard.html` | 真实预约 + 预测趋势 + 对比 |
| 月度排班 | `/schedule.html` | CP-SAT排班（默认最新），月份下拉框切换 |

---

## 二、预测引擎

### 2.1 数据来源

飞书多维表格 — 临床主数据表 (`NjSdbaToNavBlksS6AecPd7rnrb`)，`fetch_feishu_data.py` 拉取后清洗写入 `pipeline_output/cleaned_output.csv`。

数据列：患者到达时间(ms UTC)、就诊科室、医嘱描述、Type、预估操作时长/医生报告时长、大分类(超声/放射)。

### 2.2 预测流水线

```
run_forecast_pipeline(df, forecast_days, forecast_start)
  │
  STEP 0: 数据清洗
    - UTC ms → Asia/Shanghai datetime，hour floor → ds 列
    - VALID_TYPES 过滤 (CT/MRI/X-ray/Ultrasound/DXA/Mammogram/Echocardiograms/骨龄测评/床边穿刺消融)
    - 大分类: Ultrasound→"超声"，其余→"放射"
  │
  STEP 1: forecast_total_type()
    按 Type 各自聚合小时数 → hybrid_forecast() → Type 总量预测
  │
  STEP 2: forecast_key_departments()
    6个关键科室独立跑 hybrid_forecast (HC/HC体检日/OBGYN/Family/Ortho/Internal)
  │
  STEP 2.5: scale_key_to_total()
    关键科室和 > Type总量 → 等比缩放(HCl -> total×scale)
  │
  STEP 3: build_remaining_pool()
    余量 = Type总量 - 关键科室(缩放后)和
  │
  STEP 4: allocate_remaining_pool()
    余量按 (weekday, hour, Type) 历史比例分配给非关键科室
  │
  STEP 5: split_order_items()
    科室级 → 按 item_ratio 拆到具体检查项目 (例: HC Ultrasound 08:00 → Duplex scan 15.9% / Pelvic 3.3% ...)
  │
  STEP 6: translate_workload()
    项目数 × 标准时长 = pred_tech_minutes + pred_doc_minutes
    NaN兜底: 缺失项 × 20/10min
```

### 2.3 hybrid_forecast() — Prophet 60% + LightGBM 40%

```
1. 补全空白小时 (reindex, fill=0)
2. 时间特征: hour, weekday, month, is_weekend, hour_sin, hour_cos
3. Prophet: growth=linear, daily+weekly seasonality, 中国节假日, seed=42
4. LightGBM: N=200, lr=0.05, max_depth=6, subsample=0.8, random_state=42
   特征: hour, weekday, month, is_weekend, hour_sin/cos, lag_1, lag_24, rolling_6h
5. 合成: pred = Prophet×0.6 + LGBM×0.4
6. 仅未来段: ds > 历史最新数据点
```

**随机种子**: `np.random.seed(42)` + LGBM `random_state=42`，确保每次运行一致。

### 2.4 HC 部门清理

预测完成后按 `WEEKDAY_DEPT_FILTER` 删除不匹配的 HC 变体：
- 周一/三/五/六 → 只保留 `GZU Health Management Center 体检日`
- 周二/四/日 → 只保留 `GZU Health Management Center`

### 2.5 指定月份预测

`prophet_lightGBM.py --month YYYY-MM`:
- 训练数据: 全部 `cleaned_output.csv`
- `forecast_days` = gap(数据截止→目标月初) + 目标月天数
- `forecast_start` = 目标月1号 0:00 — 只保留该月预测行
- 输出: `Demand_Forecast_YYYY-MM_Hourly.csv` + `_Daily.csv`

**示例**: 数据到 6/17，跑7月 → gap=13天 + 31天 = 44天预测，过滤后只输出7月31天。

---

## 三、排班引擎 (schedule.py)

### 3.1 人员模型

从飞书人员表拉取 (`MiRrw2dILig6I2k7wU7ceV0on9e` / `tbl8f0tku6yPwc2V`):

| 字段 | 含义 |
|------|------|
| 姓名 | 人员名字 |
| 科室 | 放射 / 超声 |
| 类型 | 医生 / 技师 |
| 雇佣形式 | 全职 / 兼职 / 备班 |

**当前人员 (33人)**:

| 角色 | 全职 | 兼职 | 备班 |
|------|------|------|------|
| 放射医生 | li zhenhuan, Dustin Huang (2人) | 8人 | 放射医生备班 |
| 放射技师 | 6人 | 3人(仅展示) | 放射技师备班 |
| B超医生 | 4人 | 7人(仅展示) | 超声医生备班 |

**Dustin 跨角色**: `Dustin Huang`(放射) + `Dustin Huang (US)`(超声展示)。

### 3.2 班型

| 类型 | 班次 | 时长 | 可用角色 |
|------|------|------|---------|
| 全天白班 | D/D1-D6/C/C1/L | 8.0-12.0h | 全部 |
| 半天 | H1/H2/H3 | 4.0h | 放射技师+B超 |
| 夜班 | N/N2/N3 | 14.0-14.5h | 放射技师+放射医生兼职 |
| 24h | L/N | 24.0h | 放射医生+放射技师 |

### 3.3 月目标工时

```python
工作日 = 当月周一~周五天数
TARGET_HOURS_FULL = 工作日 × 8h
TARGET_HOURS_80   = TARGET_HOURS_FULL × 0.8
```

| 月份 | 工作日 | 目标 |
|------|--------|------|
| 6月 | 22天 | 176h |
| 7月 | 23天 | 184h |

### 3.4 角色约束

| | 放射医生 | 放射技师 | B超医生 |
|------|---------|---------|---------|
| 白天最小值 | ≥1 (交替优先) | =2 (硬无slack) | ≥2 (Sun=2) |
| 全职夜班 | 0 | =1 (硬无slack) | 0 |
| 兼职夜班 | 1/天 (8人轮转) | — | — |
| L/N | 2个/人/月 | 2个/人/月 | 无 |
| 仪器限制 | — | ≤3人/天 | ≤4人/天 |
| 下午约束 | — | — | Tue+Thu=3, Wed+Fri=2 |
| 配对禁止 | — | — | 禁止(Liu+Lu),(Liu+Hou) |
| OnCall | ❌ | ✅ 6人轮转 | ✅ 4US+Dustin |
| 备班模式 | 整班 | 按小时 | 整班 |

### 3.5 HC需求转换 (Phase 1)

```
分钟数 → HC:
  1. 超声合并: doc += tec, tec = 0
  2. 体检中心 doc 推迟: HC doc 均分 8:00-17:00
  3. <5分钟合并到下一小时
  4. HC = 分钟 ÷ (60 × 负荷率), 容忍度 25min, 仅 7:00-18:00
  5. 峰值平滑: 上午(7-12)/下午(13-18)各6h, 尖刺<3h→cap
```

### 3.6 L/N 预分配 (Phase 2)

每位放射医生/技师每月 2 个 24h 班，均匀间隔分布：`n_persons × 2 ÷ n_days`。

### 3.7 主流程 (main())

```
main()
  ├─ Phase 1: HC 需求转换
  ├─ Phase 2: L/N 预分配
  ├─ Phase 3a: Stage 1 — 放射医生 + 放射技师
  ├─ Dustin 超声 HC 抵扣 (上放射的日子 -0.5)
  ├─ Phase 3b: Stage 1 — B超医生 (用抵扣后需求)
  ├─ Phase 4: Stage 2 — 全部角色填到 100%
  ├─ Phase 5: Stage 3 — 备班全覆盖
  ├─ Phase 6: merge + OnCall + 半天合并 + 80/20分离 + 工时帽
  ├─ Phase 7: Dustin 跨角色 + 超声楼层轮转 (B1/4/9)
  ├─ Phase 8: Excel 输出 ← Schedule_YYYY-MM_V3.xlsx
  └─ Phase 9: HTML 仪表盘 ← 同时归档 publish/schedule_YYYY-MM.html
```

### 3.8 放射医生排班逻辑

```
Stage 1 — 硬需求:
  每天 ≥1 全职白班 (li/Dustin 软交替, 同天惩罚 -100K)
  全职 0 夜班
  Dustin Wed+Fri 极高奖励 (P0级, 软约束)
  L/N × 2
  兼职每天 1 夜班 (8人轮转, slack 兜底→备班)
  月工时 ≤ 目标
  目标: max(工时 - 超时惩罚 - 同天惩罚 + DustinWF奖励 + 均衡)

Stage 2: 填到 176h
Stage 3: 备班整班模式覆盖剩余需求 + 夜班
```

### 3.9 放射技师排班逻辑

```
Stage 1 — 硬需求:
  每天 = 2 全职白班 (硬)
  每天 = 1 全职夜班 (硬)
  ≤ 3 人/天 (仪器)
  L/N × 2
  24h 覆盖 ≥1 (极高惩罚 slack)
  需求覆盖 (极高惩罚 slack)
  月工时 ≤ 目标

Stage 2: 填到 176h, 夜班 ≤1
Stage 3: 备班按小时模式全覆盖
```

### 3.10 B超医生排班逻辑

```
Stage 1 — 硬需求:
  全天白班 ≥2 (Sun=2)
  ≤4 人/天
  Tue+Thu PM = 3 (全天班或H3)
  Wed+Fri PM = 2
  禁止 Liu+Lu, Liu+Hou
  月工时 ≤ 目标

Stage 2: 填到 176h, PM 约束保持
Stage 3: 备班整班模式全覆盖

HTML 生成前硬切:
  全天不足 → 半天→D 提拔 → 休班医生拉入
  PM不足 → H2→D → 休班医生拉入
```

### 3.11 OnCall 分配

**放射技师**: 6人轮转，优先当天休息的人。

**B超医生**: 5人 (4 US + Dustin)
```
每天：
  1. 优先休息的 US 医生 → 选 OnCall 最少者
  2. 全部上班 + Dustin 在放射 → Dustin 补位
  3. 全部上班 → US 中 OnCall 最少者
```

### 3.12 80/20 分离 + OT 列

```
累计工时 ≤ 140.8h → 标 "80%"
累计工时 > 140.8h → 标 "20%"
累计工时 > TARGET_HOURS_FULL → 不标 80/20, 工时计为 OT
```

排班表显示: `80%列 | 20%列 | OT列 | 目标列`，80%+20% ≤ 目标。

### 3.13 楼层轮转

每天有全天白班的 B超医生：
- 先分配 1 人 B1 (每日强制)
- 再分配 4(四楼) 9(九楼) — 贪婪算法，选该楼层累计最少的人

### 3.14 月度 CSVs + HTML 归档

```
pipeline_output/
  Demand_Forecast_2026-06_Hourly.csv    ← 6月预测
  Demand_Forecast_2026-07_Hourly.csv    ← 7月预测
  schedule/
    Schedule_2026-06_V3.xlsx
    Schedule_Dashboard_2026-06_V3.html
    Schedule_Dashboard_2026-07_V3.html

publish/
  schedule.html             ← 最新月份
  schedule_2026-06.html     ← 6月归档
  schedule_2026-07.html     ← 7月归档
  monthly_forecast_hourly.csv  ← 最新月份 (CI用)
```

---

## 四、操作指南

### 4.1 每月排班（最重要）

**你要做的**：

```bash
cd "D:\PythonWorkspace\GZU_Analysis_Test (2)"

# 一条命令: 拉数据 + 预测 + 排班
python run_pipeline.py --month 2026-07 --no-feishu --solver-time 120
```

**发生了什么**：
1. 拉最新飞书数据 → `cleaned_output.csv`
2. 预测 7 月 → `Demand_Forecast_2026-07_Hourly.csv`
3. 排班 → `Schedule_Dashboard_2026-07_V3.html`
4. 自动归档到 `publish/schedule_2026-07.html`

**如果不想拉数据**（飞书没变化）：
```bash
python run_pipeline.py --month 2026-07 --skip-fetch --no-feishu --solver-time 120
```

**如果连预测都不想重跑**（已有预测 CSV）：
```bash
python run_pipeline.py --month 2026-07 --skip-fetch --skip-forecast --no-feishu --solver-time 120
```

**跑完推送上线**：
```bash
cp pipeline_output/schedule/Schedule_Dashboard_2026-07_V3.html publish/schedule.html
git add publish/ && git commit -m "7月排班" && git push
```

### 4.2 每日推送（自动）

每天 16:30 GitHub Actions 自动运行：
1. `feishu_bot.py` 拉明天预约 → 推飞书群卡片
2. `daily_compare.py` 对比真实 vs 预测
3. 部署到 GitHub Pages

无需手动操作。

### 4.3 每周预测更新（自动）

每周一 08:00 GitHub Actions 自动运行：
- `weekly_forecast.py` 从月度 CSV 切当周 Mon-Sun → `forecast_data.json`

无需手动操作。

### 4.4 排班表线上查看

| 页面 | 地址 |
|------|------|
| 首页 | `https://liuyx339-oss.github.io/gzu-schedule/` |
| 排班表 | `https://liuyx339-oss.github.io/gzu-schedule/schedule.html` |
| 需求日报 | `https://liuyx339-oss.github.io/gzu-schedule/dashboard.html` |

密码: `gzu2026`

排班表页面顶部下拉框切换月份。

### 4.5 排班表功能

| 功能 | 说明 |
|------|------|
| 🔐 密码 | gzu2026 |
| 🔀 三角色切换 | 放射医生/放射技师/B超医生, Tab+下拉框同步 |
| 📊 工时统计 | 80%池/20%池/OT/备班/LN/总工时/目标/OnCall |
| 🎨 分类着色 | 80%(黑)/20%(蓝框sup)/LN(橙粗框)/备班(红虚线) |
| 🏥 楼层备注 | B超 [4]/[9]/[B1] |
| 📞 OnCall | 右下📞标记 |
| 📈 日详情 | 点击日期→HC 需求柱状图 |
| ✏️ 编辑 | 点格子→选班型(PTO/CTO/OFF/空白)→保存GitHub |
| 📝 备注需求 | CRUD → GitHub API |
| 🗓️ 日期星期 | 表头"06月01日 一" |
| 🔄 月份切换 | 顶栏下拉框自动检测归档 |

---

## 五、关键常量

### SHIFT_DICT

| Shift | 时间 | Hours |
|-------|------|-------|
| D | 08:30-17:30 | 8.5 |
| D1-D6 | 变体 | 8.0-9.0 |
| C/C1 | 变体 | 8.0 |
| L | 08:00-20:00 | 12.0 |
| H1 | 07:40-11:40 | 4.0 |
| H2 | 08:30-12:30 | 4.0 |
| H3 | 13:30-17:30 | 4.0 |
| N | 17:30-08:00 | 14.5 |
| N2/N3 | 变体 | 14.0 |
| L/N | 08:00-08:00 | 24.0 |

### CP-SAT 权重

| 权重 | 值 | 用途 |
|------|-----|------|
| S1_COVERAGE_WEIGHT | 1,000,000 | Stage1 slack惩罚 |
| S1_BALANCE_WEIGHT | 500 | 工时均衡惩罚 |
| S2_COVERAGE_WEIGHT | 1,000,000 | Stage2 slack惩罚 |
| S3_BACKUP_MINIMIZE | 1,000 | 备班最小化 |

---

## 六、飞书数据源

| 用途 | Base | Table |
|------|------|-------|
| 临床主数据 | `NjSdbaToNavBlksS6AecPd7rnrb` | `tbl9camXrcKz4qhZ` |
| 人员表 | `MiRrw2dILig6I2k7wU7ceV0on9e` | `tbl8f0tku6yPwc2V` |
| 体检预约(机器人) | `NM6HbB8gKaqtDysTTrRcve0ZnAc` | `tblrUOmKxEmHxCxa` |
| OB预约(机器人) | `XDa9w6qGBigqGNkOENvctCJtnqd` | `tbltb1eix0QOEcQP` |

机器人推送群: `oc_8533fa82a288a4468436873deb02d359`
