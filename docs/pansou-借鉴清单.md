# PanSou 代码与设计逻辑分析 · 盘迹可借鉴清单

> 读的是 `D:/wangpan/pansou`（commit `221665f`，2026-10-12）。规模：Go 约 10.2 万行（含测试），
> 113 个插件目录 / `main.go` 注册 75 个 / Docker 默认启用 69 个，13 种网盘 + `others` 兜底。
> 结论只对"盘迹 PanTrace"当前这套 Python + Vue + 单上游 + 转存 SDK 的架构说事。

---

## 0. 结论先行

1. **PanSou 值得拿的不是"180 个源"，是四个机制**：链接存活检测、结果归一化+打分合并、缓存分层与单飞、"快返回+后台补齐"的时间预算。这四个迁到 Python 都不贵，且直接打在我们现有的痛点上。
2. **最该立刻抄的是链接检测**（`service/check_service.go`）。我们现在"链接失效"要等 2 个不同访客举报才隐藏；它用 8 家网盘的**公开接口**（不需要账号、不需要 cookie）当场判死活，五态 + 分级 TTL。这一个模块能把反馈工单里最大的一类自动闭环。
3. **不该抄的是它的并发哲学**。它的自适应并发靠"往上探"找上限，因为它打的是免费站点；我们的上游是计费套壳 API，往上探等于烧钱 + 招风控。出口闸和熔断要抄，AIMD 上探不要抄。TG 频道抓取同理，合规风险不对等，不碰。

---

## 1. PanSou 到底是怎么设计的

### 1.1 插件 = 数据源，注册表 + 优先级

`plugin/plugin.go:24` 定义 `AsyncSearchPlugin`：`Name()` / `Priority()` / `AsyncSearch()` / `SkipServiceFilter()`。
每个插件在自己的 `init()` 里 `RegisterGlobalPlugin()` 自注册，主程序只 import 一次。加一个源 = 加一个包，主流程零改动。

关键的两个设计细节，都是踩过坑才有的：

- **请求级状态走 `ext`，不放在插件实例上**（`plugin/plugin.go:66-98`）。插件是全局单例，早期用 `SetMainCacheKey()` 存"这次该写哪个缓存键"，两个并发请求互相覆盖，A 词的结果被写进 B 词的缓存槽。现在改成 per-request 的 `ext["_main_cache_key"]`，字段只作兜底。
- **`Priority()` 不只是排序用**，它直接进最终打分（见 1.5），一级源 +1000、四级源 -200。源的可信度是产品决策，写进代码而不是写进文档。

### 1.2 时间预算：4 秒同步窗口 + 波次推导批截止 + 后台补齐

不是"每插件 30 秒超时"那种笨办法（README 里写的 30 秒是旧的，代码 `config/config.go:532` 默认 **10 秒**）。

```
请求进来 → 所有源并发开跑
  ├─ 4s 内返回的 → 立即返回给用户（ASYNC_RESPONSE_TIMEOUT）
  └─ 超窗的 → 转后台继续跑，跑完 merge 进主缓存（isFinal=true）
```

**副作用要看清**：返回给客户端的 `SearchResponse` 里没有 `partial` / `is_final` 字段，客户端无法知道"还有源在补"。用户第一次搜拿到 300 条，30 秒后再搜同一个词拿到 1200 条——体验上是玄学。这是他们的**缺陷**，我们抄机制时应该补一个显式字段。

### 1.3 四层并发调度

| 层 | 做法 | 位置 |
|---|---|---|
| 出口闸 | 固定容量信号量（默认 128），出网前取名额，ctx 结束就放弃 | `service/outbound_gate.go:31` |
| 请求参数 | `conc` 只提供**下界**，`有效并发 = min(max(conc, 任务数), 出口上限)` | 同上 `:73` |
| 批截止 | `波次 = ceil(任务/并发)`，`截止 = 波次 × 每任务 p90 + 1s`，夹在 `[4s, 10s]` | 同上 `:113` |
| 自适应 | 排队（p90 > 地板×1.6）或丢弃 → ×0.85；顺畅 → +12.5%，落地到 JSON 文件 | `service/adaptive_concurrency.go:41` |

