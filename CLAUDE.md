# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## 会话规则

1. **称呼**：每次回复前必须以"六六"作为称呼
2. **决策确认**：遇到不确定的代码设计问题时，必须先询问六六，不得直接行动
3. **部署决策**：凡是涉及部署方案的问题（服务器选型、网络配置、数据库、安全策略、运维方式），必须先列出可选项并询问六六，不得自行决定。部署前必须确认以下五个维度：

### 部署五维度检查清单

| 维度 | 要决策的问题 | 常见可选项 |
|------|------------|-----------|
| **1. 是否 24h 运行** | 服务器需要全天候跑还是按需启动？ | 24h 常开 / 定时开关 / 手动启动 |
| **2. 谁能访问** | 只有内网能访问？还是外网也能？ | 仅本机 / 局域网 / 外网（需公网IP或云服务器） |
| **3. 数据放哪** | 数据库和文件存在哪里？ | SQLite 本地文件 / MySQL/PostgreSQL / 飞书 Bitable / 纯 CSV/JSON |
| **4. 多人还是单人** | 一个人用还是多人同时用？ | 单人（文件级够用） / 多人（需要 Web 服务 + 数据库） |
| **5. 更新方式** | 代码/数据更新了怎么让线上生效？ | 手动替换文件 / Git pull + 重启 / CI/CD 自动部署 |

4. **项目汇报**：完成一个项目/阶段后，主动撰写汇报文档（`project_report.md`），置于 `pipeline_output/` 目录下。汇报规则如下：

### 3.1 汇报风格
- **结果导向**：先说产出和效果，再讲过程
- **价值导向**：每个模块都回答"这带来什么价值"
- **指标导向**：用数字说话，含基线→目标→实际，达成率百分比
- **未来规划**：明确下一步做什么、优先级、依赖
- **DDL 导向**：每个待办项标注预计完成时间

### 3.2 汇报结构

#### Why — 背景与痛点
- 项目要解决什么问题？
- 现状的痛点是什么？（人工排班耗时？覆盖不均？效益不可量化？）
- 不做会怎样？做成了会怎样？

#### What — 场景与产出
- **使用场景**：谁、在什么时候、怎么用这个系统？（例如：每月25日，排班主管运行流水线生成下月排班表）
- **最终产出**：Excel 排班表 + HTML 仪表盘 + 预测图表，具体列出文件清单
- **输入→输出流程图**：飞书原始数据 → 清洗后 CSV → 预测 CSV → 排班 Excel/HTML

#### How — 工作流与核心技术
- **流水线架构**：四阶段总览图（fetch → forecast → dashboard → schedule）
- **每个阶段**：用的模型/算法（Prophet、LightGBM、CP-SAT）、核心逻辑（不超过5个要点）、关键参数
- **不深入细节**但标注关键代码文件位置（文件名+行号）

#### 交付成果 — 做什么 & 不做什么
- **能力清单**：系统能干什么（预测需求量、生成排班表、可视化仪表盘）
- **边界清单**：系统不能干什么（例如：不处理临时调班、不实时同步飞书、不支持多院区）
- **使用说明**：运行命令、所需环境、输入格式要求

#### 价值衡量 — 指标 & 达成率
- 定义核心 KPI（例如：排班耗时降低 X%、人员利用率提升 Y%、需求覆盖率 Z%）
- 每个指标标注：**基线值**（优化前）→ **目标值** → **实际值** → **达成率%**
- 未达标的指标说明原因和改进方案

#### 未来规划 — 可优化点 & 动态整改
- **优化点列表**，每条包含：优先级（P0/P1/P2）、当前瓶颈是什么、建议改进方案、预期收益、预计完成时间
- 已识别但未解决的技术债或功能缺口

#### 动态整改 — 预测 vs 实际差距追踪
- **差距分析**：对比模型预测值与实际运营数据的偏差（例如：预测需求 vs 实际检查量、预测工时 vs 实际工时），量化偏差率
- **根因归类**：偏差来源是模型误差？数据漂移？业务规则变更？外部因素（节假日、疫情、政策）？
- **整改措施**：针对每个差距采取了什么动作（例如：调整模型超参、引入新特征、修正班型约束、更新数据源）
- **整改效果**：措施生效后偏差率的变化（整改前 X% → 整改后 Y%）
- **闭环节奏**：多久做一次差距回顾（例如：每月排班上线后 1 周内回顾、每季度深度复盘）

---

## 项目概述

广州某医院放射科/超声科的**排班优化系统**。从飞书 Bitable 拉取历史检查数据，用 Prophet + LightGBM 预测未来日/小时级需求量，再通过三阶段 CP-SAT（OR-Tools）求解器生成最大化效益的排班表，最终输出 Excel + 交互式 HTML 仪表盘。

---

## 技术栈

