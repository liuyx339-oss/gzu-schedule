#!/usr/bin/env python3
"""
=========================================================
全流程编排脚本
=========================================================
所有产物直接输出到 pipeline_output/ 目录，无需额外收集步骤。

流程:
  Step 1:   fetch_feishu_data.py       → pipeline_output/cleaned_output.csv
  Step 2:   prophet_lightGBM.py        → pipeline_output/*.csv, *.png
  Step 2.5: fetch_real_reservations.py → pipeline_output/Real_Reservations_*.csv
  Step 3:   generate_dashboard.py      → pipeline_output/dashboard.html
  Step 4:   schedule.py → pipeline_output/schedule/Schedule_*.xlsx + _Dashboard_*.html
  Step 5:   deploy.py → deploy_package/ (给同事的可交互排班包)

成品结构:
  pipeline_output/
  ├── cleaned_output.csv
  ├── Demand_Forecast_Hourly.csv / Daily.csv
  ├── Real_Reservations_Checkup.csv / _OB.csv
  ├── dashboard.html
  ├── *_Heatmap.png / *_Trend.png
  └── schedule/
      ├── Schedule_YYYY-MM_V3.xlsx
      └── Schedule_Dashboard_YYYY-MM_V3.html

用法:
  python run_pipeline.py                              # 默认运行
  python run_pipeline.py --sample 2000                # 调试模式
  python run_pipeline.py --skip-fetch                 # 跳过Step1
  python run_pipeline.py --skip-forecast              # 跳过Step1+2
"""

from __future__ import annotations

import argparse
import glob as glob_mod
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINE_DIR = os.path.join(SCRIPT_DIR, "pipeline_output")