实测数字（`docs/系统开发设计文档.md` §12）：同一个查询，`conc=10` 时 10.23s / 821 条，改成按任务数给足后 5.24s / 1407 条。**墙是截止时间，不是并发。** 这条结论比代码本身值钱。

但同一份文档也记了一个自我否决：曾按 CPU 核数给起始并发设上限，实测结果数腰斩（459 vs 987）而耗时完全不变——"把某个量压小之前，先确认它是不是墙"。

### 1.4 缓存：二级 + isFinal 分层写 + 按键单飞 + 延迟批量写

- **内存分片 + 磁盘分片 + GOB 序列化**（`util/cache/`）。内存占磁盘配额的 3/5，淘汰时回写磁盘。
- **`SetWithFinalFlag`**：非最终结果只写内存，最终结果才同步写两层（`enhanced_two_level_cache.go:110`）。避免半成品污染磁盘缓存。
- **读-改-写必须按键加锁**（`service/main_cache_lock.go:23`，64 条分片锁）。多个源并发完成、都做 `Get → merge → Set`，不加锁就是后写覆盖先写，"明明搜到了只剩一个源"。这个 bug 我们的转存映射表将来一定会遇到。
- **磁盘缓存回填内存时用剩余 TTL**，不是重新计 60 分钟（`enhanced_two_level_cache.go:120` 那段注释）。否则磁盘 TTL 被整个绕过。

### 1.5 结果合并与打分排序

去重键：`UniqueID → MessageID → title+channel`。同键冲突时按**信息完整度**择优：

```
有唯一ID +10 / 有链接 +5+链接数 / 有正文 +3 / 标题长度/10 / 有频道 +2 / 标签数
```

排序是三项相加（`service/search_service.go:656`）：

| 项 | 取值 | 说明 |
|---|---|---|
| 源等级 | 1级 +1000 / 2级 +500 / 3级 0 / 4级 -200 | `:1941` |
| 新鲜度 | 1天内 500、3天 400、1周 300、1月 200、3月 100、1年 50、更早 20 | `:1959` |
| 优先词 | 命中标题时 `(词表长度 - 位置) × 70`，最高 400 | `:689` |

**等级分压过新鲜度分**（1000 vs 500），意味着可信源的一条旧资源会排在新源的新资源前面。这是刻意的：盗版站的日期基本是编的，源可信度比时间戳靠得住。这一点和我们很像——我们的上游 `indexedTime` 字段恒为空，本来就没有时间可用。

### 1.6 链接存活检测（最值钱的一块）

`service/check_service.go`，1588 行，覆盖 8 家网盘。核心思路：**只用各网盘的公开分享接口探活，不需要任何账号**。

以夸克为例（`:309`）：

```
POST https://drive-h.quark.cn/1/clouddrive/share/sharepage/token
body: {pwd_id, passcode, support_visit_limit_private_share: true}
  code 0                 → 拿到 stoken，继续查 detail（判违规/过期）
  code 41008             → locked（需要提取码）
  code 41004/41010/41011 → bad（失效）
  其他                   → 按 message 关键词兜底："不存在/失效/违规/过期/取消" → bad；"提取码/密码" → locked
```

五态：`ok` / `bad` / `locked` / `unsupported` / `uncertain`，**每态一个 TTL**（`:1133`：ok 24h / bad 6h / locked 12h / unsupported 24h / uncertain 30min）。bad 比 ok 短，是给误判留自愈窗口。另有：

- **单飞**：`inflight map[key]*activeCheckCall`，同一链接并发只打一次上游（`:177`）
- **持久缓存**：bbolt 落盘，重启不重验（`:1148`）
- **归一化键**：`platform + 标准化后的 pwd_id`，同一个分享的不同 URL 形态不重复检（`:1105`）
- **代理可传**：`proxy_url` 由调用方指定，同时进缓存 scope，避免"换了代理拿到旧结论"（`:1039`）

