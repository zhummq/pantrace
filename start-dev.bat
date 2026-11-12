@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 启动后端 :8000
start "PanTrace Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"

echo 启动前端 :5173
start "PanTrace Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

timeout /t 3 >nul
start http://127.0.0.1:5173
