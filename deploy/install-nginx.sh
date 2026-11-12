#!/usr/bin/env bash
# 用 Nginx 托管前端，把 /api 和 /admin 反代给后端。
#
# 为什么不直接把 root 指到项目目录：
# Nginx 的 worker 进程以 www-data（Debian/Ubuntu）或 nginx（RHEL）身份跑，
# 读不了 /root、/home 下的文件。项目放在这些目录里，root 指过去就是 403。
# 所以这份脚本会把 frontend/dist 同步到一个公开可读的目录（默认 /var/www/pantrace）。
#
# 用法（在服务器上，项目根目录）：
#   sudo ./deploy/install-nginx.sh                      # HTTP，装好并 reload
#   sudo ./deploy/install-nginx.sh --domain panseek.xyz # 绑定域名
#   sudo ./deploy/install-nginx.sh --domain panseek.xyz --ssl --email you@mail.com
#                                                       # 申请 Let's Encrypt 证书 + 强制跳 HTTPS
#   sudo ./deploy/install-nginx.sh --print              # 只打印配置，不动系统
#   sudo ./deploy/install-nginx.sh --sync-only          # 前端重新 build 后只同步产物
#
# 前端改了之后的日常更新：
#   cd frontend && npm run build
#   sudo ./deploy/install-nginx.sh --sync-only
set -euo pipefail

SITE_NAME="pantrace"
DOMAIN="_"
PUBLISH_DIR=""
BACKEND_PORT="8000"
ROOT_ARG=""
PRINT_ONLY=0
SYNC_ONLY=0
SSL=0
EMAIL=""

usage() {
  cat <<'EOF'
用法: install-nginx.sh [选项]

  --domain NAME    绑定域名或 IP（默认 _，即接受任意 Host）
  --ssl            申请 Let's Encrypt 证书并把 HTTP 301 到 HTTPS
                   需要 --domain 是真实域名（不能是 IP），80 端口能公网访问
  --email MAIL     证书到期通知邮箱（--ssl 时建议填）
  --name NAME      站点名，同时决定配置文件名和发布目录（默认 pantrace）
  --target DIR     前端产物发布目录（默认 /var/www/<name>）
  --port PORT      后端监听端口（默认 8000）
  --root DIR       项目根目录（默认：本脚本的上一级目录）
  --sync-only      只同步前端产物并 reload，不改配置
  --print          只输出 nginx 配置到屏幕，不写入、不 reload
  -h, --help       显示本帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)    DOMAIN="${2:?--domain 需要参数}"; shift ;;
    --ssl)       SSL=1 ;;
    --email)     EMAIL="${2:?--email 需要参数}"; shift ;;
    --name)      SITE_NAME="${2:?--name 需要参数}"; shift ;;
    --target)    PUBLISH_DIR="${2:?--target 需要参数}"; shift ;;
    --port)      BACKEND_PORT="${2:?--port 需要参数}"; shift ;;
    --root)      ROOT_ARG="${2:?--root 需要参数}"; shift ;;
    --sync-only) SYNC_ONLY=1 ;;
    --print)     PRINT_ONLY=1 ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

ROOT="${ROOT_ARG:-$(cd "$(dirname "$0")/.." && pwd)}"
ROOT="$(cd "$ROOT" && pwd)"
DIST="$ROOT/frontend/dist"
PUBLISH_DIR="${PUBLISH_DIR:-/var/www/$SITE_NAME}"
CONF="/etc/nginx/conf.d/$SITE_NAME.conf"
LE_DIR="/etc/letsencrypt/live/$DOMAIN"

# 已经有证书就别在没带 --ssl 的时候把配置降级回 HTTP——那样 HTTPS 会静默失效
if [ "$SSL" != "1" ] && [ "$DOMAIN" != "_" ] && [ -f "$LE_DIR/fullchain.pem" ]; then
  SSL=1
  echo "检测到 $DOMAIN 已有证书，继续启用 HTTPS（不想的话先删掉 $LE_DIR 或手动改配置）"
fi

# ---- 前置检查 ----
[ -f "$ROOT/backend/app/main.py" ] || { echo "!! $ROOT/backend/app/main.py 不存在，--root 指错了？" >&2; exit 1; }
[ -f "$DIST/index.html" ] || { echo "!! 缺少 $DIST/index.html
   先在本地或服务器上构建：cd $ROOT/frontend && npm run build" >&2; exit 1; }

# 发布目录是脚本自己管的，绝不能是 / 或空，否则下面 --delete 会清空系统
case "$PUBLISH_DIR" in
  ""|"/"|"/etc"|"/usr"|"/var") echo "!! 发布目录不安全：$PUBLISH_DIR" >&2; exit 1 ;;
