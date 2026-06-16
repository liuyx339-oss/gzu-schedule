# run_pipeline.py — 全流程编排脚本代码逻辑详解

> 对应文件: `run_pipeline.py` (约290行)

## 一、概述

`run_pipeline.py` 是放射/超声排班系统的**总控脚本**。它的职责只有一个：**按顺序串联全部4个子脚本，并将所有产物统一收集到 `pipeline_output/` 目录**。

### 设计原则

- **松耦合**：每个子脚本独立运行，`run_pipeline` 只负责调用和收集产物
- **失败即停**：任一步骤失败(exit code≠0)，立即 `sys.exit()` 终止，不继续执行
- **产物集中**：全部输出复制到 `pipeline_output/` 单目录下，方便分发和归档
- **透传参数**：`run_pipeline` 不解析子脚本的业务逻辑，仅透传命令行参数

---

## 二、完整流程

```
Step 1: fetch_feishu_data.py
   输入: 飞书 Bitable API
   输出: cleaned_output.csv
         ↓
Step 2: prophet_lightGBM.py
   输入: cleaned_output.csv
   输出: forecast_output/*.csv, forecast_output/*.png
         ↓
Step 3: generate_dashboard.py
   输入: forecast_output/ (CSV数据)
   输出: forecast_output/dashboard.html
         ↓
Step 4: schedule.py (或 schedule.py)
   输入: forecast_output/Demand_Forecast_Hourly.csv
   输出: Schedule_YYYY-MM_V3.xlsx, Schedule_Dashboard_YYYY-MM_V3.html
         ↓
collect_outputs()
   → pipeline_output/
```

---

## 三、核心函数详解

### 3.1 `run_step(step_label, cmd)`

```python
def run_step(step_label: str, cmd: list[str]) -> None:
```

**功能**: 运行一个子进程步骤。

**实现细节**:
- 打印分隔线和步骤标题
- 调用 `subprocess.run(cmd, cwd=SCRIPT_DIR)` — `cwd` 统一设为项目根目录
- **成功(exit code=0)**: 静默返回，继续下一步
- **失败(exit code≠0)**: 打印 `[ERROR]` 和退出码，`sys.exit(result.returncode)` 终止整个流水线

**为什么用 subprocess 而不是 import?**
- 每个子脚本有自己的 `argparse`、全局变量、`if __name__ == "__main__"` 入口
- subprocess 保证隔离：一个脚本的全局状态不影响下一个
- 失败即停的语义清晰——exit code≠0 直接终止

### 3.2 `_copy_file(src, dst_dir, label)`

```python
def _copy_file(src: str, dst_dir: str, label: str) -> bool:
```

**功能**: 复制单个文件到目标目录，保留时间戳。

**实现细节**:
- `shutil.copy2(src, dst)` — 保留文件时间戳（比 `shutil.copy` 多保留元数据）
- 文件存在 → 打印 `[OK]`，返回 True
- 文件不存在 → 打印 `[WARN]` 并**跳过**（不中断，有些产物可能未被生成），返回 False

**容错设计**: 预测图表 PNG 的文件名因月份变化可能不匹配 glob pattern，`_copy_file` 的 `[WARN]` 跳过机制保证流水线不会因此崩溃。

### 3.3 `collect_outputs(schedule_xlsx, schedule_html)`

```python
def collect_outputs(schedule_xlsx: str | None = None, schedule_html: str | None = None) -> None:
```

**功能**: **收集阶段**——所有子脚本执行完毕后，统一复制到 `pipeline_output/`。

**完整流程**:

```python
# 1. 清空 pipeline_output/
if os.path.exists(OUTPUT_DIR):
    shutil.rmtree(OUTPUT_DIR)   # 完全删除旧目录
os.makedirs(OUTPUT_DIR)         # 重建空目录

# 2. 复制 cleaned_output.csv → pipeline_output/
_copy_file(CLEANED_CSV, OUTPUT_DIR, "cleaned_output.csv")

# 3. 复制 forecast_output/ 全部内容 → pipeline_output/forecast_output/
fc_dst = os.path.join(OUTPUT_DIR, "forecast_output")
os.makedirs(fc_dst, exist_ok=True)

# 3a. CSV文件列表(4个)
for csv_name in ["Demand_Forecast_Hourly.csv", "Demand_Forecast_Daily.csv",
                 "Real_Daily_Summary.csv", "Forecast_Daily_Summary.csv"]:
    _copy_file(src, fc_dst, ...)

# 3b. PNG图表(glob匹配 *_Heatmap.png, *_Trend.png)
for pattern in FORECAST_CHART_PATTERNS:
    for fpath in glob_mod.glob(os.path.join(FORECAST_DIR, pattern)):
        _copy_file(fpath, fc_dst, ...)

# 3c. 预测仪表盘HTML
_copy_file("forecast_output/dashboard.html", fc_dst, ...)

# 4. 复制排班结果 → pipeline_output/schedule/
schedule_dst = os.path.join(OUTPUT_DIR, "schedule")
os.makedirs(schedule_dst)
# 优先使用传入路径，否则glob匹配脚本根目录下的 Schedule_*_V3.*
```

