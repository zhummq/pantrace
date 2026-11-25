"""用桩替换 AstrBot 的 API，专测调度：批、滞留、AI 结算、卸载排空。"""
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "stubs"))
sys.path.insert(0, str(HERE.parent.parent / "astrbot-plugin-pantrace"))

import main as plugin  # noqa: E402


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class Obj:
    pass


def make_event(mid, text, *, gid="200", sender="111", role="owner"):
    ev = Obj()
    ev.message_str = text
    ev.unified_msg_origin = f"aiocqhttp:GroupMessage:{gid}"
    ev.get_group_id = lambda: gid
    ev.get_sender_id = lambda: sender
    ev.get_self_id = lambda: "999"
    ev.get_sender_name = lambda: "老王"
    ev.plain_result = lambda s: s
    mo = Obj()
    mo.message_id = mid
    mo.raw_message = {"sender": {"role": role}}
    mo.message = []
    ev.message_obj = mo
    return ev


async def feed(p, ev):
    async for _ in p.on_group_message(ev):
        pass


def build(report_delay=0.0, ai=False, settle=15):
    cfg = Cfg(
        base_url="http://example.test",
        ingest_token="t",
        capture_enabled=True,
        capture_groups=["200"],
        capture_roles=["owner", "admin"],
        ai_structure=ai,
        ai_settle=settle,
        ai_timeout=60,
    )
    p = plugin.PanTraceCollector(None, cfg)
    calls = []

    async def fake_report(chunk):
        calls.append([i.payload() for i in chunk])
        if report_delay:
            await asyncio.sleep(report_delay)
        return True, {"links": 1, "added": 1, "touched": 0, "dropped": []}

    p._report = fake_report
    return p, calls


async def case_stranded():
    """关键回归：发送器忙的时候进来的消息，必须自己发出去，不能等下一条消息。"""
    p, calls = build(report_delay=1.0)
    await feed(p, make_event("m1", "三体 https://pan.quark.cn/s/aaa"))
    await asyncio.sleep(1.4)          # 第一批已发出、正在等后端（含 1s 合并窗口）
    assert len(calls) == 1, f"第一批应该已经在飞，实际 {len(calls)}"
    await feed(p, make_event("m2", "狂飙 https://pan.quark.cn/s/bbb"))
    t0 = time.time()
    while time.time() - t0 < 3 and len(calls) < 2:
        await asyncio.sleep(0.1)
    assert len(calls) == 2, f"第二条没被发出去（滞留），实际 {len(calls)}"
    assert [m["id"] for m in calls[1]] == ["m2"]
    print(f"  ✓ 不滞留：第二条在 {time.time() - t0:.1f}s 内自行发出")
    await p.terminate()


async def case_batch():
    """同一节拍内进来的几条应该合成一次 POST。"""
    p, calls = build()
    for i in range(3):
        await feed(p, make_event(f"b{i}", f"片子{i} https://pan.quark.cn/s/x{i}"))
    t0 = time.time()
    while time.time() - t0 < 3 and not calls:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    assert len(calls) == 1 and len(calls[0]) == 3, calls
    print("  ✓ 合并成 1 次 POST / 3 条消息")
    await p.terminate()


async def case_ai_slow_expire():
    """模型比 ai_settle 慢：先按规则发走，不能把链路堵在这儿。"""
    p, calls = build(ai=True, settle=4)

    async def slow(prompt, system):
        await asyncio.sleep(9)
        return json.dumps({"items": [{"title": "三体", "shareUrl": "https://pan.quark.cn/s/aaa"}]})

    p._llm_text = slow
    t0 = time.time()
    await feed(p, make_event("s1", "三体 https://pan.quark.cn/s/aaa"))
    while time.time() - t0 < 6 and not calls:
        await asyncio.sleep(0.05)
    waited = time.time() - t0
    assert calls and waited < 6, f"应该 ~4s 就发走，实际 {waited:.1f}s"
    assert "items" not in calls[0][0], "超时的那条不该带 AI 结果"
    print(f"  ✓ 模型慢时 {waited:.1f}s 就按规则发走（ai_settle=4）")
    await p.terminate()


async def case_ai_fast():
    p, calls = build(ai=True, settle=8)

    async def fast(prompt, system):
        return '好的：\n```json\n{"items":[{"title":"三体 全集","shareUrl":"https://pan.quark.cn/s/aaa","sharePwd":"8k2x"}]}\n```'

    p._llm_text = fast
    await feed(p, make_event("f1", "三体 https://pan.quark.cn/s/aaa 提取码 8k2x"))
    t0 = time.time()
    while time.time() - t0 < 5 and not calls:
        await asyncio.sleep(0.05)
    assert calls and calls[0][0].get("items"), calls
    assert calls[0][0]["items"][0]["title"] == "三体 全集"
    print("  ✓ AI 秒回时带上 items，且能剥掉 ```json 围栏")
    await p.terminate()