### 1.7 存活观测 + 源码守卫测试

- **`service/liveness.go`**：每源 20 轮滑动窗口，记 `yield` / `zero` / `fail`，暴露在 `/api/health`。作者明确写了"**只观测不淘汰**"的理由：窗口内零产出的源，请求照样能产出内容并进缓存，拿掉是丢数据。这条克制值得学。
- **`audit/*_guard_test.go`**：把"不许出现的写法"写成测试——响应体必须关闭、`ReadAll` 必须有上限、解压必须有炸弹防护。这类守卫测试不测功能，测**回归的方向**。

---

## 2. 对照盘迹：现状与缺口

| 维度 | 盘迹现在 | PanSou 做法 | 差距性质 |
|---|---|---|---|
| 搜索源 | **1 条**（idatariver MAPI），签名过期即全站不可用 | 75 源并发，单源挂掉无感 | 致命单点 |
| 延迟 | 全局串行 `MIN_INTERVAL=2s`，最慢 75s 超时 | 4s 窗口返回部分 + 后台补齐 | 结构性慢 |
| 缓存 | 进程内 dict，重启即空，TTL 5 分钟 | 内存+磁盘二级，isFinal 分层，单飞 | 冷启动全打上游 |
| 结果排序 | 沿用上游顺序，无自有打分 | 源等级 + 新鲜度 + 优先词 | 不可控 |
| 失效链接 | 2 个不同访客举报 → 30 天内隐藏 | 8 家公开接口预检，五态 + TTL | **高价值缺口** |
| 源健康 | `/api/health` 只有 lastError 一句话 | 每源 20 轮滑窗 yield/zero/fail | 排障靠猜 |
| 去重 | 无（上游已去重） | 归一化 URL 键 + 完整度择优 | 接多源后必须有 |
| 输入上限 | 搜索参数有校验，但响应体大小不设限 | 请求体 1MiB / 检测 256 条 / 解压炸弹防护 | 放大风险 |

---

## 3. 建议按这个顺序拿

### P0-1 链接存活检测（预计 1.5 天，收益最大）

**为什么先做这个**：它是唯一一个"用户能直接感知、且我们现在完全没有"的能力。现在的结果页里混着大量已删分享，用户点开 → 转存失败 → 报"链接失效" → 要凑够 2 个人才隐藏。检测上之后，第一次有人点之前这条就已经标红了。

**落地方案**

1. 新建 `backend/app/check.py`（单文件，不碰现有模块）：

```python
STATE_OK = "ok"; STATE_BAD = "bad"; STATE_LOCKED = "locked"
STATE_UNSUPPORTED = "unsupported"; STATE_UNCERTAIN = "uncertain"

_TTL = {"ok": 6 * 3600, "bad": 24 * 3600, "locked": 24 * 3600,
        "unsupported": 7 * 86400, "uncertain": 3600}

def check_one(platform: str, url: str, pwd: str = "", timeout: float = 8.0) -> tuple[str, str]:
    """返回 (state, summary)。不抛异常，任何异常都归 uncertain。"""
```

2. 建表（跟 `analytics.db` 同库，或单独 `links.db`）：

```sql
CREATE TABLE IF NOT EXISTS link_check (
  key        TEXT PRIMARY KEY,   -- f"{platform}:{pwd_id}"
  platform   TEXT NOT NULL,
  url        TEXT NOT NULL,
  state      TEXT NOT NULL,
  summary    TEXT DEFAULT '',
  checked_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lc_exp ON link_check(expires_at);
```

3. 检测点先只做三家（覆盖我们 80% 的结果）：**夸克**（`drive-h.quark.cn/.../sharepage/token`，照抄 `check_service.go:309` 的 code 映射）、**UC**（同构，`:439`）、**百度**（`:468`，要 verify + init 两步）。其余先返回 `unsupported`，前端不显示角标，别硬凑。

