# AGENTS.md - 开发指南

## 项目概述
这是一个 Python 医院员工排班（轮值）和需求预测系统，主要功能包括：
- 基于 Prophet 的医学影像科室需求预测
- 放射科和超声科智能排班生成
- 数据清洗和 ETL 管道

## 构建、Lint 和测试命令

### 运行主脚本
```bash
# 数据清洗
python cleaner.py

# Prophet 预测
python prophet_forecast.py

# 生成排班（带预测）
python schedule_new_predict.py

# 生成排班（静态）
python schedule_new.py

# 生成 B 超排班
python generate_bchao_roster.py
```

### 测试
- 本仓库目前**没有测试**
- 如需添加测试，使用 pytest：
  ```bash
  # 运行所有测试
  pytest

  # 运行单个测试文件
  pytest tests/test_file.py

  # 运行单个测试函数
  pytest tests/test_file.py::test_function_name

  # 运行匹配模式的测试
  pytest -k "test_pattern"
  ```

### 代码检查（推荐）
```bash
# 使用 ruff（快速，推荐）
ruff check .

# 使用 pylint
pylint **/*.py

# 使用 black 格式化
black .
```

## 代码风格指南

### 导入顺序
- 标准库优先，然后是第三方库，最后是本地模块
- 分组顺序：标准库 > 第三方 > 本地
- 本地模块使用绝对导入
```python
import pandas as pd
import numpy as np
import math
import warnings

from prophet import Prophet
import logging

# 本地导入
from module import func
```

### 格式化
- 最大行长度：100 字符（软限制 120）
- 使用 4 空格缩进（不使用 Tab）
- 合理使用空行分隔逻辑部分
- 使用 `====` 分隔符为代码块添加章节注释
```python
# ==========================================
# 1. 章节标题
# ==========================================
```

### 命名规范
- **函数/变量**：`snake_case`（如 `get_hourly_headcount`、`hours_bank`）
- **常量**：`UPPER_SNAKE_CASE`（如 `STAFF`、`TARGET_HOURS`）
- **类**：`PascalCase`（当前未使用，但新增时遵循此规范）
- 本代码库中允许使用中文变量名（如 `大分类`、`放射医生`）

### 类型
- 这是一个动态类型代码库
- 建议为新函数添加类型提示：
```python
def get_hourly_headcount(workload_mins: float, dept: str, role: str, is_daytime: bool) -> int:
```

### 错误处理
- 使用 try/except 块并指定具体异常类型
- 始终提供有意义的错误信息
- 失败时提前返回，避免深度嵌套
```python
try:
    df = pd.read_csv("file.csv", encoding="utf-8-sig")
except UnicodeDecodeError:
    # 回退到 GBK 编码（Windows 导出文件常用）
    df = pd.read_csv("file.csv", encoding="gbk")
except Exception as e:
    print(f"❌ 读取数据失败: {e}")
    return
```

### 数据处理
- CSV 输出使用 `encoding="utf-8-sig"`（Excel 兼容）
- 读取大文件时使用 `low_memory=False`
- 显式处理缺失值，使用 `.dropna()` 或 `.fillna()`

### 日志记录
- 使用带表情符号的 print 语句输出状态信息（当前约定）
- 使用 `warnings.filterwarnings("ignore")` 抑制库警告
- 抑制冗长的库日志：
```python
logging.getLogger('prophet').setLevel(logging.ERROR)
```

### 文档
- 为主函数添加 docstring，使用 Google 风格或简单格式：
```python
def clean_and_format_data(input_file, output_file):
    """
    清洗并格式化原始放射科执行数据。
    
    参数:
        input_file: 源 CSV 文件路径
        output_file: 目标 CSV 文件路径
    """
```

### 文件结构
- 入口点使用 `if __name__ == "__main__":` 守卫
- 将相关常量放在模块级别（文件顶部）
- 将配置/数据定义与业务逻辑分离

### 最佳实践
- 修改 DataFrame 时始终使用 `.copy()` 避免 SettingWithCopyWarning
- 使用 pandas 向量化操作代替循环
- 正确关闭文件或使用上下文管理器（`with open(...)`）
- 处理前验证文件是否存在

### 本仓库常见模式
- 代码中保留中文注释（新增代码可选）
- print 语句中使用表情符号作为视觉状态提示
- 模块化设计，独立函数
- CSV 作为主要数据交换格式