async def case_no_provider():
    p, calls = build(ai=True, settle=8)

    async def none(prompt, system):
        return None

    p._llm_text = none
    await feed(p, make_event("n1", "三体 https://pan.quark.cn/s/aaa"))
    t0 = time.time()
    while time.time() - t0 < 5 and not calls:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)
    assert calls and "items" not in calls[0][0]
    print("  ✓ 没模型时立刻按规则发走，不空等")
    await p.terminate()


async def case_drain_on_terminate():
    """重载/卸载不能丢在途数据。"""
    p, calls = build(report_delay=0.2)
    for i in range(3):
        await feed(p, make_event(f"d{i}", f"片子{i} https://pan.quark.cn/s/x{i}"))
    await p.terminate()
    total = sum(len(c) for c in calls)
    assert total == 3, f"卸载时丢了数据：只发了 {total} 条"
    print(f"  ✓ terminate 排空：{total}/3 条全部发出")


async def case_retry():
    """后端抖一下：重试后仍要成功，且不重复入库。"""
    p, calls = build()
    state = {"n": 0}

    async def flaky(chunk):
        state["n"] += 1
        if state["n"] == 1:
            return False, {"msg": "ClientError"}
        calls.append([i.payload() for i in chunk])
        return True, {"links": 0, "added": 0, "touched": 0,
                      "dropped": [{"id": "r1", "reason": "无有效链接"}]}

    p._report = flaky
    await feed(p, make_event("r1", "三体 https://pan.quark.cn/s/aaa"))
    t0 = time.time()
    while time.time() - t0 < 6 and not calls:
        await asyncio.sleep(0.05)
    assert calls, "重试没生效"
    print(f"  ✓ 首次失败后重试成功（第 {state['n']} 次尝试）")
    await p.terminate()


def case_collapse():
    text = "三体 https://pan.quark.cn/s/aaa\n卡片 https://pan.quark.cn/s/aaa\n狂飙 https://pan.quark.cn/s/bbb"
    got = plugin._collapse_same_url(text)
    assert got.count("https://pan.quark.cn/s/aaa") == 1
    assert got.count("https://pan.quark.cn/s/bbb") == 1
    print("  ✓ 同一条消息里重复的链接只留一次，不同链接不受影响")


async def case_terminate_fast():
    """后端完全不可达时，重载不能卡住——排空只试一次，不退避。"""
    p, calls = build()

    async def dead(chunk):
        return False, {"msg": "ClientError"}

    p._report = dead
    await feed(p, make_event("t1", "三体 https://pan.quark.cn/s/aaa"))
    await asyncio.sleep(0.2)
    t0 = time.time()
    await p.terminate()
    cost = time.time() - t0
    assert cost < 8, f"卸载卡了 {cost:.1f}s"
    print(f"  ✓ 后端不可达时卸载 {cost:.1f}s 结束（不跑退避）")


async def case_adapt():
    """模型比下限慢一点：第一条踩线，第二条起阈值自动放大，不再踩。

    这正是"一会可以一会不可以"的现场：写死 15 秒，17 秒的模型每一条都掷硬币。
    """
    p, calls = build(ai=True, settle=3)   # 下限 3 秒
    hit = {"n": 0}

    async def slow(prompt, system):
        if hit["n"] == 0:
            hit["n"] += 1
            await asyncio.sleep(4)        # 第一条：4 秒，超过 3 秒下限
        else:
            await asyncio.sleep(4)
        return '{"items":[{"title":"指环王 力量之戒 S3","shareUrl":"https://pan.quark.cn/s/a"}]}'

    p._llm_text = slow
    await feed(p, make_event("a1", "指环王 https://pan.quark.cn/s/a"))
    t0 = time.time()
    while time.time() - t0 < 8 and not calls:
        await asyncio.sleep(0.05)
    assert calls and "items" not in calls[0][0], "第一条应该踩线，走规则解析"
    # 等迟到的样本落进窗口
    t0 = time.time()
    while time.time() - t0 < 3 and len(p._ai_lat) < 1:
        await asyncio.sleep(0.05)
    assert len(p._ai_lat) == 1 and p._ai_lat[0] > 3.5, list(p._ai_lat)
    assert p._ai_wait() > 5, f"阈值没放大：{p._ai_wait()}"

    calls.clear()
    await feed(p, make_event("a2", "指环王 https://pan.quark.cn/s/a"))
    t0 = time.time()
    while time.time() - t0 < 12 and not calls:
        await asyncio.sleep(0.05)
    assert calls and calls[0][0].get("items"), "第二条应该等到模型并带上 items"
    print(f"  ✓ 自适应：首条踩线后阈值 3s → {p._ai_wait():.1f}s，第二条用上 AI 标题")
    await p.terminate()



