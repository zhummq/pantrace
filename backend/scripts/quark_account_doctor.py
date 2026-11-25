"""夸克转存账号体检 / 换 cookie：一条命令定位「线上点开链接全回退」是不是账号挂了。

用法（项目根目录，服务器上就是 `/srv/pantrace/backend`）：

    # 1) 体检：把库里每个夸克账号的 cookie 拎出来，真打一次夸克接口
    .venv/bin/python scripts/quark_account_doctor.py

    # 2) 只测一串新 cookie，不落库（先把 cookie 存进文件，别写在命令行里——
    #    shell history 会把它留下来）
    .venv/bin/python scripts/quark_account_doctor.py --test cookie.txt

    # 3) 确认能用之后，写回数据库 + 清掉失败缓存，立刻恢复
    .venv/bin/python scripts/quark_account_doctor.py --set cookie.txt

    # 4) 只想清失败缓存（换完 cookie 但不想重启时）
    .venv/bin/python scripts/quark_account_doctor.py --purge-fail

判读
----
返回 `登录态有效` 的，cookie 没问题，别再折腾它，去查转存那一步（容量满 /
资源违规 / 上游风控会给出各自的 reason，看 mirrors 表）。
返回 `cookie 已失效` 的，夸克那边登录态掉了——换 cookie 是唯一解，
本脚本的 `--set` 一条命令做完：写 cookie、fail_count 归零、status 置 1、
清 mirrors 里的 fail 缓存（否则改完还要等 10 分钟才重新试）。

为什么必须清 fail 缓存
----------------------
`resolver.mirror()` 会把失败结果写进 mirrors 表并缓存 `mirror_fail_ttl` 秒
（默认 600）。换完 cookie 不清它，用户点同一个资源还是拿回退链接，
看上去像"改了没用"。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "analytics.db"

# 夸克 PC 网页版只认这几个字段。别的域（bilibili 的 _UP_A4A_11_、isg 之类）
# 一起留着不影响请求，但会干扰人判断"这串到底是谁的"。
QK_KEYS = ("__pus", "__kp", "__kps", "__ktd", "__uid", "__puus")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
API = "https://drive-pc.quark.cn/1/clouddrive"


def _sec(text: str) -> str:
    """脱敏：只留 __uid 前 6 位，cookie 主体一律不打印。"""
    uid = ""
    for part in str(text or "").split(";"):
        part = part.strip()
        if part.startswith("__uid="):
            uid = part.split("=", 1)[1][:6]
    return f"__uid={uid}…" if uid else "（无 __uid，可能不是夸克 cookie）"


def _read_cookie(arg: str) -> str:
    """`-` 从 stdin 读，其余当文件名。命令行直接传 cookie 会进 history，不提供。"""
    if arg == "-":
        return sys.stdin.read().strip()
    path = Path(arg)
    if not path.exists():
        raise SystemExit(f"找不到文件：{arg}（cookie 写进文件再传路径，或用 - 从标准输入读）")
    return path.read_text(encoding="utf-8").strip()


def _requests():
    try:
        import requests
    except ImportError:
        raise SystemExit("缺 requests：在 backend 目录用 .venv 里的 python 跑")
    return requests


def probe(cookie: str) -> dict:
    """真打两个只读接口。返回判定结果，不抛异常（探测失败本身就是信息）。

    只用只读接口：不建目录、不转存、不产生任何账号内的可见痕迹。
    """
    requests = _requests()
    out: dict = {"ok": False, "level": "", "detail": "", "nick": "", "vip": "", "used": "", "total": ""}
    if not cookie.strip():
        out["detail"] = "cookie 为空"
        return out
    if "__puus" not in cookie:
        out["detail"] = "缺 __puus（夸克的登录态主凭证），这串 cookie 不可能登录"
        return out

    headers = {
        "cookie": cookie,
        "user-agent": UA,
        "referer": "https://pan.quark.cn/",
        "accept": "application/json, text/plain, */*",
    }
    try:
        r = requests.get(
            "https://pan.quark.cn/account/info",
            params={"fr": "pc", "platform": "pc"},
            headers=headers, timeout=15,
        )
        j = r.json()
    except Exception as exc:  # noqa: BLE001
        out["detail"] = f"网络异常：{exc}"
        return out

    data = j.get("data") or {}
    if j.get("code") == "OK" and data.get("nickname"):
        out["ok"] = True
        out["level"] = "登录态有效"
        out["nick"] = data.get("nickname")
    else:
        out["level"] = "cookie 已失效"
        out["detail"] = f"account/info 返回 {json.dumps(j, ensure_ascii=False)[:160]}"
        return out

    # 能登录 ≠ 能转存。再确认一次读目录和会员态：被封禁/风控的号在这两个接口上会露馅
    try:
        r = requests.get(
            f"{API}/file/sort",
            params={
                "pr": "ucpro", "fr": "pc", "uc_param_str": "", "pdir_fid": "0",
                "_page": "1", "_size": "1", "_fetch_total": "1",
                "_sort": "file_type:asc,updated_at:desc",
            },
            headers=headers, timeout=15,
        )
        j = r.json()
        if j.get("code") != 0:
            out["ok"] = False
            out["level"] = "能登录但读不了目录"
            out["detail"] = f"file/sort code={j.get('code')} msg={j.get('message')}"
            return out
    except Exception as exc:  # noqa: BLE001
        out["detail"] = f"读目录异常：{exc}"

    try:
        r = requests.get(
            f"{API}/member",
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            headers=headers, timeout=15,
        )
        j = r.json()
        d = j.get("data") or {}
        out["vip"] = str(d.get("member_type") or "")
        total = int(d.get("total_capacity") or 0)
        used = int(d.get("use_capacity") or 0)
        if total:
            out["total"] = f"{total / 1024 ** 4:.1f} TiB"
            out["used"] = f"{used / 1024 ** 3:.0f} GiB（{used / total * 100:.1f}%）"
    except Exception:  # noqa: BLE001
        pass
    return out


def _print_probe(tag: str, res: dict) -> None:
    mark = "OK " if res["ok"] else "FAIL"
    print(f"  [{mark}] {tag}")
    if res.get("nick"):
        print(f"         昵称 {res['nick']}  会员 {res.get('vip') or '-'}")
    if res.get("total"):
        print(f"         容量 {res['total']}  已用 {res['used']}")
    if res.get("detail"):
        print(f"         原因 {res['detail']}")


def _db():
    if not DB.exists():
        raise SystemExit(f"没有数据库：{DB}（在 backend 目录下跑）")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_list() -> int:
    conn = _db()
    rows = conn.execute(
        "SELECT id,type,name,cookie,status,fail_count,last_error,updated_ts "
        "FROM pan_accounts WHERE type='quark' ORDER BY id"
    ).fetchall()
    if not rows:
        print("库里没有任何夸克账号 —— 转存必然全回退，用 --set cookie.txt 加一个")
        return 1
    bad = 0
    for row in rows:
        print(
            f"#{row['id']} {row['name'] or '(无名)'}  status={row['status']} "
            f"fail_count={row['fail_count']}  {_sec(row['cookie'])}"
        )
        if row["last_error"]:
            print(f"   last_error: {row['last_error'][:300]}")
        if row["updated_ts"]:
            print(f"   最后更新: {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['updated_ts']))}")
        res = probe(row["cookie"])
        _print_probe("cookie 探测", res)
        if not res["ok"]:
            bad += 1
        print()
    print(f"共 {len(rows)} 个，失效 {bad} 个")
    return 0 if bad == 0 else 3


def cmd_test(arg: str) -> int:
    cookie = _read_cookie(arg)
    print(f"待测 cookie：{_sec(cookie)}  长度 {len(cookie)}")
    res = probe(cookie)
    _print_probe("cookie 探测", res)
    return 0 if res["ok"] else 3


def _purge_fail(conn: sqlite3.Connection) -> int:
    """删掉 mirrors 里失败的缓存行。成功行不动——那些是我们已经建好的分享。"""
    cur = conn.execute("DELETE FROM mirrors WHERE status='fail'")
    return int(cur.rowcount or 0)


def cmd_set(arg: str) -> int:
    cookie = _read_cookie(arg)
    print(f"待写 cookie：{_sec(cookie)}  长度 {len(cookie)}")
    res = probe(cookie)
    _print_probe("写入前校验", res)
    if not res["ok"]:
        print("\n这串 cookie 本身不通，拒绝写库。先换一串能登录的。")
        return 3

    conn = _db()
    rows = conn.execute(
        "SELECT id FROM pan_accounts WHERE type='quark' ORDER BY fail_count, id"
    ).fetchall()
    now = time.time()
    if rows:
        aid = int(rows[0]["id"])
        conn.execute(
            "UPDATE pan_accounts SET cookie=?, status=1, fail_count=0, last_error='', "
            "updated_ts=? WHERE id=?",
            (cookie, now, aid),
        )
        action = f"更新 #{aid}"
    else:
        cur = conn.execute(
            "INSERT INTO pan_accounts (type,name,cookie,access_token,refresh_token,"
            "expires_at,temp_dir,status,fail_count,last_error,extra,created_ts,updated_ts) "
            "VALUES ('quark',?,?,'','',0,'',1,0,'','{}',?,?)",
            ("主号", cookie, now, now),
        )
        aid = int(cur.lastrowid or 0)
        action = f"新建 #{aid}"
    purged = _purge_fail(conn)
    conn.commit()
    conn.close()
    print(f"\n已{action}；清掉 {purged} 条失败缓存。")
    print("转存链路下一次点击就会用新 cookie（账号类型缓存 20 秒内刷新，不必重启）。")
    return 0


def cmd_purge() -> int:
    conn = _db()
    n = _purge_fail(conn)
    conn.commit()
    conn.close()
    print(f"清掉 {n} 条失败缓存")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return cmd_list()
    if args[0] == "--test" and len(args) > 1:
        return cmd_test(args[1])
    if args[0] == "--set" and len(args) > 1:
        return cmd_set(args[1])
    if args[0] == "--purge-fail":
        return cmd_purge()
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
