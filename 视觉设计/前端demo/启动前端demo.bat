@echo off
chcp 65001 >nul
title GeoThermoAI 前端 Demo
echo ============================================
echo   GeoThermoAI 前端 Demo 启动器 (Gradio)
echo   环境: GeoThermoAI (Python 3.11)
echo ============================================
echo.

cd /d "d:\Files\研究和项目\10.GeoThermoAI\视觉设计\前端demo"

echo 正在启动 Gradio 服务器...
echo 启动后会自动打开浏览器: http://127.0.0.1:7860
echo.
echo 按 Ctrl+C 可停止服务器
echo ============================================
echo.

D:\Apps\Anaconda3\envs\GeoThermoAI\python.exe app.py

echo.
echo 服务器已停止。
pause
