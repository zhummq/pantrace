@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo   盘迹 PanTrace - 生产模式（单端口）
echo ==========================================

if not exist "backend\.venv\Scripts\python.exe" (
  echo [1/3] 创建 Python 虚拟环境...
  python -m venv backend\.venv
  backend\.venv\Scripts\python.exe -m pip install -q -r backend\requirements.txt
)

if not exist "backend\.env" (
  echo [!] 缺少 backend\.env，正在从模板复制，请填入 API_KEY 后重试
  copy backend\.env.example backend\.env >nul
  pause
  exit /b 1
)

echo [2/3] 构建前端...
pushd frontend
if not exist "node_modules" call npm install --registry=https://registry.npmmirror.com
call npm run build
popd

echo [3/3] 启动服务 http://127.0.0.1:8000
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
