@echo off
chcp 65001 >nul
title GZU 排班系统 — 环境安装

echo ==========================================
echo   GZU 放射/超声排班系统 — 环境安装
echo ==========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] 未检测到 Python，请先安装 Python 3.10+
    echo 下载地址: https://www.python.org/downloads/
    echo 安装时务必勾选 "Add Python to PATH"
    pause
    exit /b 1
)
echo [OK] Python 已安装
python --version
echo.

:: 创建虚拟环境
if exist venv\ (
    echo [INFO] 虚拟环境已存在，跳过创建
) else (
    echo [INFO] 正在创建虚拟环境...
    python -m venv venv
    echo [OK] 虚拟环境创建完成
)
echo.

:: 激活虚拟环境
call venv\Scripts\activate.bat

:: 升级 pip
echo [INFO] 正在升级 pip...
python -m pip install --upgrade pip -q

:: 安装依赖
echo [INFO] 正在安装依赖（可能需要 5-10 分钟）...
echo [INFO] 如果 Prophet 安装失败，请先安装 Visual C++ Build Tools
echo         https://visualstudio.microsoft.com/visual-cpp-build-tools/
echo.
pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo.
    echo ==========================================
    echo   [WARN] 部分依赖安装失败
    echo ==========================================
    echo   Prophet 在 Windows 上需要 C++ 编译器
    echo   备选方案: 使用 conda 安装
    echo     conda install -c conda-forge prophet
    echo.
    pause
    exit /b 1
)

:: 检查 .env 文件
if not exist .env (
    echo [INFO] 创建 .env 配置文件...
    copy .env.example .env >nul 2>&1
    if %errorlevel% neq 0 (
        echo [WARN] 未找到 .env.example，请手动创建 .env 文件
    )
)

echo.
echo ==========================================
echo   [DONE] 环境安装完成！
echo ==========================================
echo.
echo   后续步骤:
echo     1. 编辑 .env 文件，填入飞书凭据
echo     2. 双击 run_pipeline.bat 运行排班流水线
echo.
pause
