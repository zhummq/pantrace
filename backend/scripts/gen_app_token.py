"""生成小程序通道的长期令牌（APP_TOKEN）。

为什么需要它
------------
小程序不走浏览器，走的是我们自己的一层代理云函数。云函数的出口 IP 由 NAT 池
随机分配，而匿名票据是「按来源 IP 签发、换 IP 即失效」的——领票落在 A 出口、
搜索落在 B 出口，就是用户看到的「票据无效，请重试」。这不是 bug，是两条设计
撞在一起了：票据假定调用方 IP 稳定，云函数恰恰不满足。

于是给这类调用方换一张凭证：长期有效、不绑 IP、只在服务端之间流转。
带对了 `X-App-Token`，后端就跳过验票；但**限流照旧**，只是从"按 IP"改成
"按客户端摘要 + 通道总闸"。

用法
----
    # 1) 生成一张，打印出来（贴到两处：后端 .env 的 APP_TOKEN、
    #    云函数环境变量的 PANTRACE_APP_TOKEN）
    python scripts/gen_app_token.py

    # 2) 直接写进 backend/.env（已有 APP_TOKEN 行则替换，没有则追加）
    python scripts/gen_app_token.py --write

    # 3) 轮换：重新生成 + --write，然后同步改云函数环境变量。
    #    旧令牌立即失效，但不会影响普通浏览器用户——他们不用令牌。

注意
----
令牌是全局共享的一把钥匙，泄露等于任何人拿到都能免票调用。所以：
  - 只放服务端和云函数环境变量，**绝不写进小程序代码**（小程序包能被反编译）；
  - 泄露了就当它已经泄露：重新生成、两边同步、观察 APP_GLOBAL_PER_MINUTE 有没有被打满。
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
PREFIX = "ptk_live_"


def gen() -> str:
    # 32 字节 ≈ 43 位 urlsafe 字符，肉眼可辨识前缀便于在日志里 grep。
    return PREFIX + secrets.token_urlsafe(32)


def write_env(token: str) -> str:
    """把令牌写进 .env。已有 APP_TOKEN 行就改值，没有就追加到末尾。

    返回一句人话的结果，交给调用方打印——不在这里 print，便于测试。
    """
    if not ENV_PATH.exists():
        return f"没找到 {ENV_PATH}，先把 .env.example 复制一份再跑"

    text = ENV_PATH.read_text(encoding="utf-8")
    pattern = re.compile(r"^APP_TOKEN=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(f"APP_TOKEN={token}", text, count=1)
        action = "已更新"
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n# 小程序通道令牌（scripts/gen_app_token.py 生成）\nAPP_TOKEN={token}\n"
        action = "已追加"

    ENV_PATH.write_text(text, encoding="utf-8")
    # 令牌写进 .env 就够了，不回显全文；这里只报指纹长度方便核对
    return f"{action} {ENV_PATH}（{action}后需重启后端生效）"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成小程序通道令牌")
    parser.add_argument("--write", action="store_true", help="直接写入 backend/.env")
    parser.add_argument("--count", type=int, default=1, help="一次生成几张（轮换过渡期可用）")
    args = parser.parse_args()

    tokens = [gen() for _ in range(max(1, args.count))]
    for i, t in enumerate(tokens, 1):
        print(f"令牌 {i}：" if len(tokens) > 1 else "令牌：")
        print(f"  {t}")
        print()

    print("配置两处（缺一处都不生效）：")
    print("  1. 服务器 backend/.env            → APP_TOKEN=<上面这串>")
    print("  2. 云函数环境变量 netdisk          → PANTRACE_APP_TOKEN=<同一串>")
    print()
    print("小程序端不需要、也不应该拿到它。")

    if args.write:
        print()
        print(write_env(tokens[0]))
        if len(tokens) > 1:
            print("注意：只写入了第一张，其余留给轮换过渡期手工处理。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