**设计细节**:
- `schedule_xlsx` 和 `schedule_html` 优先使用传入参数（主流程中通过 `glob` 匹配最新文件后传入）
- 若未传入，fallback 到 `glob` 在脚本根目录搜索 `Schedule_*_V3.xlsx` 和 `Schedule_Dashboard_*_V3.html`

---

## 四、`main()` 执行流程详解

### Step 1: 飞书数据提取

```
条件: 未设置 --skip-fetch 且未设置 --skip-forecast
命令: python fetch_feishu_data.py [透传参数]
透传: --wait-refresh, --check-freshness, --sample, --app-id, --app-secret

跳过时: 检查 cleaned_output.csv 是否存在
        → 存在: 继续 | 不存在: sys.exit(1) 报错
```

**为什么 `--skip-forecast` 也跳过 Step1?**
- `--skip-forecast` 意味着已有完整的 `forecast_output/` 目录
- 而 `forecast_output/` 是由 Step1+2 共同产出的
- 所以跳过 Step2 必然也跳过 Step1

### Step 2: 需求预测

```
条件: 未设置 --skip-forecast
命令: python prophet_lightGBM.py
参数: 无命令行参数 (全部配置来自 cleaned_output.csv)

跳过时: 检查 Demand_Forecast_Hourly.csv 是否存在
        → 不存在: [WARN] 警告但不退出 (后续Step4会报错)
```

**prophet_lightGBM.py 不接受命令行参数**: 它的全部输入来自 `cleaned_output.csv`（路径硬编码），输出固定写入 `forecast_output/`。

### Step 3: 预测仪表盘

```
命令: python generate_dashboard.py
条件: 无条件执行 (不依赖飞书API，仅读取 forecast_output/ 中的CSV)
参数: 无
```

**为什么不跳过?** `generate_dashboard.py` 是纯本地操作——读取已有CSV生成HTML，速度快且不依赖外部服务，无条件执行最安全。

### Step 4: 排班优化

```
命令: python schedule.py --output-dir <SCRIPT_DIR> [透传参数]
      (原脚本名: schedule.py, 后改为 schedule.py)
透传: --month, --no-feishu, --solver-time
固定: --output-dir SCRIPT_DIR  (保持排班结果输出到项目根目录)
```

**`--output-dir` 的作用**: `schedule.py` 中 `base_dir = args.output_dir or os.path.dirname(__file__)`，影响：
- 数据读取路径: `base_dir/forecast_output/Demand_Forecast_Hourly.csv`
- 输出路径: `base_dir/Schedule_*_V3.xlsx`, `base_dir/Schedule_Dashboard_*_V3.html`

传 `SCRIPT_DIR` 保证排班脚本能正确找到 `forecast_output/` 目录。

### 产物收集

```python
# glob搜索最新排班文件
schedule_xlsx_files = sorted(glob("Schedule_*_V3.xlsx"))
schedule_html_files = sorted(glob("Schedule_Dashboard_*_V3.html"))

# 取最新的(排序后最后一个)
collect_outputs(
    schedule_xlsx=schedule_xlsx_files[-1] if schedule_xlsx_files else None,
    schedule_html=schedule_html_files[-1] if schedule_html_files else None,
)
```

### 最终总结

遍历 `pipeline_output/` 目录树，打印文件列表和大小：
```python
for root, dirs, files in os.walk(OUTPUT_DIR):
    # 缩进打印目录结构和每个文件大小(KB)
```

---

## 五、命令行参数完整表

### 飞书数据提取透传 (→ fetch_feishu_data.py)