esac

if [ "$SSL" = "1" ]; then
  if [ "$DOMAIN" = "_" ]; then
    echo "!! --ssl 必须配 --domain，且是真实域名" >&2; exit 1
  fi
  # Let's Encrypt 不给 IP 签证书，写了也是白跑一趟
  if printf '%s' "$DOMAIN" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "!! --ssl 不能用于 IP 地址（$DOMAIN），Let's Encrypt 只给域名签证书" >&2
    echo "   想让 IP 走 HTTPS 只能自签，浏览器会报警，不建议" >&2
    exit 1
  fi
fi

# ---- 站点通用部分：静态托管 + 两个反代 ----
site_locations() {
  cat <<EOF
    root $PUBLISH_DIR;
    index index.html;
    charset utf-8;

    # 单页应用：找不到的路径一律回 index.html，交给前端路由
    location / {
        try_files \$uri \$uri/ /index.html;
    }

    location /assets/ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # 看板是动态页面，必须回后端。
    # 少了这一条会被上面的 try_files 吃掉，打开 /admin 看到的是前端首页。
    location = /admin {
        proxy_pass http://127.0.0.1:$BACKEND_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:$BACKEND_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        proxy_connect_timeout 10s;
        proxy_send_timeout 90s;
        # 必须大于后端 REQUEST_TIMEOUT（75 秒）。Nginx 默认 60 秒会先断，
        # 用户看到 504，而那时候上游其实已经扣了费。
        proxy_read_timeout 90s;
    }

    gzip on;
    gzip_min_length 1k;
    gzip_comp_level 5;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;
EOF
}

HTTP_CONF="server {
    listen 80;
    server_name $DOMAIN;

$(site_locations)
}"

# nginx 1.25.1 起 listen ... http2 已废弃，改用独立指令 http2 on;
# 低版本写 http2 on; 反而会报 unknown directive，所以按版本分支
http2_directive() {
  local ver major minor
  ver=$(nginx -v 2>&1 | sed -n 's#.*nginx/\([0-9.]*\).*#\1#p')
  major=$(printf '%s' "$ver" | cut -d. -f1)
  minor=$(printf '%s' "$ver" | cut -d. -f2)
  major=${major:-1}; minor=${minor:-24}
  if [ "$major" -gt 1 ] || { [ "$major" -eq 1 ] && [ "$minor" -ge 25 ]; }; then
    printf '    listen 443 ssl;\n    http2 on;\n'
  else
    printf '    listen 443 ssl http2;\n'
  fi
}

build_ssl_conf() {
  cat <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    # 续期校验走这个路径，必须留在 80 上，不能被下面的 301 吃掉
    location /.well-known/acme-challenge/ {
        root $PUBLISH_DIR;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
$(http2_directive)
    server_name $DOMAIN;

    ssl_certificate $LE_DIR/fullchain.pem;
    ssl_certificate_key $LE_DIR/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;

$(site_locations)
}
EOF
}

if [ "$PRINT_ONLY" = "1" ]; then
  if [ "$SSL" = "1" ]; then build_ssl_conf; else echo "$HTTP_CONF"; fi
  exit 0
fi

[ "$(id -u)" = "0" ] || { echo "!! 需要 root 权限，请用 sudo 运行" >&2; exit 1; }
command -v nginx >/dev/null 2>&1 || { echo "!! 没装 Nginx：apt install nginx  /  dnf install nginx" >&2; exit 1; }

install_conf() {
  local content="$1" backup=""
  if [ -f "$CONF" ]; then
    backup="$CONF.bak.$(date +%s)"
    cp "$CONF" "$backup"
  fi
  printf '%s\n' "$content" > "$CONF"
  chmod 644 "$CONF"
  if ! nginx -t 2>/tmp/nginx_test_$$.log; then
    echo "!! 配置校验失败，已回滚到旧配置：" >&2
    cat /tmp/nginx_test_$$.log >&2
    if [ -n "$backup" ]; then mv "$backup" "$CONF"; else rm -f "$CONF"; fi
    rm -f /tmp/nginx_test_$$.log
    return 1
  fi
  rm -f /tmp/nginx_test_$$.log
}

reload_nginx() { systemctl reload nginx 2>/dev/null || nginx -s reload; }

# ---- 1. 同步前端产物 ----
echo "[1/4] 同步前端产物 -> $PUBLISH_DIR"
install -d -m 755 "$PUBLISH_DIR"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "$DIST/" "$PUBLISH_DIR/"
else
  # 没有 rsync 就退回删了重拷。路径前面已经校验过，不会是危险目录
  find "$PUBLISH_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  cp -r "$DIST/." "$PUBLISH_DIR/"
