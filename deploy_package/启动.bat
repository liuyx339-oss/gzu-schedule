@echo off
chcp 65001 >nul
title GZU 排班系统

echo ==========================================
echo   GZU 放射/超声排班系统
echo ==========================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] 未检测到 Python
    echo 请先安装 Python 3.10+: https://www.python.org/downloads/
    echo 安装时务必勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo [OK] Python 已就绪
echo.

pip show python-dotenv >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] 安装必要依赖...
    pip install python-dotenv requests -q
)

echo [INFO] 正在启动排班系统...
echo.
echo   排班页面将自动在浏览器中打开
echo   如需修改排班，直接点击格子即可
echo   按 Ctrl+C 停止服务器
echo.
start "" http://127.0.0.1:8765/排班仪表盘.html
python server.py --port 8765

pause