| 参数 | 类型 | 说明 |
|------|------|------|
| `--wait-refresh SEC` | int | 拉取前等待秒数(让飞书缓存刷新) |
| `--check-freshness` | flag | 连续拉取两次对比数据新鲜度 |
| `--sample N` | int | 只拉取前N条记录(调试模式) |
| `--app-id ID` | str | 飞书App ID(覆盖环境变量) |
| `--app-secret SECRET` | str | 飞书App Secret(覆盖环境变量) |

### 排班优化透传 (→ schedule.py)

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--month YYYY-MM` | str | 自动检测 | 排班月份 |
| `--no-feishu` | flag | False | 跳过飞书人员拉取，用fallback |
| `--solver-time SEC` | int | 300 | CP-SAT每阶段求解时间上限 |

### 跳过控制

| 参数 | 说明 | 跳过范围 |
|------|------|----------|
| `--skip-fetch` | 跳过飞书数据提取 | Step 1 |
| `--skip-forecast` | 跳过数据提取+预测 | Step 1 + Step 2 |

---

## 六、最终产物结构

```
pipeline_output/
├── cleaned_output.csv                    (~50 MB)  飞书拉取+清洗后的原始数据
├── forecast_output/                                需求预测全部产物
│   ├── Demand_Forecast_Hourly.csv       (~500 KB)  逐小时需求预测
│   ├── Demand_Forecast_Daily.csv        (~10 KB)   逐日需求汇总
│   ├── Real_Daily_Summary.csv           (~5 KB)    历史实际日汇总
│   ├── Forecast_Daily_Summary.csv       (~5 KB)    预测日汇总
│   ├── RadTech_Heatmap.png                         放射技师热力图
│   ├── RadDoc_Heatmap.png                          放射医生热力图
│   ├── USDoc_Heatmap.png                           超声医生热力图
│   ├── RadTech_Trend.png                           放射技师趋势图
│   ├── RadDoc_Trend.png                            放射医生趋势图
│   ├── USDoc_Trend.png                             超声医生趋势图
│   ├── Combined_Heatmap.png                        综合热力图
│   ├── Combined_Trend.png                          综合趋势图
│   └── dashboard.html                             预测仪表盘
└── schedule/                                       排班结果
    ├── Schedule_YYYY-MM_V3.xlsx        (~50 KB)    Excel排班表
    └── Schedule_Dashboard_YYYY-MM_V3.html (~100 KB) Web排班仪表盘
```

---

## 七、典型用法

```bash
# 完整运行 (需要飞书凭据和网络)
python run_pipeline.py

# 调试模式 (只拉2000条 + 跳过人员API + 60s求解)
python run_pipeline.py --sample 2000 --no-feishu --solver-time 60

# 已有数据, 直接排班
python run_pipeline.py --skip-fetch --no-feishu

# 跳过预测, 从已有forecast_output开始
python run_pipeline.py --skip-forecast --no-feishu

# 指定排班月份 + 延长时间
python run_pipeline.py --month 2026-07 --solver-time 600 --no-feishu

# 拉取前等30秒(飞书缓存刷新)
python run_pipeline.py --wait-refresh 30 --no-feishu
```

---

## 八、错误处理策略

| 场景 | 行为 |
|------|------|
| Step 失败 (exit≠0) | `sys.exit()` 终止，不执行后续 |
| `--skip-fetch` 但 `cleaned_output.csv` 不存在 | `sys.exit(1)` |
| `--skip-forecast` 但 `Demand_Forecast_Hourly.csv` 不存在 | `[WARN]` 警告，继续 (Step4会失败) |
| `collect_outputs` 中某产物缺失 | `[WARN]` 跳过，继续复制其他文件 |
| 旧 `pipeline_output/` 存在 | `rmtree` 完全删除后重建 |

---

## 九、与各子脚本的关系

```
run_pipeline.py  (总控, ~290行)
    │
    ├── fetch_feishu_data.py         数据源: 飞书Bitable → CSV
    │   └── 凭据: 环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET
    │
    ├── prophet_lightGBM.py          模型: Prophet(趋势) + LightGBM(残差)
    │   └── 输入: cleaned_output.csv → 输出: forecast_output/
    │
    ├── generate_dashboard.py        可视化: Plotly/Matplotlib图表
    │   └── 输入: forecast_output/CSVs → 输出: dashboard.html
    │
    └── schedule.py                  优化: 三阶段CP-SAT排班
        └── 输入: forecast_output/Demand_Forecast_Hourly.csv
            → 输出: Schedule_*.xlsx + Schedule_Dashboard_*.html
```