- **Python 3.12+**（无虚拟环境，系统级 pip）
- **数据**: pandas, numpy, openpyxl
- **预测**: Prophet (Facebook, `fbprophet`→`prophet`), LightGBM
- **优化**: OR-Tools CP-SAT (`ortools.sat.python.cp_model`)
- **可视化**: Plotly, Matplotlib, 内联 HTML/JS
- **外部集成**: 飞书开放平台 API（Bitable 数据源）, requests

---

## 四阶段流水线

```
运行入口: run_pipeline.py  (总控，~290行)
  │
  ├── Step 1: fetch_feishu_data.py    飞书 Bitable → cleaned_output.csv
  ├── Step 2: prophet_lightGBM.py     预测 → forecast_output/*.csv + *.png
  ├── Step 3: generate_dashboard.py   预测仪表盘 → forecast_output/dashboard.html
  └── Step 4: schedule.py            CP-SAT 排班 → Schedule_*.xlsx + *.html
       │
       └── collect_outputs() → pipeline_output/
```

### 关键设计决策

- **松耦合**：每个子脚本通过 `subprocess.run()` 独立调用，不 import。好处：隔离各自的 argparse、全局变量和崩溃影响；失败 exit code ≠ 0 时立即 `sys.exit()` 终止流水线
- **文件协议**：子脚本之间通过 CSV 文件通信，不通过内存或管道。`cleaned_output.csv` 是 Step1→Step2 的契约，`Demand_Forecast_Hourly.csv` 是 Step2→Step4 的契约
- **路径约定**：所有脚本硬编码同目录相对路径（`SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))`），`run_pipeline.py` 通过 `cwd=SCRIPT_DIR` 保证子进程工作目录一致

---

## 常用命令

```bash
# 完整流水线（需要飞书凭据 + 网络）
python run_pipeline.py

# 调试模式：只拉 2000 条 + 跳过飞书人员 API + 60s 求解
python run_pipeline.py --sample 2000 --no-feishu --solver-time 60

# 已有 cleaned_output.csv，从预测开始
python run_pipeline.py --skip-fetch --no-feishu

# 已有完整 forecast_output/，直接排班
python run_pipeline.py --skip-forecast --no-feishu

# 指定排班月份 + 延长求解时间
python run_pipeline.py --month 2026-07 --solver-time 600 --no-feishu

# 单独运行各步骤
python fetch_feishu_data.py --sample 2000
python prophet_lightGBM.py
python generate_dashboard.py
python schedule.py --month 2026-07 --no-feishu --solver-time 300
```

### 飞书凭据

环境变量 `FEISHU_APP_ID=cli_aaa8d24639b8dcd8` / `FEISHU_APP_SECRET=b0ayVQKIuUGmvzRu9YCm9gpZHUzniNz1`。3 个脚本 (`fetch_feishu_data.py`, `schedule.py`, `prophet_lightGBM.py`) 已统一硬编码此凭据作为默认值，可直接运行无需每次设置环境变量。也可通过 `--app-id` / `--app-secret` 命令行覆盖。

---

## schedule.py 排班引擎核心逻辑

约 2400 行，是整个系统最复杂的模块。三条角色线：`放射医生`、`放射技师`、`B超医生`，各自有不同的班型池和覆盖约束。

```
Phase 1-2: HC 需求计算 + L/N 预分配
Phase 3:   Stage 1 — CP-SAT 分配 80% 工时池（目标 140.8h/人）
Phase 4:   Stage 2 — CP-SAT 分配剩余 20% 工时池
Phase 5:   Stage 3 — CP-SAT 备班池全覆盖（硬约束，必须覆盖所有需求）
Phase 6:   On-Call 分配 + Dustin 跨角色处理
           → Excel 输出（条件格式、冻结窗格、分类着色）
           → HTML 可视化仪表盘
```

关键约束：
- 放射技师 24h 覆盖（夜班 + L/N 长班），备班按小时计（无班型）
- B超医生每日总人数 ≤ 5，无夜班
- Dustin 角色：放射优先（一/三/五优先）→ 剩余支持超声 → 只抵扣 0.5 HC
- 每阶段 CP-SAT 求解时间默认 120s（单阶段），可通过 `--solver-time` 覆盖

---

## 文件命名注意事项

- 排班脚本已从 `3.2 schedule最大效益化.py` 重命名为 `schedule.py`，`run_pipeline.py` 中所有引用已同步修正
- `备选_ prophet_forecast.py` 和 `备选_ prophet_forecast_MAPE.py` 是备选方案，不参与主流水线
- `regenerate_dashboard.py` 是仪表盘重新生成工具，独立使用
- `dashboard_template.html` 是仪表盘的 HTML 模板，`generate_dashboard.py` 向其注入数据
- `需求更新.pdf` 和 `近期影像科数据.xlsx` 是需求文档和原始数据参考
- `cols_dump.txt` 是调试用的列名导出