def run_step(step_label: str, cmd: list[str]) -> None:
    """运行一个子进程步骤，失败时打印错误并退出"""
    print(f"\n{'=' * 60}")
    print(f"  {step_label}")
    print(f"  {' '.join(cmd)}")
    print(f"{'=' * 60}")
    result = subprocess.run(cmd, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        print(f"\n[ERROR] {step_label} 失败 (exit code={result.returncode})")
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(
        description="全流程编排: 数据处理 → 预测 → 看板 → 排班",
    )
    # ---- fetch_feishu_data.py 透传 ----
    parser.add_argument("--wait-refresh", type=int, default=0, metavar="SECONDS")
    parser.add_argument("--check-freshness", action="store_true")
    parser.add_argument("--sample", type=int, default=None, metavar="N")
    parser.add_argument("--app-id", type=str, default=None, metavar="APP_ID")
    parser.add_argument("--app-secret", type=str, default=None, metavar="APP_SECRET")
    # ---- schedule.py 透传 ----
    parser.add_argument("--month", type=str, default=None, metavar="YYYY-MM")
    parser.add_argument("--no-feishu", action="store_true")
    parser.add_argument("--solver-time", type=int, default=300, metavar="SECONDS")
    # ---- 跳过 ----
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-forecast", action="store_true")
    args = parser.parse_args()

    cleaned_csv = os.path.join(PIPELINE_DIR, "cleaned_output.csv")

    # 确保输出目录存在
    os.makedirs(PIPELINE_DIR, exist_ok=True)

    # ================================================================
    # Step 1: fetch_feishu_data.py → pipeline_output/cleaned_output.csv
    # ================================================================
    if not args.skip_fetch and not args.skip_forecast:
        fetch_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "fetch_feishu_data.py")]
        if args.wait_refresh:
            fetch_cmd.append(f"--wait-refresh={args.wait_refresh}")
        if args.check_freshness:
            fetch_cmd.append("--check-freshness")
        if args.sample is not None:
            fetch_cmd.append(f"--sample={args.sample}")
        if args.app_id:
            fetch_cmd.extend(["--app-id", args.app_id])
        if args.app_secret:
            fetch_cmd.extend(["--app-secret", args.app_secret])
        run_step("Step 1/5: 飞书数据提取与清洗", fetch_cmd)
    else:
        print(f"\n[SKIP] Step 1/5: 飞书数据提取与清洗")
        if not os.path.exists(cleaned_csv):
            print(f"  [ERROR] --skip-fetch 但 {cleaned_csv} 不存在!")
            sys.exit(1)

    # ================================================================
    # Step 2: prophet_lightGBM.py
    #         读 pipeline_output/cleaned_output.csv
    #         写 pipeline_output/*.csv + *.png
    # ================================================================
    if not args.skip_forecast:
        prophet_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "prophet_lightGBM.py")]
        if args.month:
            prophet_cmd.extend(["--month", args.month])
        run_step("Step 2/5: 需求预测 (Prophet + LightGBM)", prophet_cmd)
    else:
        print(f"\n[SKIP] Step 2/5: 需求预测")
        main_csv = os.path.join(PIPELINE_DIR, "Demand_Forecast_Hourly.csv")
        if not os.path.exists(main_csv):
            print(f"  [WARN] {main_csv} 不存在，后续步骤可能失败")

    # ================================================================
    # Step 2.5: fetch_real_reservations.py
    #           写 pipeline_output/Real_Reservations_Checkup.csv + _OB.csv
    # ================================================================
    reservations_script = os.path.join(SCRIPT_DIR, "fetch_real_reservations.py")
    if os.path.exists(reservations_script):
        reservations_cmd = [sys.executable, reservations_script]
        if args.app_id:
            reservations_cmd.extend(["--app-id", args.app_id])
        if args.app_secret:
            reservations_cmd.extend(["--app-secret", args.app_secret])
        run_step("Step 2.5/5: 明日真实预约数据拉取", reservations_cmd)
    else:
        print(f"\n[WARN] fetch_real_reservations.py 不存在，跳过")

    # ================================================================
    # Step 3: generate_dashboard.py
    #         读 pipeline_output/*.csv → 写 pipeline_output/dashboard.html
    # ================================================================
    dashboard_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "generate_dashboard.py")]
    run_step("Step 3/5: 预测仪表盘生成", dashboard_cmd)

    # ================================================================
    # Step 4: schedule.py
    #         读 pipeline_output/Demand_Forecast_Hourly.csv
    #         写 pipeline_output/schedule/Schedule_*.xlsx + _Dashboard_*.html
    # ================================================================
    schedule_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "schedule.py")]
    if args.month:
        schedule_cmd.extend(["--month", args.month])
    if args.no_feishu:
        schedule_cmd.append("--no-feishu")
    if args.solver_time:
        schedule_cmd.extend(["--solver-time", str(args.solver_time)])
    schedule_cmd.extend(["--output-dir", SCRIPT_DIR])

    run_step("Step 4/5: 排班优化", schedule_cmd)

    # ================================================================
    # Step 5: deploy.py → deploy_package/
    # ================================================================
    deploy_script = os.path.join(SCRIPT_DIR, "deploy.py")
    schedule_dir = os.path.join(PIPELINE_DIR, "schedule")
    xlsx_files = sorted(glob_mod.glob(os.path.join(schedule_dir, "Schedule_*_V3.xlsx")))
    deploy_xlsx = xlsx_files[-1] if xlsx_files else None

    if os.path.exists(deploy_script) and deploy_xlsx:
        deploy_cmd = [sys.executable, deploy_script, "--xlsx", deploy_xlsx]
        run_step("Step 5/5: 生成部署包 (deploy_package/)", deploy_cmd)
        print(f"\n  => 部署包: {os.path.join(SCRIPT_DIR, 'deploy_package')}")
        print(f"  => 拷贝 deploy_package/ 文件夹给同事即可")
    else:
        print(f"\n[WARN] 跳过部署包生成")

    # ================================================================
    # 最终总结
    # ================================================================
    print(f"\n{'=' * 60}")
    print(f"  [DONE] 全流程完成!")
    print(f"{'=' * 60}")
    print(f"\n  输出目录: {PIPELINE_DIR}")
    print(f"\n  目录结构:")
    for root, dirs, files in os.walk(PIPELINE_DIR):
        level = root.replace(PIPELINE_DIR, "").count(os.sep)
        indent = "    " * level
        folder_name = os.path.basename(root) or "pipeline_output"
        print(f"  {indent}{folder_name}/")
        sub_indent = "    " * (level + 1)
        for fname in sorted(files)[:30]:
            fpath = os.path.join(root, fname)
            size_kb = os.path.getsize(fpath) / 1024
            print(f"  {sub_indent}{fname}  ({size_kb:,.0f} KB)")
        if len(files) > 30:
            print(f"  {sub_indent}... 还有 {len(files) - 30} 个文件")
    print()


if __name__ == "__main__":
    main()
