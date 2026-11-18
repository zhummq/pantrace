"""临时造一份演示用的埋点库，只为了在本地看清板长什么样。

写进 %TEMP%/pt_demo_analytics.db，通过 ANALYTICS_DB 环境变量指给后端，
不碰 backend/data 里的真实数据。
"""
import os
import random
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
PATH = os.path.join(tempfile.gettempdir(), "pt_demo_analytics.db")
if os.path.exists(PATH):
    os.remove(PATH)

random.seed(20261118)

KEYWORDS = [
    ("流浪地球 3 4K", 38), ("三体 全集 蓝光", 26), ("黑神话 悟空 存档", 19),
    ("百家讲坛 全集", 15), ("2026 考研 数学", 11), ("漫长的季节 1080p", 9),
    ("沙丘 2", 8), ("权力的游戏 全集", 7), ("奥本海默 4K", 5),
]
PLATFORMS = ["quark", "baidu", "uc", "xunlei", "guangdong"]
TARGETS = [
    "流浪地球 3 · 4K 修复版", "三体 全集 蓝光", "漫长的季节", "黑神话 悟空 存档",
    "百家讲坛 全集", "沙丘 2", "权力的游戏 全集 1080p",
]
UAS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0",
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/119.0 Mobile",
]
BOT_UA = "python-requests/2.31.0"


def day_of(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


conn = sqlite3.connect(PATH)
conn.execute(
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, kind TEXT,
        visitor TEXT, session TEXT, ip TEXT, ua TEXT, referer TEXT, path TEXT,
        query TEXT, platform TEXT, target TEXT, url TEXT, ok INTEGER,
        duration_ms INTEGER, result_count INTEGER, cached INTEGER, device TEXT,
        is_new INTEGER, bot INTEGER, source TEXT, meta TEXT)"""
)

now = time.time()
today = datetime.fromtimestamp(now, TZ).date()
rows = []


def add(ts, kind, **kw):
    ua = kw.get("ua", random.choice(UAS))
    rows.append((
        ts, day_of(ts), kind,
        kw.get("visitor", "v_%07d" % random.randint(0, 9999999)),
        "s_1", kw.get("ip", "103.24.%d.%d" % (random.randint(1, 250), random.randint(1, 250))),
        ua, kw.get("referer", ""), "/", kw.get("query", ""), kw.get("platform", ""),
        kw.get("target", ""), kw.get("url", ""), 1 if kw.get("ok", True) else 0,
        kw.get("duration_ms", 0), kw.get("result_count", -1), kw.get("cached", 0),
        "mobile" if "Mobile" in ua or "iPhone" in ua else "desktop",
        kw.get("is_new", 0), 1 if kw.get("bot") else 0, kw.get("source", ""),
        kw.get("meta", ""),
    ))


# 14 天：前 7 天当环比基准，后 7 天是"当前"
for back in range(13, -1, -1):
    d = today - timedelta(days=back)
    is_prev = back >= 7
    base = 163 if is_prev else 183          # 日均搜索
    for _ in range(random.randint(base - 14, base + 14)):
        # 时间分布：晚上和上午两个峰
        hour = random.choices(
            range(24),
            weights=[1, 1, 1, 1, 1, 2, 4, 7, 11, 9, 7, 6, 5, 5, 5, 6, 7, 9, 12, 14, 13, 10, 6, 3],
        )[0]
        ts = datetime(d.year, d.month, d.day, hour, random.randint(0, 59),
                      random.randint(0, 59), tzinfo=TZ).timestamp()
        if ts > now:
            continue
        pv = random.random()
        if pv < 0.12:
            add(ts, "pv", referer=random.choice(["https://www.baidu.com/", "https://tieba.baidu.com/", ""]))
        kw, weight = random.choices(KEYWORDS, weights=[w for _, w in KEYWORDS])[0]
        failed = random.random() < 0.008
        zero = (not failed) and random.random() < (0.28 if is_prev else 0.312)
        add(
            ts, "search", query=kw, platform=random.choice(PLATFORMS),
            ok=not failed, duration_ms=random.randint(280, 900) if not failed else random.randint(2000, 5200),
            result_count=0 if (zero or failed) else random.randint(3, 60),
            cached=1 if random.random() < 0.42 else 0,
            is_new=1 if random.random() < 0.55 else 0,
            meta='{"page": 1}' if not failed else '{"page": 1, "error": "upstream timeout"}',
        )
        if not failed and not zero and random.random() < (0.245 if is_prev else 0.274):
            t = random.choice(TARGETS)
            add(ts + 3, "click", target=t, platform="quark",
                url="https://pan.quark.cn/s/%06x" % random.randint(0, 0xFFFFFF),
                query=kw, duration_ms=random.randint(200, 2400))

# 机器人流量：故意留几条，验证"已剔除"的说法
for _ in range(40):
    add(now - random.randint(0, 6 * 86400), "search", query="test", ua=BOT_UA, bot=True)

conn.executemany(
    """INSERT INTO events (ts, day, kind, visitor, session, ip, ua, referer, path,
       query, platform, target, url, ok, duration_ms, result_count, cached, device,
       is_new, bot, source, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    rows,
)
conn.commit()
n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
conn.close()
print("seeded", n, "events ->", PATH)
