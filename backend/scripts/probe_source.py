"""候选搜索源探针：加源之前先跑一遍，别拿"代码写完了"当"源能用"。

为什么要有这个脚本
------------------
调研 PanSou 那 113 个插件时实测发现，三个失败点长得很像，但成因完全不同：

    hjzhencai   首页 200、搜索页也 200  → 但站点已转型成影视站，"小红书"0 结果
    sdso.top    接口 200               → 但返回的是自建 JS 挑战页，不是 JSON
    kkmao 等    直接 000               → DNS / 连接层面就不通

只看"首页能不能打开"会全部误判成可用。这个脚本按三层依次判定：
可达（HTTP 层）→ 可抓（返回的是内容还是挑战页）→ 有效（真能捞出网盘链接）。

用法
----
    # 1) 只探测可达性，一次给一批域名
    python scripts/probe_source.py --hosts haisou.cc sdso.top ahhhhfs.com

    # 2) 探测一个具体搜索接口（{kw} 会被替换成关键词）
    python scripts/probe_source.py --url "https://haisou.cc/api/pan/share/search?query={kw}&scope=title" --kw 三体

    # 3) 顺手看看页面里有多少网盘链接
    python scripts/probe_source.py --url "https://example.com/search?q={kw}" --kw 三体 --show

结果判读
--------
    OK           三层全过，可以考虑接
    JS_CHALLENGE 返回挑战页，需要 JS 引擎（PanSou 用 goja，Python 侧要 mini-racer/playwright）
    CLOUDFLARE   403 + cf 特征，需要 cloudscraper
    NO_LINK      页面通了但没有网盘链接 —— 多半是搜索无结果，或链接在详情页里
    UNREACHABLE  DNS / 连接失败，别浪费时间
    TIMEOUT      超时，源可能限速或本身很慢

判 NO_LINK 时先换关键词再下结论：站点可能只是这个关键词没货。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 网盘域名 → 平台 key。和 app/sdk/urls.py 保持一致，加新平台两边一起改。
PAN_HOSTS = {
    "pan.quark.cn": "quark",
    "pan.baidu.com": "baidu",
    "www.alipan.com": "ali",
    "www.aliyundrive.com": "ali",
    "drive.uc.cn": "uc",
    "fast.uc.cn": "uc",
    "pan.xunlei.com": "xunlei",
    "115.com": "115",
    "115cdn.com": "115",
    "anxia.com": "115",
    "123pan.com": "123",
    "123pan.cn": "123",
    "123684.com": "123",
    "cloud.189.cn": "tianyi",
    "www.guangyapan.com": "guangya",
    "guangyapan.com": "guangya",
    "caiyun.139.com": "yidong",
    "yun.139.com": "yidong",
    "lanzou": "lanzou",
}

LINK_RE = re.compile(
    r"https?://[^\s\"'<>()\\]*?(?:"
    + "|".join(re.escape(h) for h in sorted(PAN_HOSTS, key=len, reverse=True))
    + r")[^\s\"'<>()\\]*"
)
# 提取码：链接附近的「提取码/密码/pwd」+ 4~8 位字母数字
PWD_RE = re.compile(r"(?:提取码|访问码|密码|pwd|password|code)\s*[:：=]\s*([A-Za-z0-9]{4,8})")
# JS 挑战页特征：随机 cookie 名 + btoa + reload，PanSou 用 goja 执行的就是这种
CHALLENGE_HINTS = ("document.cookie", "btoa(", "location.reload", "__jsl", "acw_sc")


def platform_of(url: str) -> str:
    for host, key in PAN_HOSTS.items():
        if host in url:
            return key
    return "others"


def classify(resp: requests.Response | None, body: str, elapsed: float, err: str) -> tuple[str, str]:
    """把一次探测归成一个状态，附带一句人话原因。"""
    if err:
        low = err.lower()
        if "timeout" in low:
            return "TIMEOUT", err
        # 证书链不全会让 requests 直接抛 SSLError，但它其实「连得上」。
        # 归到 UNREACHABLE 会让人误判成站死了，实际多半是本机 CA 库旧或站点用了自签。
        if "certificate" in low or "ssl" in low:
            return "CERT_ERROR", err
        return "UNREACHABLE", err
    if resp is None:
        return "UNREACHABLE", "无响应"

    body_head = body[:4000]
    if resp.status_code == 403 and ("cloudflare" in body_head.lower() or "cf-ray" in resp.headers.get("server", "").lower()):
        return "CLOUDFLARE", f"HTTP 403，CF 特征，耗时 {elapsed:.2f}s"
    if resp.status_code == 403:
        return "CLOUDFLARE", f"HTTP 403，耗时 {elapsed:.2f}s"
    if resp.status_code >= 400:
        return "BAD_STATUS", f"HTTP {resp.status_code}，耗时 {elapsed:.2f}s"

    hits = [h for h in CHALLENGE_HINTS if h in body_head]
    # 正常页面里也可能出现 document.cookie（统计脚本），所以要求同时不像 JSON/HTML 正文
    if len(hits) >= 2 and not body_head.lstrip().startswith(("{", "[")):
        return "JS_CHALLENGE", f"命中 JS 挑战特征 {hits}，耗时 {elapsed:.2f}s"
    return "OK", f"HTTP {resp.status_code}，{len(body)} 字节，耗时 {elapsed:.2f}s"


def probe(url: str, kw: str, timeout: float, show: bool, proxy: str = "", expect_links: bool = True) -> dict:
    target = url.replace("{kw}", requests.utils.quote(kw)) if "{kw}" in url else url
    err, resp, body, elapsed = "", None, "", 0.0
    started = time.perf_counter()
    # 默认和 app/config.py 的 use_env_proxy=False 对齐：本机 http_proxy 反而更慢，
    # 而且探针要回答的是「部署机直连能不能通」，走本地代理测出来的结论是假的。
    # 本地想复现「挂了代理能不能通」，显式给 --proxy。
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    try:
        resp = session.get(
            target,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": f"{requests.utils.urlparse(target).scheme}://{requests.utils.urlparse(target).netloc}/",
            },
            timeout=timeout,
        )
        elapsed = time.perf_counter() - started
        body = resp.text or ""
    except Exception as exc:  # 探测脚本不改判定口径，异常一律归类
        elapsed = time.perf_counter() - started
        err = f"{type(exc).__name__}: {exc}"

    state, reason = classify(resp, body, elapsed, err)
    links = []
    for raw in LINK_RE.findall(body):
        u = raw.rstrip(".,;)")
        links.append({"url": u, "platform": platform_of(u)})
    # 同一个分享的不同 URL 形态只留一条
    seen, uniq = set(), []
    for it in links:
        if it["url"] not in seen:
            seen.add(it["url"])
            uniq.append(it)

    pwds = PWD_RE.findall(body)

    # 只测根路径时本来就不该有网盘链接，判 NO_LINK 是误伤
    if expect_links and state == "OK" and not uniq:
        state, reason = "NO_LINK", reason + "；页面通但无网盘链接"

    return {
        "target": target,
        "state": state,
        "reason": reason,
        "links": uniq,
        "pwds": pwds,
        "show": show,
        "body": body,
    }


def report(r: dict) -> None:
    print(f"\n[{r['state']}] {r['target']}")
    print(f"  {r['reason']}")
    if r["links"]:
        by_platform: dict[str, int] = {}
        for it in r["links"]:
            by_platform[it["platform"]] = by_platform.get(it["platform"], 0) + 1
        dist = "、".join(f"{k}×{v}" for k, v in sorted(by_platform.items(), key=lambda kv: -kv[1]))
        print(f"  网盘链接 {len(r['links'])} 条（去重后）：{dist}")
        if r["pwds"]:
            print(f"  提取码候选 {len(r['pwds'])} 个：{', '.join(r['pwds'][:6])}")
        if r["show"]:
            for it in r["links"][:10]:
                print(f"    - [{it['platform']}] {it['url']}")
    elif r["show"] and r["body"]:
        print(f"  正文前 300 字：{r['body'][:300]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="候选搜索源探针")
    ap.add_argument("--hosts", nargs="*", help="一批域名，只测根路径可达性")
    ap.add_argument("--url", help="具体搜索地址，支持 {kw} 占位")
    ap.add_argument("--kw", default="三体", help="探测关键词，默认「三体」")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--show", action="store_true", help="打印链接明细与正文片段")
    ap.add_argument("--proxy", default="", help="显式代理，如 http://127.0.0.1:16796。留空=直连（和线上一致）")
    args = ap.parse_args()

    if not args.hosts and not args.url:
        ap.print_help()
        return 2

    if args.hosts:
        targets = [f"https://{h}/" if "://" not in h else h for h in args.hosts]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
            pool.map(
                lambda u: probe(u, args.kw, args.timeout, args.show, args.proxy, expect_links=False),
                targets,
            )
        )
        ok = sum(1 for r in results if r["state"] == "OK")
        for r in results:
            report(r)
        print(f"\n合计 {len(results)} 个，OK {ok} 个，可达率 {ok / len(results):.0%}")
        print("注意：根路径 OK 只代表「能打开」，不代表「能搜」。加源前务必用 --url 测一次真实搜索。")
        return 0

    r = probe(args.url, args.kw, args.timeout, args.show, args.proxy)
    report(r)
    return 0 if r["state"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