4. 调用方式：**不在搜索链路里同步做**。搜索返回后 `background.add_task`，只检当页 20 条里命中缓存 miss 的，`ThreadPoolExecutor(max_workers=6)`，结果写表。前端下次拿到同一批结果时读表标角标（`item.state`）。这样搜索延迟一点不变。

5. 出口过滤复用现有机制：在 `_apply_hidden()` 旁边加 `_drop_checked_bad()`，`state == "bad"` 且未过期 → 从 `items` 里剔除。**注意**：现有注释写了"过滤只发生在响应出口，缓存里存原始结果"，检测表也要照这个原则，别往 `_cache` 里写加工后的数据。

**坑**：夸克检测要带 `origin` / `referer`，不带会被当爬虫；`pwd_id` 从 `sdk/urls.py` 的解析结果取，别自己再写一套正则。

### P0-2 缓存升级：单飞 + 陈旧可用 + 落盘（0.5 天）

现在的 `_cache` 是 `dict[str, (ts, body)]`，三个问题：重启全空、5 分钟内同一词 N 个用户各自等 2 秒串行间隔、缓存过期瞬间并发全打到上游。

改成 `backend/app/cache.py`：

```python
# fresh 期：直接返回，不刷新
# stale 期（fresh ~ fresh+30min）：立刻返回旧结果 + 后台刷新（用户无感）
# 过期：单飞 —— 第一个请求去抓，其余等它的结果，不重复打上游
def get(key, *, fetch): ...  # fetch 是回源函数
```

单飞用 `dict[key] = threading.Event()` 实现，20 行内。落盘用 SQLite 一张 `search_cache(key, body, fresh_until, stale_until, complete)`，`cache_max_entries` 的淘汰逻辑保留。

**顺带修一个真问题**：现在 `MIN_INTERVAL=2s` 是**全局**串行，两个不同关键词也要排队。改成按上游分组的令牌桶（同一上游 2s 一个），缓存命中不占桶。这样用户 A 搜"三体"不会卡住用户 B 搜"庆余年"。

### P1-1 搜索源插件化 + 第二通道（3 天）

不是为了"多"，是为了**签名过期时不至于全站死**。接口设计照 PanSou 但用 Python 的写法：

```python
# backend/app/sources/__init__.py
class Source(Protocol):
    name: str
    level: int          # 1 最可信 ~ 3
    timeout: float
    enabled: bool
    def search(self, kw: str, page: int, page_size: int) -> list[Item]: ...

REGISTRY: dict[str, Source] = {}
def register(src): REGISTRY[src.name] = src
```

- 先把现有 `providers.py` 包成 `sources/idr.py`（零行为变化，纯重构）
- 第二通道**别照搬 69 个**。从 PanSou 里挑 2 个"有公开 JSON 接口、代码最短"的改写成 Python（`plugin/pansearch` 是 Next.js `_next/data` 接口，`plugin/yunso` / `plugin/qupansou` 也是纯 HTTP）。只做 2 个，跑两周看真实产出率再决定加不加。
- 响应加两个字段：`sources: [{name, state, count, ms}]`、`partial: bool`。前端在结果数旁边显示"3 个源 · 1 个超时"，用户知道为什么少。

**这条的红线**：我们 README 里写过"以前挂过降级链，签名失效被悄悄转到慢通道，日志里只剩慢"。所以多源必须**每个源独立报错、独立计时、都能在 /api/health 看见**，不许静默 fallback。

### P1-2 归一化 + 去重 + 打分（和 P1-1 同批做）

多源一来，同一个夸克链接会从三个源各来一遍。抄 `mergeSearchResults`：

- 去重键：`platform + 归一化的 share id`（`sdk/urls.py` 已经能解析，复用它，别新写）
- 冲突择优：完整度打分（有提取码 +3、有 fileCount +2、有 sizeBytes +2、标题更长 +len/10）
- 排序分：`源等级(1000/500/0) + 完整度 + 有提取码(+100) + 关键词命中`
- **时间项暂时不启用**——我们上游没有 datetime，凭空造一个"索引时间"会让排序变成噪声。等第二通道真能带时间再加。