class FakeProvider:
    def __init__(self, model):
        self.provider_config = {"model": model}

    async def text_chat(self, prompt, system_prompt=None):
        return "ok"


class FakeCtx:
    """最小的 AstrBot 上下文：只提供取模型的三个入口。"""

    def __init__(self, provider, pid=""):
        self._provider = provider
        self._pid = pid

    def get_current_chat_provider_id(self, umo=""):
        return self._pid

    def get_using_provider(self, **kwargs):
        return self._provider

    def get_provider_by_id(self, provider_id=""):
        return self._provider


async def case_model_name():
    """必须能说出"当前用的是哪个模型"，且只在模型变化时打一次。"""
    p, _ = build()
    p.context = FakeCtx(FakeProvider("kimi-k2-0905-preview"))
    logs = []
    real_info = plugin.logger.info
    plugin.logger.info = lambda msg, *a: logs.append(msg % a if a else msg)
    try:
        await p._llm_text("hi", "sys")
        assert p._model_name == "kimi-k2-0905-preview", p._model_name
        hits = [x for x in logs if "使用模型" in x]
        assert len(hits) == 1, hits
        assert "kimi-k2-0905-preview" in hits[0] and "默认模型" in hits[0], hits[0]

        await p._llm_text("hi", "sys")            # 第二次不该再刷一遍
        assert len([x for x in logs if "使用模型" in x]) == 1
        print(f"  ✓ 模型名进日志：{hits[0].strip()}（同模型只打一次）")
    finally:
        plugin.logger.info = real_info

    # 配了「指定提供商 ID」时，日志要带上这个 ID，方便对着 WebUI 核对
    p2, _ = build()
    p2.config["ai_provider"] = "deepseek-main"
    p2.context = FakeCtx(FakeProvider("deepseek-chat-v3"), pid="deepseek-main")
    logs.clear()
    plugin.logger.info = lambda msg, *a: logs.append(msg % a if a else msg)
    try:
        await p2._llm_text("hi", "sys")
        hits = [x for x in logs if "使用模型" in x]
        assert hits and "deepseek-main" in hits[0] and "deepseek-chat-v3" in hits[0], hits
        print(f"  ✓ 带提供商 ID：{hits[0].strip()}")
    finally:
        plugin.logger.info = real_info

    # 插件刚加载就该自报模型，不用等群里来消息
    p3, _ = build()
    p3.context = FakeCtx(FakeProvider("qwen3-235b-a22b"))
    logs.clear()
    plugin.logger.info = lambda msg, *a: logs.append(msg % a if a else msg)
    try:
        await p3.initialize()
        hits = [x for x in logs if "使用模型" in x]
        assert hits and "qwen3-235b-a22b" in hits[0], logs
        print(f"  ✓ 加载即自报：{hits[0].strip()}")
    finally:
        plugin.logger.info = real_info
        await p.terminate()
        await p2.terminate()
        await p3.terminate()


async def case_concurrency():
    """AI 并发必须被压住：群里刷屏时不能同时打 12 个模型请求出去。"""
    p, calls = build(ai=True, settle=30)
    live = {"now": 0, "peak": 0}

    async def slow(prompt, system):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.3)
        live["now"] -= 1
        return '{"items":[]}'

    p._llm_text = slow
    for i in range(9):
        await feed(p, make_event(f"c{i}", f"片子{i} https://pan.quark.cn/s/x{i}"))
    await asyncio.sleep(1.6)
    assert live["peak"] <= 3, f"模型并发超了：{live['peak']}"
    print(f"  ✓ 9 条并进，模型并发峰值 {live['peak']}（上限 3）")
    await p.terminate()


async def main():
    case_collapse()
    for fn in (case_batch, case_stranded, case_ai_fast, case_ai_slow_expire,
               case_no_provider, case_drain_on_terminate, case_retry,
               case_terminate_fast, case_adapt, case_model_name,
               case_concurrency):
        await fn()
    print("\n全部通过")


asyncio.run(main())
