#!/usr/bin/env bash
# 把 PanTrace 装成 systemd 服务。
#
# 解决的就是"关掉 SSH 服务就停"这件事：直接 ./start.sh 跑的是当前会话的前台进程，
# 会话一断就收到 SIGHUP 退出。交给 systemd 托管后进程归 init 管，
# 断线不受影响，服务器重启也会自动拉起。
#
# 用法（在服务器上，项目根目录）：
#   sudo ./deploy/install-service.sh              # 按当前路径和当前用户安装
#   sudo ./deploy/install-service.sh --print      # 只打印 unit 内容，不安装
#   sudo ./deploy/install-service.sh --port 8080
#   sudo ./deploy/install-service.sh --user pantrace
#   sudo ./deploy/install-service.sh --name pantrace-test
#
# 卸载：
#   sudo systemctl disable --now pantrace
#   sudo rm /etc/systemd/system/pantrace.service && sudo systemctl daemon-reload
set -euo pipefail

SERVICE_NAME="pantrace"
PORT="8000"
ROOT_ARG=""
RUN_USER="${SUDO_USER:-$(id -un)}"
PRINT_ONLY=0

usage() {
  cat <<'EOF'
用法: install-service.sh [选项]

  --print          只输出 unit 内容到屏幕，不写入、不启动
  --root DIR       项目根目录（默认：本脚本的上一级目录）
  --port PORT      监听端口（默认 8000）
  --user USER      运行服务的系统用户（默认当前用户）
  --name NAME      服务名（默认 pantrace）
  -h, --help       显示本帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --print) PRINT_ONLY=1 ;;
    --root)  ROOT_ARG="${2:?--root 需要参数}"; shift ;;
    --port)  PORT="${2:?--port 需要参数}"; shift ;;
    --user)  RUN_USER="${2:?--user 需要参数}"; shift ;;
    --name)  SERVICE_NAME="${2:?--name 需要参数}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

ROOT="${ROOT_ARG:-$(cd "$(dirname "$0")/.." && pwd)}"
ROOT="$(cd "$ROOT" && pwd)"
VENV="$ROOT/backend/.venv"
UNIT="/etc/systemd/system/$SERVICE_NAME.service"

# ---- 前置检查：这里报错比 systemd 起来又挂掉好排查得多 ----
[ -f "$ROOT/backend/app/main.py" ] || { echo "!! $ROOT/backend/app/main.py 不存在，--root 指错了？" >&2; exit 1; }
[ -x "$VENV/bin/python" ] || { echo "!! 虚拟环境不存在：$VENV
   先手动跑一次 ./start.sh 让它把 venv 和依赖装好（跑起来后 Ctrl+C 停掉即可）" >&2; exit 1; }
"$VENV/bin/python" -c "import fastapi, uvicorn, requests" >/dev/null 2>&1 \
  || { echo "!! 依赖不全，先执行：$VENV/bin/pip install -r backend/requirements.txt" >&2; exit 1; }
[ -f "$ROOT/backend/.env" ] || { echo "!! 缺少 $ROOT/backend/.env" >&2; exit 1; }
[ -f "$ROOT/frontend/dist/index.html" ] || { echo "!! 缺少 frontend/dist，后端会没有页面可托管" >&2; exit 1; }

id "$RUN_USER" >/dev/null 2>&1 || { echo "!! 用户不存在：$RUN_USER" >&2; exit 1; }

# ProtectHome 会屏蔽 /root /home，项目放在这些目录下必须关掉，
# 否则服务起来读不到代码，报 Permission denied。
case "$ROOT" in
  /root|/root/*|/home/*|/Users/*) PROTECT_HOME="ProtectHome=false" ;;
  *) PROTECT_HOME="" ;;
esac

UNIT_CONTENT="[Unit]
Description=PanTrace netdisk search ($ROOT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$ROOT
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
# 必须单 worker：缓存与上游串行锁都在进程内，多进程会各自一套，串行保护失效
ExecStart=$VENV/bin/python -m uvicorn app.main:app --app-dir $ROOT/backend --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
# 限制日志体积，避免长期跑把磁盘写满
LogRateLimitIntervalSec=30s
LogRateLimitBurst=200

# 基础加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full"
[ -n "$PROTECT_HOME" ] && UNIT_CONTENT="$UNIT_CONTENT
$PROTECT_HOME"
UNIT_CONTENT="$UNIT_CONTENT

[Install]
WantedBy=multi-user.target"

if [ "$PRINT_ONLY" = "1" ]; then
  echo "$UNIT_CONTENT"
  exit 0
fi

[ "$(id -u)" = "0" ] || { echo "!! 需要 root 权限，请用 sudo 运行" >&2; exit 1; }

# 端口被占时直接说清楚，不然 systemd 会每 3 秒重启一次刷日志
port_busy() {
  ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -qE "[:.]$PORT\$"
}
if port_busy && ! systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "!! 端口 $PORT 已被占用，先停掉旧进程：" >&2
  ss -ltnp 2>/dev/null | grep -E "[:.]$PORT\b" >&2 || true
  echo "   找到 PID 后执行：kill <PID>" >&2
  echo "   （如果那是旧的 ./start.sh，直接 Ctrl+C 或 kill 掉即可）" >&2
  exit 1
fi

printf '%s\n' "$UNIT_CONTENT" > "$UNIT"
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"

echo
echo "已安装：$UNIT"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "状态：运行中 ✅"
  systemctl --no-pager --lines=0 status "$SERVICE_NAME" | head -5 || true
  echo
  echo "自测："
  curl -s --max-time 5 "http://127.0.0.1:$PORT/api/health" || echo "  （健康检查无响应，看日志：journalctl -u $SERVICE_NAME -n 50）"
  echo
  echo "常用命令："
  echo "  systemctl status $SERVICE_NAME      # 看状态"
  echo "  systemctl restart $SERVICE_NAME     # 改完代码/配置后重启"
  echo "  journalctl -u $SERVICE_NAME -f      # 实时看日志"
  echo "  systemctl disable --now $SERVICE_NAME  # 停止并取消开机自启"
else
  echo "状态：启动失败 ❌  日志如下：" >&2
  journalctl -u "$SERVICE_NAME" -n 40 --no-pager >&2 || true
  exit 1
fi