fi
chmod -R a+rX "$PUBLISH_DIR"

# SELinux 开着的话，放在 /var/www 的静态文件会被拦（403）
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
  echo "      SELinux 处于 Enforcing，补一条文件上下文"
  chcon -R -t httpd_sys_content_t "$PUBLISH_DIR" 2>/dev/null || true
fi

# ---- 2. 写配置 ----
if [ "$SYNC_ONLY" = "1" ]; then
  echo "[2/4] --sync-only：跳过配置写入"
  [ -f "$CONF" ] || { echo "!! $CONF 不存在，先跑一次不带 --sync-only 的完整安装" >&2; exit 1; }
else
  echo "[2/4] 写入 $CONF（server_name: $DOMAIN）"
  # 先落 HTTP 配置：即使下面申请证书失败，站点也还能用 http 访问
  install_conf "$HTTP_CONF" || exit 1
fi

# ---- 3. HTTPS ----
if [ "$SSL" = "1" ] && [ "$SYNC_ONLY" != "1" ]; then
  echo "[3/4] 配置 HTTPS"
  if [ -f "$LE_DIR/fullchain.pem" ]; then
    echo "      证书已存在，跳过申请（续期由 certbot.timer 负责）"
  else
    command -v certbot >/dev/null 2>&1 || {
      echo "      没装 certbot，正在安装"
      if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot
      elif command -v dnf >/dev/null 2>&1; then
        dnf install -y certbot
      else
        echo "!! 请手动安装 certbot 后重试" >&2; exit 1
      fi
    }
    EMAIL_ARG=(--register-unsafely-without-email)
    if [ -n "$EMAIL" ]; then EMAIL_ARG=(-m "$EMAIL"); fi
    echo "      向 Let's Encrypt 申请 $DOMAIN 的证书"
    if ! certbot certonly --webroot -w "$PUBLISH_DIR" -d "$DOMAIN" \
         --non-interactive --agree-tos "${EMAIL_ARG[@]}" --keep-until-expiring; then
      echo "!! 证书申请失败，站点继续以 HTTP 提供服务" >&2
      echo "   常见原因：域名没解析到本机 / 80 端口被安全组挡住 / 已被签发过多次触发限流" >&2
      echo "   查看详情：/var/log/letsencrypt/letsencrypt.log" >&2
      exit 1
    fi
  fi
  install_conf "$(build_ssl_conf)" || exit 1
else
  echo "[3/4] 跳过 HTTPS"
fi

echo "[4/4] reload nginx"
reload_nginx

# Ubuntu 自带的 default 站点会抢 80 端口：它是第一个 server 块，
# 请求 Host 匹配不上任何 server_name 时就落到它身上，看到的是 Nginx 欢迎页。
if [ -L /etc/nginx/sites-enabled/default ] || [ -f /etc/nginx/sites-enabled/default ]; then
  echo
  echo "注意：检测到 Nginx 默认站点（sites-enabled/default）。"
  echo "      访问 IP 时可能被它接走，看到 Nginx 欢迎页而不是本站。"
  echo "      确认这台机器只跑这一个站的话，执行："
  echo "        sudo rm /etc/nginx/sites-enabled/default && sudo systemctl reload nginx"
fi

echo
echo "自测："
SCHEME="http"
if [ "$SSL" = "1" ] && [ -f "$LE_DIR/fullchain.pem" ]; then SCHEME="https"; fi
for path in "/" "/api/health" "/admin"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$SCHEME://127.0.0.1$path" || echo "000")
  printf "  %-14s %s\n" "$SCHEME$path" "$code"
done

if [ "$SCHEME" = "https" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -I "http://127.0.0.1/" || echo "000")
  printf "  %-14s %s（应为 301）\n" "http 跳转" "$code"
fi

body=$(curl -s --max-time 8 "$SCHEME://127.0.0.1/" || true)
case "$body" in
  *盘迹*) echo "  首页内容：正常" ;;
  *"Welcome to nginx"*) echo "  首页内容：被 Nginx 默认站点接管了，按上面的提示删掉 default" >&2 ;;
  *) echo "  首页内容：不像本站页面，检查 root 是否为 $PUBLISH_DIR" >&2 ;;
esac

echo
if [ "$SSL" = "1" ]; then
  echo "证书续期：systemctl status certbot.timer（装 certbot 时会自动启用，到期前 30 天自动续）"
fi
echo "以后前端更新："
echo "  cd $ROOT/frontend && npm run build && sudo $ROOT/deploy/install-nginx.sh --sync-only"
