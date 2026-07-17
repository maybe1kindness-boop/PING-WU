@echo off
chcp 65001 >nul
title KHunter - 盘中实时模拟盘

cd /d "%~dp0"

:: 关键：代码中含 emoji，必须开启 UTF-8 模式，否则一打印 emoji 就崩溃
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未检测到 Python，请先安装 Python 3.8+ 并加入 PATH
    pause
    exit /b 1
)

echo ============================================
echo  KHunter 盘中实时模拟盘（桌面应用）
echo  双击此文件即可启动，无需命令行
echo ============================================
echo.
echo 正在打开桌面窗口...
echo.

python main.py paper

pause
