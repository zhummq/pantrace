#!/usr/bin/env bash
# 盘迹 PanTrace —— Linux 启动脚本
set -euo pipefail

# 全部用绝对路径，避免从任何目录调用时找不到文件
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

VENV="$ROOT/backend/.venv"
PY="${PYTHON:-python3}"
PORT="${PORT:-8000}"

# ---- 1. 虚拟环境 ----
# 光看文件存不存在不够：半成品 venv（python 在、包没装）和跨机器拷贝的 venv
# 都会让后续命令报 "No such file or directory"。这里实际跑一次来验证。
need_new_venv=0
if [ ! -x "$VENV/bin/python" ]; then
  need_new_venv=1
elif ! "$VENV/bin/python" -c "import sys" >/dev/null 2>&1; then
  echo "!! 现有虚拟环境不可用，重建"
  need_new_venv=1
fi

if [ "$need_new_venv" = "1" ]; then
  rm -rf "$VENV"
  echo "[1/3] 创建虚拟环境..."
  if ! "$PY" -m venv "$VENV" 2>/tmp/pantrace_venv_err; then
    echo "!! 创建虚拟环境失败。缺少 venv 模块的话先装："
    echo "   Ubuntu/Debian: apt install -y python3-venv"
    echo "   Rocky/CentOS:  dnf install -y python3"
    echo "--- 错误详情 ---"
    cat /tmp/pantrace_venv_err
    exit 1
  fi
else
  echo "[1/3] 虚拟环境已存在"
fi

# ---- 2. 依赖 ----
if ! "$VENV/bin/python" -c "import fastapi, uvicorn, requests, pydantic_settings" >/dev/null 2>&1; then
  echo "[2/3] 安装依赖（首次约 1 分钟）..."
  "$VENV/bin/python" -m pip install -U pip -q
  "$VENV/bin/python" -m pip install -r "$ROOT/backend/requirements.txt"
else
  echo "[2/3] 依赖已就绪"
fi

if [ ! -f "$ROOT/backend/.env" ]; then
  echo "!! 缺少 backend/.env"
  echo "   cp backend/.env.example backend/.env && vim backend/.env"
  exit 1
fi

if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  echo "!! 缺少 frontend/dist，本地执行 cd frontend && npm run build 后一起上传"
  exit 1
fi

echo "[3/3] 启动 http://0.0.0.0:$PORT"

# 必须单 worker：缓存和上游串行锁都在进程内
# 用 python -m uvicorn，不依赖 bin/uvicorn 的可执行位和 shebang（避免解释器路径失效）
exec "$VENV/bin/python" -m uvicorn app.main:app \
  --app-dir "$ROOT/backend" \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips "*"