### P2-1 源存活观测（半天）

`liveness` 每个源保留最近 20 轮的 `yield/zero/fail`，进 `/api/health`，同时在 `/admin` 加一个小面板。照 PanSou 的克制：**只展示不自动禁用**。

### P2-2 出口闸 + 熔断（1 天）

- 每源一个信号量（并发上限按实测 p90 定，从 3 起步）
- 熔断：连续 3 次超时/5xx → 打开 60s（该源本轮直接返回空 + state=open），半开探测一次
- **不抄 AIMD 上探**：我们的上游按次计费且有风控，往上探的代价是真金白银。固定水位 + 熔断够用。

### P2-3 上限守卫 + 守卫测试（半天）

- 上游响应体上限（我们现在 `resp.json()` 直接吃，没上限）
- `/api/resolve` 已有单 IP 限流，但缺"单次批量条数"上限
- 写 `tests/test_guards.py`：断言 `providers.py` / `sources/*` 里每个 `requests.*` 调用都带 `timeout=`，断言响应读取前有 size 检查。用 AST 扫源码，不测行为。

---

## 4. 明确不该抄的

| 项 | 理由 |
|---|---|
| TG 频道抓取 | 需要代理 + 来源是侵权内容集散地，合规风险和我们"网盘聚合搜索"的定位不对等 |
| 69 个插件照单全收 | 目标站多为盗版影视/资源站，维护成本极高（作者 10 万行里一半是插件适配），且我们的价值在转存不在数量 |
| 自适应并发上探（AIMD） | 打免费站找上限是赚的；打计费 API 是烧钱 + 招风控 |
| 分片内存/磁盘缓存 + GOB | Go 特有的性能工程。Python 单进程，SQLite + LRU dict 足够，抄了只是增加故障面 |
| bbolt / JWT / CORS 全家桶 | 我们有 SQLite 和口令门，重复建设 |
| 部分结果不告知客户端 | 这是他们的缺陷。我们要显式 `partial` |

---

## 5. 如果只做一件事

**做 P0-1 链接检测。** 它不依赖任何架构改动、不增加上游依赖、不和现有缓存/转存逻辑打架，但它直接把"用户点开发现是死链"这个最大的体验断点从"事后 2 人举报"提前到"事前预判"。附带好处：反馈工单里 `dead_link` 这一类会自然下降，看板上的信号变干净。

---

## 附：可复用代码索引

| 用途 | 位置 |
|---|---|
| 8 家网盘公开检测接口 + code 映射 | `service/check_service.go:240`（阿里）/ `:309`（夸克）/ `:439`（UC）/ `:468`（百度）/ `:547`（天翼）/ `:671`（123）/ `:714`（迅雷）/ `:800`（115）/ `:888`（移动） |
| 五态 + 分级 TTL | `service/check_service.go:34`、`:1133` |
| 检测单飞 + 持久缓存 | `service/check_service.go:177`、`:1148` |
| 分享 URL 归一化（8 种） | `service/check_service.go:1105`、`:1409-1563` |
| 13 种网盘类型识别 | `util/regex_util.go:68` |
| 结果合并 + 完整度打分 | `service/search_service.go:106`、`:167` |
| 排序三项打分 | `service/search_service.go:656`、`:1941`、`:1959` |
| 出口闸 / 批截止 | `service/outbound_gate.go:31`、`:113` |
| 按键分片锁（读-改-写） | `service/main_cache_lock.go:23` |
| 存活观测滑窗 | `service/liveness.go:13-70` |
| 输入上限 | `api/limits.go:15-31` |
| 守卫测试写法 | `audit/body_close_guard_test.go`、`audit/readall_guard_test.go` |
| 插件接口 / 请求级 ext 传参 | `plugin/plugin.go:24`、`:66` |
| 加源的完整文档 | `docs/插件开发指南.md`（1255 行，含检查清单） |
