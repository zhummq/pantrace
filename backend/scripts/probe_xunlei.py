"""迅雷转存服务器端探针：排查「本地 200 / 服务器 404」是不是机房 IP 被迅雷软拦截。

用法（服务器上，项目根目录）：
    cd /opt/pantrace/backend
    .venv/bin/python scripts/probe_xunlei.py https://pan.xunlei.com/s/XXXX?pwd=YYYY

做的事：
    1. 从 data/analytics.db 读启用的迅雷账号 refresh_token
    2. 用项目真实的 XunleiClient 走完整链路：读分享 → 转存(pantrace 目录) → 建分享 → 删除
    3. refresh_token 轮换后自动写回数据库，账号不留坏状态
    4. 每一步实时打印结果，哪一步 404 一眼可见

结果判读：
    - 全部 OK           → 服务器 IP 没问题，之前失败另有原因，把输出交给维护者
    - 转存 404、其余 OK → 机房 IP 被迅雷对写操作软拦截（本地同样请求是 200）
    - 读分享就 404       → 源链接失效，与服务器无关
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "analytics.db"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    pwd = ""
    if "pwd=" in url:
        pwd = url.split("pwd=", 1)[1].split("&", 1)[0].strip()

    from app.sdk.adapters.xunlei.client import XunleiClient
    from app.sdk.types import Account

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM pan_accounts WHERE type='xunlei' AND status=1 ORDER BY id LIMIT 1"
    ).fetchall()
    if not rows:
        print("数据库里没有启用的迅雷账号")
        return 1
    row = rows[0]
    acct = Account(
        id=int(row["id"]), type="xunlei", name=str(row["name"]),
        access_token=str(row["access_token"]), refresh_token=str(row["refresh_token"]),
        expires_at=int(row["expires_at"]), temp_dir=str(row["temp_dir"]),
    )
    print(f"账号: #{row['id']} {row['name']}")

    def on_cookie(value: str) -> None:
        token = refresh = ""
        for part in value.split(";"):
            if part.startswith("__token__="):
                token = part.split("=", 1)[1]
            elif part.startswith("__refresh__="):
                refresh = part.split("=", 1)[1]
        conn.execute(
            "UPDATE pan_accounts SET access_token=?, refresh_token=?, updated_ts=strftime('%s','now') WHERE id=?",
            (token, refresh, row["id"]),
        )
        conn.commit()
        print("  (轮换后的 token 已写回数据库)")

    client = XunleiClient(acct, on_cookie=on_cookie)

    print("== 1/4 读分享 ==")
    info = client.share_info(url, pwd)
    print(f"  OK: {info.title!r}，{len(info.files)} 个文件")

    print("== 2/4 转存到 pantrace 目录 ==")
    fids = client.transfer(info)
    print(f"  OK: {len(fids)} 个新 fid")

    print("== 3/4 建我们的分享 ==")
    link = client.create_share(fids, info.title or "探针测试", expire_hours=24)
    print(f"  OK: {link.url}")

    print("== 4/4 删除转存文件 ==")
    client.delete(fids)
    print("  OK: 已清理")

    print()
    print(">>> 服务器侧全链路通过，IP 没有被拦 <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())
