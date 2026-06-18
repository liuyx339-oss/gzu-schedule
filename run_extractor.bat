@echo off
chcp 65001 >nul
echo ========================================
echo   屏幕内容提取
echo   千问 VL + 飞书 Wiki
echo ========================================
echo.

REM ===== 千问 API Key =====
set DASHSCOPE_API_KEY=你的千问Key填这里

REM ===== 飞书凭据 =====
set FEISHU_APP_ID=cli_aaa8d24639b8dcd8
set FEISHU_APP_SECRET=b0ayVQKIuUGmvzRu9YCm9gpZHUzniNz1

echo 环境变量已加载
echo 启动中...
echo.

python "%~dp0\screen_extractor.py"
pause
