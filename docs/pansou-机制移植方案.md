# PanSou 核心机制移植方案（完整版）

> 目标：把 PanSou 的**插件机制 + 异步执行层 + 数据清洗 + 缓存 + 观测 + TG 通道**整套搬进 `backend/`，不是接它的源。
> 读的代码：`plugin/plugin.go`、`service/search_service.go`、`service/batch_search.go`、`service/liveness.go`、`service/tg_reachability.go`、`util/parser_util.go`、`util/regex_util.go`、`util/http_util.go`、`util/retry.go`、`util/pool/`、`model/response.go`。
> 现有约束：Python 3 + FastAPI + SQLite；上游 idatariver 单通道、`REQUEST_TIMEOUT=75`、`use_env_proxy=False`（`MIN_INTERVAL=2.0` 全局串行已在 P8 删除）。

---

## 0. 移植总分：37 个机制，34 搬 3 不搬

上一版只列了 7 组，等于把执行层搬了、把**观测层和防抖动层漏了**。那两层恰恰是 PanSou 踩了最多坑才长出来的。这一版按代码模块逐个清点，一项不漏。

| 层 | 机制数 | 全搬 | 改造 | 不搬 |
|---|---|---|---|---|
| A 插件层 | 4 | 3 | 1 | 0 |
| B 执行层 | 5 | 2 | 3 | 0 |
| C 缓存层 | 7 | 5 | 2 | 0 |
| D 清洗层 | 7 | 5 | 2 | 0 |
| E 合并层 | 5 | 4 | 1 | 0 |
| F 观测层 | 4 | 3 | 1 | 0 |
| G TG 通道 | 5 | 3 | 2 | 0 |
| H 明确不搬 | 3 | — | — | 3 |

“改造”不是打折，是 Go 的工程手段在 Python 里没有对应收益（比如分片锁）。下面逐项标了具体做法。

---

## 1. A 插件层（4 项）

### A1 自注册 + 白名单默认全关 ✅ 全搬

```go
// 每个插件包内
func init() { plugin.RegisterGlobalPlugin(NewHunhepanAsyncPlugin()) }
```

`RegisterGlobalPluginsWithFilter(enabled)`：`nil` 或空 = 一个都不启用。**默认全关这个默认值是对的**，新加的源必须显式批准才上线。

```python
# backend/app/search/registry.py
_REGISTRY: dict[str, BasePlugin] = {}

def register(cls):
    _REGISTRY[cls.name] = cls()
    return cls

def load_all() -> list[BasePlugin]:
    pkg = "app.search.plugins"
    for mod in pkgutil.iter_modules(importlib.import_module(pkg).__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{pkg}.{mod.name}")
    enabled = {s.strip() for s in settings.enabled_sources.split(",") if s.strip()}
    return [p for n, p in _REGISTRY.items() if n in enabled]
```

加一个源 = 加一个文件 + 环境变量里加个名字，`main.py` 零改动。

### A2 请求态走 ext，绝不写实例字段 ✅ 全搬

PanSou 的注释写了两遍：插件实例是全局单例，`SetMainCacheKey` / `SetCurrentKeyword` 在并发下会被别的请求覆盖，**A 关键词的结果会写进 B 的缓存槽**。它为此把 `ctx` 和 `mainCacheKey` 全塞进 `ext` 的副本里：

```go
func pluginExtWithContext(ext, ctx, mainCacheKey) map[string]interface{} {
    taskExt := make(map[string]interface{}, len(ext)+2)   // 复制，不是就地写
    ...
}
```

Python 同理：`Plugin.search(client, keyword, ext)`，实例上只有 `name`/`level`/`timeout` 这类不可变配置。**这条不遵守，并发一上来必串台。**

### A3 双超时客户端 ✅ 全搬

```go
GetClient()             // 4s   前台快速返回
backgroundHTTPClient()  // 30s  后台补齐用
```

两个 `httpx.AsyncClient`，连接池分开。前台短超时是为了“到点就走”，后台长超时是为了“走了还继续抓”。

### A4 配置延迟解析（不在 import 时读）🔧 改造

PanSou 的坑：插件在包 `init` 阶段构造，那时配置还没加载，读到的永远是默认值——它为此改成 `pluginClientTimeouts()` 调用时解析。

Python 的 import 时机可控，做法是**先 `load_settings()` 再实例化插件**，插件构造时读的是已加载的配置。比 Go 简单，但顺序不能反。

---

## 2. B 执行层（5 项）

### B1 4 秒响应窗口 + 后台补齐 ★ 最值钱

```go
select {
case results := <-resultChan:        // 4 秒内回来了
case err := <-errorChan:             // 失败 → 回退旧缓存
case <-time.After(responseTimeout):  // 4 秒到 → 返回空 + IsFinal=false，后台继续
}
```

Python 侧关键是 `asyncio.shield`：

```python
task = asyncio.create_task(self._fetch(key, keyword, ext))
try:
    results = await asyncio.wait_for(asyncio.shield(task), timeout=self.response_timeout)
    return PluginOutcome(results, is_final=True, source=self.name)
except asyncio.TimeoutError:
    return PluginOutcome([], is_final=False, source=self.name, pending=True)
```

**没有 `shield`，`wait_for` 超时会取消底层任务，“后台补齐”就是空话。**

直接收益：插件化前是 `MIN_INTERVAL=2.0` 全局串行 + 单通道 75 秒超时，两个用户并发搜不同词，第二个排在第一个后面。现在每源独立超时、独立窗口，**互不排队**（全局等待已删，见 P8）。

### B2 完整度标记贯穿全程 ✅ 全搬

`IsFinal` 不只在响应里，它一路传到缓存写入、后台补齐合并、以及“下次该不该重抓”。见 C2。

### B3 工作池 + 拿不到槽就降级 🔧 改造

```go
defaultMaxBackgroundWorkers = 20
defaultMaxBackgroundTasks   = 100
```

`acquireWorkerSlot()` 拿不到槽时**不阻塞，直接降级用短超时客户端执行**。

上一版我写“第一版直接排队，降级留到压测后”——**这条收回**。降级语义在 Python 里就是一个 `wait_for(sem.acquire(), 0)` 探试，成本极低：

```python
try:
    await asyncio.wait_for(self._sem.acquire(), timeout=0.01)
    long_client = True
except asyncio.TimeoutError:
    long_client = False          # 拿不到槽：降级用短超时客户端，不排队
```

排队会让我们重新引入“第二个用户等第一个”的老问题，而排队和降级只差三行代码。

### B4 重试 + Abort 语义 🔧 改造

```go
type abortError — 明确表示“别再试了”
func DoWithRetry(cfg RetryConfig, fn func(attempt int) error) error
```

`Abort` 的用途是区分**可重试**（超时、5xx）和**不可重试**（404 接口已删、403 永久封）。没有这个区分，重试只是在给死源加负载。

Python：一个 `retry(fn, attempts=2, on_abort=(AbortError,))`，**只对超时和 5xx 重试**，4xx 直接 abort。第一版给 1 次重试即可，别给 3 次——4 秒窗口里塞不下。

### B5 响应体大小上限 ✅ 全搬

```go
maxBytes := 2 * 1024 * 1024
body, _ := io.ReadAll(io.LimitReader(resp.Body, maxBytes))
```

httpx 侧用 `response.aiter_bytes()` 累计计数，超 2MB 断开。防的是一个异常响应把内存吃满——我们服务器内存不大，这条很实在。

---

## 3. C 缓存层（7 项）

### C1 缓存键三要素 ✅ 已做对
`name:keyword:ext_digest`。少 `name` 各源串台，少 `ext` 摘要会拿错结果。现有 `f"{query}|{platforms}|{page}|{page_size}|{scope}"` 已含全影响结果的参数，保持。

### C2 `complete` 语义 ✅ 全搬
```sql
complete INTEGER DEFAULT 0   -- 0=还在跑/部分，1=完整
```
`complete=0` 让缓存能表达“这条不完整，下次该重抓”，而不是把 4 秒超时返回的空结果当权威缓存钉住一小时。

### C3 整批零产出不写缓存 ⚠️ 上一版漏了

```go
// 插件路径下整批零产出不写缓存
if o.requireYield && o.yielded == 0 { return 0, false }
```

注释点破了 `complete()` 的盲区：**它只看“没有超时也没有失败”，于是“全部成功但零产出”会被判为完整，按完整 TTL 缓存一整个周期。**

这是最容易踩的坑：源改版后返回 200 + 空列表，所有健康检查都绿，缓存却把空结果钉了一小时。

**规则：本轮所有源都成功但总产出为 0 → 不写缓存。**

### C4 全失败不写缓存 ✅ 全搬
`if o.succeeded == 0 { return 0, false }`

### C5 写缓存要合并，不能覆盖 ⚠️ 上一版漏了，且是实测事故

```go
// 原先这里是 Set 整块覆盖，合并的只是"本请求快照 + 补齐结果"
// 但补齐发生在首轮写入之后很久，这期间任何一次重复搜索都可能已经把结果并进同一个键
// 实测（受控场景）覆盖写只剩 13 条、丢掉 74% 的并发结果
written := writeFinalMainCache(cache, cacheKey, merged, ttl)
```

**后台补齐写缓存必须“先读、按键合并、再写”，绝不是 `SET`。** 这条直接决定“补齐”是加分还是自毁。

Python 侧就是 SQLite 里 `INSERT ... ON CONFLICT DO UPDATE` 前先读出来合并，或者在同一事务里做。

### C6 过期重取 vs 缓存不死 ⚠️ 上一版说反了

PanSou 的实测结论很反直觉：

> 过期后重搜与前一次命中相比是 −32、−72、+5 条；真正让结果变多的是“缓存不死、后台合并往里加”（+55、+72），**不是过期重搜**。

所以它**删掉了“有超时就用短 TTL”这一档**，回到单一 TTL。上一版我写的“过期但非空先给旧的 + 后台刷”方向对，但要补上这句：**不要为了“新鲜”缩短 TTL，那会让结果变少。**

### C7 L1 内存 + L2 SQLite 两级 🔧 改造
PanSou 是内存 + 磁盘分片（Go 为了躲大 map 锁竞争）。我们 `dict` + SQLite 一张表足够，分片无收益（GIL）。

```sql
CREATE TABLE IF NOT EXISTS search_cache (
  cache_key   TEXT PRIMARY KEY,
  payload     TEXT NOT NULL,
  complete    INTEGER DEFAULT 0,
  created_at  INTEGER NOT NULL,
  last_access INTEGER NOT NULL,
  access_cnt  INTEGER DEFAULT 0
);
```

---

## 4. D 清洗层（7 项）

### D1 按网盘类型归并密码 ✅ 全搬

```go
type linkCandidates struct {
    direct []Link                       // 不需要归并的
    byType map[string]map[string]string // 类型 → baseURL → 密码
}
```

密码可能离链接很远（“链接：xxx 提取码：abcd”），百度/天翼/UC/123/115/阿里云这六类必须**等整段文本扫完**才知道配哪个密码，最后 `finalize()` 按固定顺序补全。

### D2 百度剥 `?pwd=` ✅ 全搬
链接存干净 base，密码单独存，输出时再拼回。这样同一个链接不同写法的去重才成立。

### D3 无密码链接也保留 ✅ 全搬
PanSou 注释原话：“不能因为没有提取码就把链接丢掉。” 没码的链接用户可能自己找码，丢掉就是丢结果。

### D4 提取码六级优先级 ✅ 全搬
天翼 `（访问码：）` → 迅雷 `?pwd=` → 通用 `?pwd=`/`password=` → 115 `password=` → 123「提取码：」→ 正文「提取码/密码」后跟 4 位。

### D5 `isValidPassword` 只允许字母数字 ✅ 全搬
挡掉大量误提取（把中文、标点当提取码）。

### D6 就近取密码窗口（60 字符）🔧 改造

```go
const nearbyPasswordWindow = 60
// 先用"这条链接自己附近"的短窗口取值，取不到才退回整条扫描
// 保证不退化：宁可回到旧行为，也不能因为附近没找到就返回空
```

60 字符对英文消息合适，**中文消息的信息密度更高，同样的字数承载更多内容**。改造点：窗口按“链接后 60 个字符 + 遇下一个链接截断”实现，中文场景先按 60 跑，用真实消息验证后再调。核心是保留**“就近优先、失败回退全文”**这个两段结构。

### D7 解析结构变化检测 ⚠️ 上一版漏了，且是致命项

```go
// 页面含消息块却解析不出条目，通常是站点改版；显式告警而不是静默返回空
if parseStatus == ParseStatusStructureChanged { log(...) }
```

`judgeParseStatus` 的逻辑值得照抄：

```go
if resultCount != 0 { return OK }
case doc.Find(".tme_no_messages_found").Length() > 0:  return NoMessages   // 真没内容
case recognized == 0 && doc.Find(".tgme_widget_message_wrap").Length() > 0:
    return StructureChanged                                                // 结构变了
```

**不能用 `len(results)==0` 判断失效**：消息识别成功但整条不含网盘链接时同样是 0 条，那是正常页面。

这条和我们上一轮讨论的“静默降级是致命问题”是同一件事：源改版后返回 200、解析出 0 条，如果不报警，用户以为“全网没有这个资源”。**每个源必须能把 `STRUCTURE_CHANGED` 状态报出来。**

---

## 5. E 合并层（5 项）

### E1 去重三级 key 降级 ✅ 全搬
`UniqueID` → `MessageID` → `title_channel`。

### E2 完整度优先（不是先到先得）✅ 全搬
```go
calculateCompletenessScore: UniqueID +10 | 有链接 +5+链接数 | 有内容 +3 | 标题长度/10 | 有频道 +2 | 标签数
```
同 key 撞上选**完整度高的**，不是保留先到的。这条比多数实现做得好。

### E3 URL 归一化再比较 ✅ 全搬
`normalizeUrl` 统一 host 大小写、去尾斜杠、去 tracking 参数，否则同一资源不同写法去不掉重。

### E4 三段打分 🔧 改造（权重可调）

```
总分 = 时间分 + 关键词分 + 源等级分

时间分：≤1天 500 / ≤3天 400 / ≤7天 300 / ≤1月 200 / ≤3月 100 / ≤1年 50 / 更早 20
关键词分：["合集","系列","全","完","最新","附","complete"]，索引 i 得分 (len-i)*70
等级分：level1 +1000 / level2 +500 / level3 0 / level4 -200
```

可解释是它最大的价值：搜「三体」时带「合集」「全」的整季资源排前面，这是业务判断，不是算法排序。

改造点：`priorityKeywords` 是英文语境下的中文词，我们做网盘资源搜索应该补 `["全集","完结","4K","蓝光","原盘","高清"]`。权重先按 1:1:1 跑，用真实搜索结果回归。

### E5 按网盘类型分组输出 ✅ 全搬
`merged_by_type: {quark: [...], baidu: [...]}`。前端有网盘筛选，这个结构正好对上，筛选时不用再扫一遍。

---

## 6. F 观测层（4 项）⚠️ 上一版整层漏了

这一层是 PanSou 最被低估的部分。没有它，“每源独立报错”只是一句口号——报了也没人看得懂。

### F1 失败分类 `failureClass` ✅ 全搬

```go
429 → "限流429"   403 → "禁止403"   404 → "不存在404"
5xx → "服务端5xx"  DeadlineExceeded → "超时"  net.Error → "网络错误"
```

我们上一轮实测的“五种死法”，正好对应这套分类：`hjzhencai` = 零产出、`sdso` = 状态码异常、`buerchen` = 404、`sopsanya` = 403/验证码、`t.me` = 超时。**有了分类，`/api/health` 才能一眼看出“这个源是死了还是被限流”。**

### F2 源存活分级 `liveness.classify` ✅ 全搬

```go
rounds < 最小轮次  → insufficient_data
yielded == 0 且 failed > 0 → failing
yielded == 0 且 failed == 0 → zero_yield     ← 最隐蔽：不报错但也不出结果
failed*2 > rounds → degraded
否则 → ok
```

`zero_yield` 是独立状态，这点非常关键：**不报错不代表健康**。源改版后返回 200 空列表，`failing` 抓不到它，只有 `zero_yield` 能。

### F3 慢项定位 `slowestTasks` 🔧 改造
记录每个任务耗时，摘要里输出最慢的 N 个。Python 侧简化成 `duration` 记进日志，前 3 名单独打。用于回答“这次是慢还是坏”。

### F4 非 200 不当作“无结果” ✅ 全搬

```go
// 429/403/5xx 等都不是可用页面，按失败上报
// 这样"频道被限流"与"频道没有匹配内容"不会混为一谈
if resp.StatusCode != http.StatusOK { return nil, &httpStatusError{...} }
```

**这条是整层的核心原则**：状态码非 200 一律算失败，不算“零结果”。否则限流会被误记成“这个频道没内容”，进而污染存活统计和缓存。

---

## 7. G TG 频道通道（5 项）——本次补入

上一版我把 TG 放进了“不搬”，理由是“实测完全不通”。**这个理由不成立**：不通的是本机，线上直连正常。TG 通道是 PanSou 唯一的“一手来源”（其余全是二手聚合站），不搬等于自断一半数据源。

### G1 可达性门 ★ 网络路径不确定时最该先搬的一块

```go
// 背景（实测）：t.me 被墙时不是"连接被拒绝"，而是"连接被静默丢包"
// 于是 111 个频道请求全部挂满超时，日志 `成功 0/111，超时未完成 111`
// 整阶段稳定 4.00 秒，换来的结果恒为 0 条
```

三条硬约束，缺一不可：

1. **探测只在后台跑，搜索路径只读状态**。塞进请求里等于拿“1.5 秒探测”换“4 秒空等”。
2. **门开时早返回，且不写缓存**。网络不通的空结果写进 60 分钟 TTL，会在网络恢复后继续骗人。
3. **拿不准一律按可达处理（fail-open）**。

参数：

```go
tgProbeBudget            = 3s    // 单次探测预算（PanSou 注释：经代理握手偶尔超 1.5s，放到 3s）
tgReachableTTL           = 5min  // 可达结论保鲜
tgUnreachableTTL         = 60s   // 不可达结论保鲜（故意短，恢复后 1 分钟内回来）
tgProbeCheckInterval     = 3s    // 后台检查间隔
tgProbeRequiredFailures  = 2     // 连续失败 2 次才敢关门
```

**`tgProbeRequiredFailures = 2` 这个设计必须照抄。** 注释写得很清楚：单次探测超时若直接判不可达，会把**可用的 TG 静默跳过**——这是该机制最危险的失效模式。所以首次失败只记连击、保持旧结论，3 秒内复检。

这条对我们尤其重要：线上直连时 t.me 偶尔握手慢（跨洋线路），**一次慢≠不可达**，判错一次就是整轮 TG 数据全丢且毫无报错。

判定标准是**“拿到了 HTTP 响应”而不是状态码**：404/403 同样证明网络通，只有传输层失败（DNS、连接、TLS、超时）才算不可达。用 `HEAD` 不用 `GET`（频道页正文 133KB，探测只需要一个比特；实测 HEAD 1.17~1.69s / 0 字节 vs GET 1.58~2.16s / 133KB）。

探测目标用**真实频道页**而不是 `t.me` 首页——“首页能开但 `/s/` 路径被单独阻断”是可能发生的。

### G2 门开时不写缓存、不计存活失败 ✅ 全搬

```go
if !TGReachable() { return nil, nil }   // 不写缓存，不计频道失败（这些频道根本没被试过）
```

Python 侧：一个 `_TGGate` 单例，`reachable` 读内存原子量，后台 `asyncio.Task` 每 3 秒检查一次。

### G3 频道并发 + 软截止 + 后台补齐比例门 🔧 改造

```go
batchTimeout := TGChannelTimeout            // 批任务软截止，默认跟随单频道超时
outcome.shouldBackfill(enabled): timedOut*3 <= total   // 超时超 1/3 就不补齐
```

比例门的理由：**缺失比例过高说明上游整体变慢，此时补齐只会把请求量再放大一倍。**

我们的改造点：频道数少（先配 3~5 个），软截止取 4 秒，补齐门同样保留 `timedOut*3 <= total`——这条不只是 TG 用，所有多源批次都该用。

### G4 TG 消息解析 ✅ 全搬

```python
# 结构对应 goquery CSS selector，lxml.cssselect 一一对应
.tgme_widget_message_wrap          # 每条消息
  .tgme_widget_message             # data-post="channel/123" → message_id
  .tgme_widget_message_date time   # datetime="RFC3339" → 时间
  .tgme_widget_message_text        # 正文（<br> 记为换行，不要序列化 HTML）
```

三个细节：

- **时间解析失败要丢弃这条**（`time.Parse` 失败 → 返回 not ok），不能给个零值时间混进排序
- **`UniqueID = channel + "_" + messageID`**，天然全局唯一，去重靠它
- **只有含链接的消息才进结果**，但“识别成功但无链接”要单独计数——这是 D7 判断结构变化的分母

**依赖（已实测确认缺失）**：

| 包 | 用途 | 当前状态 |
|---|---|---|
| `lxml` | t.me 页面解析 | ❌ 未装 |
| `cssselect` | 让 lxml 支持 CSS selector（与 goquery 一一对应） | ❌ 未装 |

**不需要 `socksio`**：线上服务器直连 t.me，不走 socks5 代理。`httpx 0.28.1` 已装，直连场景零额外依赖。

用 bs4 也能解析，但 lxml 快一个数量级，多源并发时差别明显。

### G5 网络路径：线上直连，不用代理 ⚠️ 关键约束

**线上服务器直连 t.me 可达，本机（开发环境）不可达。** 实测：本机无论直连还是走系统代理 `127.0.0.1:18924`，访问 `t.me/s/tgsearchers7` 都是 `http=000`、10 秒超时。

这个事实改变了两件事的设计：

**① 配置：`TG_PROXY` 字段保留但默认为空（直连）**

保留字段不是为了现在用，是为了「哪天线上直连也不通了」时不必改代码。默认空 = 直连：

```bash
# .env 新增
TG_ENABLED=false
TG_CHANNELS=tgsearchers7          # PanSou 的默认频道
TG_PROXY=                         # 留空=直连（线上就是这个配置）
TG_CHANNEL_TIMEOUT=4.0
TG_CHANNEL_REQUEST_TIMEOUT=4.0
TG_RESPONSE_MAX_BYTES=2097152
TG_BACKFILL_ENABLED=true
```

**② `httpx` 必须显式 `trust_env=False`，这条是硬要求**

本机环境里确实存在 `HTTP_PROXY` / `HTTPS_PROXY=http://127.0.0.1:18924`。`httpx.AsyncClient` 默认 `trust_env=True`，会**自动把这个代理吃进去**——于是「直连」这个配置在本机实际变成了「走一个连不通的代理」，结论和线上完全相反。

实测（本机 venv，httpx 0.28.1）：

```
--- trust_env=True（默认）---
   all://127.0.0.1  -> NoneType          # NO_PROXY 里的被排除
   all://localhost  -> NoneType
   all://[::1]      -> NoneType
   https://         -> AsyncHTTPTransport(proxy=AsyncHTTPProxy[...])   # ← 挂上了环境代理
   http://          -> AsyncHTTPTransport(proxy=AsyncHTTPProxy[...])

--- trust_env=False ---
   mounts 数量: 0                        # ← 真直连，不读任何环境变量
```

项目主上游已经踩过这个坑（`use_env_proxy=False` 就是为此设的，`probe_source.py` 用 `trust_env=False` 修过一次）。TG 客户端同样处理：

```python
self.client = httpx.AsyncClient(
    trust_env=False,          # 不读 HTTP_PROXY/HTTPS_PROXY，和 use_env_proxy=False 对齐
    proxy=settings.tg_proxy or None,   # None = 真直连
    timeout=4.0,
)
```

**不写 `trust_env=False`，本地开发时会得到一个假的「TG 不可达」结论，然后你去查代码，而代码没错。**

**③ 可达性门在直连场景下的价值反而更高**

线上直连时门确实大部分时间开着，但仍有三种情况必须靠它：

- **本机开发**：不通 → 门关 → 本地不会每次空等 4 秒（否则改一行代码要等 TG 超时）
- **t.me 临时抖动/被限流 429**：门关 → 3~5 个频道不会全部挂满超时
- **上线初期线上也抽风**：门关 → 报 `TG unreachable` 而不是「搜不到」

所以 G1 那一整套（后台探测 / fail-open / 连续 2 次才关门）**一个参数都不改**。

**④ 解析器只能离线验证，必须准备样本**

本机不通 = 拿不到真实页面 = 解析器无法本地跑通。解法是**在线上抓样本，本地做离线单测**：

```bash
# 线上服务器执行，存一页搜索结果
curl -s "https://t.me/s/tgsearchers7?q=三体" -o tests/fixtures/tg_search.html
```

`tests/fixtures/tg_search.html` 入库后，解析器单测完全不依赖网络。这一步要在写 `tg/parse.py` **之前**做，否则只能靠猜 selector。

**这也是 D7（结构变化检测）必须实装的理由**：本机测不了的代码，线上出问题唯一的信号就是那行 `ParseStatusStructureChanged` 告警。

---

## 8. H 明确不搬的（3 项，理由要硬）

| 东西 | 为什么不搬 |
|---|---|
| GOB / sonic 序列化 | 我们用 JSON + SQLite，无跨语言需求；sonic 是 Go 的高性能 JSON 库，Python 侧没有对应物也不需要 |
| 分片缓存 `sharded_*` | Go 为了躲大 map 的锁竞争才分片；Python 有 GIL，单 dict 足够，分片只增加复杂度 |
| `finalUpdateTracker` | 那是它多层缓存回调的补丁（防同一 key 被并发重复更新）；我们只有一层写库，用 SQLite 事务天然解决 |

另外两个“不搬”要说明白：

- **自适应并发**（`adaptive_concurrency.go`）：我们源少（先 3~5 个），固定 `Semaphore(20)` 足够，自适应反而引入不可预测。
- **`SkipServiceFilter`**：那是二级关键词过滤，我们不做。

---

## 9. 目录结构

```
backend/app/search/
├── registry.py        自注册 + 白名单加载（默认全关）
├── base.py            BasePlugin：4秒窗口 / shield / 双客户端 / 工作池降级 / 回退
├── cache.py           L1 dict + L2 SQLite；complete 语义；合并写不覆盖
├── outcome.py         BatchOutcome：全失败/零产出不写缓存 + 补齐比例门
├── observe.py         failureClass + liveness 分级（20 轮滑动窗口）
├── pool.py            非阻塞工作池 + 全局计数
├── types.py           Link / SearchResult / PluginOutcome / ParseStatus / FailureClass
├── clean.py           链接解析 / 六类归并 / 提取码        （P4）
├── merge.py           去重三级降级 + 完整度 + 三段打分     （P4）
├── adapter.py         SearchResult → 前端 items（唯一转换点） （P8）
├── engine.py          多源并发 → 合并 → 排序 → 本地分页    （P8）
├── tg/
│   ├── gate.py        可达性门（后台探测 / fail-open / 连续2次才关门）  （P5）
│   ├── channel.py     单频道抓取 + 结构检测                        （P5）
│   ├── parse.py       t.me 页面解析（lxml.cssselect）              （P5）
│   └── service.py     searchTG 编排：门 → 并发 → 缓存分流 → 补齐    （P5）
└── plugins/
    ├── _template.py   新源模板（下划线开头，不会被自动加载）
    └── haisou.py      第一个真实源（取数走本项目官方 API，一次请求出链接）
```

---

## 10. 实施进度与逐行对照

P1/P2 已落地。**每一块都能指回 pansou 的具体函数和行号**，不是重新设计：

| 我们的文件 | 对应的 pansou 源码 | 规模 | 关系 |
|---|---|---|---|
| `search/base.py` `search()` | `plugin/plugin.go:642-925` `AsyncSearch` | Go 284 行 → Py 约 90 行 | **逐行翻译**，逻辑一行未改 |
| `search/base.py` `_refresh()` | `plugin/plugin.go:1172-1237` `refreshCacheInBackground` | Go 66 行 → Py 约 30 行 | 逐行翻译，含两条提前退出 |
| `search/base.py` `_partial()` | `AsyncSearch` 内 `partial()` 闭包 | Go 36 行 → Py 约 20 行 | 逐行翻译 |
| `search/pool.py` | `plugin/plugin.go:401-445` `acquireWorkerSlot`/`releaseWorkerSlot` | Go 45 行 | 语义等价（见下） |
| `search/cache.py` `merge_put()` | `service/search_service.go:304-325` `writeFinalMainCache` | Go 22 行 | 逐行翻译 |
| `search/cache.py` `merge_results()` | `service/search_service.go` `mergeSearchResults` | — | 逐行翻译 |
| `search/outcome.py` | `service/batch_search.go:77-320` `batchSearchOutcome` | Go 240 行 → Py 约 110 行 | 逐行翻译，含三条判定 |
| `search/observe.py` `classify_failure()` | `service/batch_search.go:31-62` `failureClass` | Go 32 行 | 逐行翻译 |
| `search/observe.py` `LivenessRegistry` | `service/liveness.go` 全文 | Go 286 行 → Py 约 110 行 | 逐行翻译，含 20 轮滑动窗口 |
| `search/types.py` | `model/response.go` + `model/plugin_result.go` + `plugin.go:164` | — | 字段一一对应 |
| `search/clean.py` 正则常量 | `util/regex_util.go:10-65` | 20 条 | 逐条抄，顺序未动 |
| `search/clean.py` `get_link_type`/`clean_*_url`/`normalize_*_url` | `util/regex_util.go:68-487` + `util/parser_util.go:80-139` | Go 420 行 → Py 约 300 行 | 逐行翻译，含每段重复的结束标记列表 |
| `search/clean.py` `extract_password`/`is_valid_password`/`extract_net_disk_links` | `util/regex_util.go:490-941` | Go 450 行 → Py 约 180 行 | 逐行翻译，十段提取顺序未动 |
| `search/clean.py` `anchor_candidates`/`extract_code_near`/`password_for` | `util/parser_util.go:286-389` | Go 104 行 → Py 约 60 行 | 逐行翻译，60 字节窗口与两条回退规则都在 |
| `search/clean.py` `LinkCandidates` | `util/parser_util.go:433-539` | Go 107 行 → Py 约 60 行 | 逐行翻译，六类归并顺序未动 |
| `search/merge.py` `merge_search_results` 等 | `service/search_service.go:106-199`、`:657-701`、`:1875-1984` | Go 约 200 行 → Py 约 110 行 | 逐行翻译，三段打分的数字一个没改 |
| `search/tg/parse.py` | `util/parser_util.go:146-928` | Go 780 行 → Py 约 480 行 | 逐行翻译，选择器与判定顺序未动 |
| `search/tg/gate.py` | `service/tg_reachability.go` 全文 | Go 241 行 → Py 约 210 行 | 逐行翻译，含 `requiredFailures=2` |
| `search/tg/channel.py` | `service/search_service.go:709-765` + `util/http_util.go:306` | Go 62 行 | 逐行翻译，三条判定都在 |
| `search/tg/service.py` | `service/search_service.go:1363-1454` + `:1458-1510` | Go 145 行 | 逐行翻译，判定全部委托给已有模块 |
| `search/plugins/haisou.py` | `plugin/haisou/haisou.go` | 骨架沿用，取数换成本项目官方 API（见 P7） |

### 三处必须替换的地方（语言差异，不是设计选择）

| Go | Python | 为什么不能照搬 |
|---|---|---|
| `go func(){...}()` + `select` + `chan` | `create_task` + `wait_for(shield(...))` | Python 没有 goroutine 和 channel。`shield` 是唯一能保证"超时不取消后台任务"的手段，这正是 Go 里 `doneChan` 的作用 |
| `acquireWorkerSlot()` 用 `select`+`default` | 同步方法改计数器 | Python 没有 `try_acquire`；`wait_for(sem.acquire(), 0)` 在 timeout=0 时会**立即超时**（即使有槽），照直觉写会变成"永远拿不到槽"。单线程事件循环里，不含 `await` 的同步方法天然原子 |
| `chan` 区分 resultChan / errorChan | `PluginOutcome.failure` | 失败信息放进返回值，避免 task 异常无人接收（超时后返回值本来就没人接） |

### 清洗层的三处语言差异（同样不是设计选择）

| Go | Python | 为什么不能照搬 |
|---|---|---|
| `len(s)` / `s[i:j]` 按**字节** | 按**字符** | 凡是用长度当阈值的地方（60 字节取码窗口、路径末段 >5、`len(title)/10`）都走 `_blen()`。不这么做，同一个中文标题在两边的排序权重就不一样 |
| `\w` `\d` `\s` 只认 ASCII | 默认认 Unicode | 全部加 `re.ASCII`。否则 `[\w.-]+\.` 会去匹配中文片段，域名判定跟着漂 |
| `url.QueryUnescape` | `unquote_plus` | Go 版把 `+` 还原成空格；Python 的 `unquote` 不还原。用错会让带 `+` 的链接去重失败 |

**一处没有复制**：`ExtractPassword` 里 `strings.IndexAny(part, ":：")` 返回**字节下标**，
再 `part[codeStart+1:]` 只跳 1 个字节。中文冒号是 3 字节，这样切出来是**非法 UTF-8**，
`isValidPassword` 必然为假 —— 该分支在 pansou 里其实是失效的，最终靠后面的
`PasswordPattern` 兜住。Python 按字符切片直接拿到干净结果，即"照文档意图实现"，
结果一致（`test_fullwidth_colon` 覆盖）。若要求逐字节复现这个瑕疵，反而会写出更错的代码。

### 已经验证的（24 个用例全过）

```
cd backend && .venv/Scripts/python.exe -m unittest tests.test_search_core -v
Ran 24 tests in 1.16s — OK
```

覆盖的关键行为：

- 快源 0.01s 返回 `is_final=True`；慢源（fetch 1s / 窗口 0.05s）超时返回空且 `is_final=False`
- **超时后底层任务没被取消**：0.7s 后缓存里出现 `complete=True` 的两条结果（`shield` 失效时这里是 `None`）
- 一快一慢并发，快的总耗时 < 0.5s（慢源 2s 窗口 0.2s）
- 超时瞬间留下 `complete=False` 的空记录（读的时机在超时刚返回时，不是"睡一觉再读"）
- `merge_put` 后写不吞先写（x1/x2/x3 都在）
- 同 key 撞上取完整度高的
- 503 失败回退旧缓存，源不会凭空消失
- 404/403 不可重试，超时/5xx 可重试
- `zero_yield` 与 `failing` 是两个状态；恢复 20 轮后从告警名单消失
- 白名单为空 = 零个源；重名注册报错

### P4 清洗层已落地（26 个用例，全仓 103 个全过）

```
cd backend && .venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
Ran 103 tests — OK
```

清洗层的关键验证不是"能跑"，而是**这些机制真的在起作用**：

- 一条消息三个链接各带各的码 → 三个码各归各位（`baidu=x7k2 / quark=q9m3 / xunlei=w5n8`）
- 同一个正文走**旧的整段扫描** → 返回 `"http"`（吃到 `链接：` 的冒号再截 4 位）。
  这条反例证明锚定窗口不是锦上添花，是修了个真实错码问题
- 无提取码的链接必须保留（`password=""` 但链接在）
- 百度 `?pwd=` 先剥后拼：`add()` 里剥、`finalize()` 里拼回
- `123pan.com/s/abc123` 无协议头 → 补 `https://`
- UC 分享码里含 `123` 不被当分隔符切断
- 天翼访问码写在 URL 括号里（`（访问码：k3m9）`）也能取出
- 排序：「合集」+ 10 天前 压过「无关键词」+ 今天（490 分 vs 500 分时间差）
- 等级分：level1 = +1000，未注册源 = 0，TG = 0（等同等级 3）

### 测试抓到的第二个真 bug（预期错，不是代码错）

我最初写测试时断言 `extract_net_disk_links` 会保留百度的 `?pwd=x7k2`，跑出来没有。
回查源码：`CleanBaiduPanURL` 把 `"?pwd="` 放进结束标记列表**先截断**，于是紧接着那句
`if strings.Contains(url, "?pwd=")` 永远进不去 —— pansou 里这段是死代码。

结论：**百度链接的密码在提取阶段必然被剥掉，靠 `ExtractPassword` 单独取回**。
这是原样行为，不是移植错误，所以没有"修"，只在测试里注明。


### 测试抓到的真 bug

`BatchOutcome.finalize()` 第一版用 `missing + errors + empty` 三个集合判断"哪些源没上报"，
**结果把每一轮正常返回的源全算成了超时**（成功的源不在这三个集合里）。
后果是 `should_backfill` 的比例门永远触发不了——超时数恒等于总数。

改成记录 `_observed` 集合后才正确。这条如果不是测试断言 `should_backfill(True) is True`，
上线后会表现为"永远不补齐"，而且日志里看不出异常。

### P5 TG 通道已落地（40 个用例，全仓 143 个全过）

```
cd backend && .venv/Scripts/python.exe -m unittest tests.test_tg
Ran 40 tests in 2.5s — OK
```

**解析层用真实页面验证**，不是自己编的样本。三个 HTML 在 `backend/tests/fixtures/`：

| 样本 | 抓取来源 | 断言 |
|---|---|---|
| `tg_search.html` | `t.me/s/tgsearchers7?q=三体` | 20 条，`status=ok` |
| `tg_channel.html` | `t.me/s/tgsearchers7` | 20 个消息块 → **19 条**（1 条纯图片被正确丢弃，且不算结构变化） |
| `tg_empty.html` | 不存在的关键词 | 0 条 + **`status=no_messages`** |

第三条最关键：**0 条结果不等于解析失效**。没有这个判定，t.me 改版后系统会一直返回空，
而所有健康检查都是绿的。

真实页面上已验证的字段：`unique_id=tgsearchers7_102423`、标题取到
`遮天（2025）4K 10bit 立体环绕声 更至 EP183`、标签 `['遮天','动画','奇幻','冒险']`、
图片 1 张且**不含头像的 `data:image/svg`**、百度链接 `password='7QA8'` 且 URL 保留 `?pwd=7QA8`。

合成样本补真实页面缺的分支：**内联按钮里的链接**（该频道不发按钮）、按钮文字里的提取码、
日期头跳过、元数据行跳过、单行格式检测。

门控的验证点，每条都对应一个具体的失效模式：

| 用例 | 防的是什么 |
|---|---|
| 404 → 可达 | 用状态码判可达会误判：404 恰恰证明网络通 |
| 单次失败 → **仍可达**，`streak=1`，原因带"暂不改判" | 单次握手超时把可用的 TG 静默跳过（pansou 注释里点名的"最危险的失效模式"） |
| 两次失败 → 关门，原因含"连续 2 次" | 只有连击达阈值才翻转 |
| 恢复 → `streak` 归零、门重开 | 不可达结论保鲜 60s，恢复后要能自己回来 |

编排层验证点：

- 门关时**一个频道请求都不发**（`calls == []`），且缓存不写
- 慢频道（0.5s）不拖累快频道：窗口 0.1s，总耗时 < 0.4s
- 全部失败 → 不写缓存（没有可信内容）
- 部分失败 → 写 `complete=False`（下次该重抓）
- 补齐走 `merge_put`：首轮 3 条 + 期间外部并入 1 条 + 补齐 1 条，四条全在
- 超时超过 1/3 → **不补齐**（`["fast","slow"]` 只打两次请求，不重复打慢的）

### 测试抓到的第三个真 bug

`_backfill` 里的协程工厂被写成 `async def factory(...)`，而 `_run_batch` 要的是
"可调用的工厂"，传进去的是一个协程对象。`guarded()` 里 `await factory()` 抛
`TypeError: 'coroutine' object is not callable`，被上层当成频道失败吞掉。

后果：**后台补齐永久静默失效**，日志里只有一条 debug，`/api/health` 一切正常。
这条是测试里断言 `"slow_1" in ids` 才暴露的——只看"补齐有没有报错"永远发现不了。

### P7 第一个真实源插件已落地（38 个用例，全仓 181 个全过）

`search/plugins/haisou.py`。骨架是 pansou 的插件机制，**取数换成了本项目一直在用的
官方 API（idatariver MAPI，apikey + `x-idr-sign`）**。pansou 原版那套网页两阶段实现
没有保留：同一个源挂两条通道就是两份缓存、两份健康统计，日志里分不清是谁在报错。

换掉的原因很直接。pansou 原版是匿名爬 `haisou.cc`：

```
/api/pan/share/search         5 类型 × N 页，只返回 hsid
/api/pan/share/{hsid}/fetch   每个候选一次，才返回分享码 + 提取码
```

一次搜索 ≤35 个请求，匿名额度 100 点/天，等于**一天只够 2~3 次**，只能当补充源。
官方 API 是另一个量级：

| | pansou 网页版 | 本项目官方 API |
|---|---|---|
| 请求数 / 次搜索 | ≤ 35 | **1** |
| 链接 | 二次取链，失败率高 | 一次给全 |
| 额度 | 匿名 100 点/天 | apikey 配额 |
| 分页 | 无（每类型各自页码） | `page` / `pageSize` / `total` |
| 清洗 | HTML 标签 + 关键词兜底 | 结构化，无需清洗 |

`ext` 支持 `page` / `pages`（≤5）/ `page_size` / `platforms` / `scope`，
默认一次请求 50 条，串行翻页。

#### 保留了什么、丢了什么

保留（全在机制层，一行没改）：自注册、响应窗口、双超时客户端、`complete` 语义缓存
+ 合并写、失败分类、`zero_yield` 判定、等级分。等级从 3 升到 **1**：这是唯一有
apikey 和分页的主通道，排序分 +1000，压过匿名源里的同名资源。

丢掉：`clean.py` 的 HTML 清洗、hsid 去重、取链截断。这三样都是为脏文本和两阶段
服务的，对结构化返回套上去只会把合法链接改坏。

#### 线上验证到哪一步

本机 `python scripts/probe_haisou.py --compare`：

```
=== 插件（官方 API）    0.7s  HTTP 403（haisou） 上游判定 apikey 无效或余额不足（1005）
=== 旧链路（providers） 0.8s  UpstreamError: 上游判定 apikey 无效或余额不足（1005）
```

HTTP 200 + code 1005：链路、签名、请求体格式都通，是本地这把 apikey 没额度
（同一时刻旧链路也是 1005，与改造无关）。换一份有效 apikey 即可看到真实条数，
不需要改代码。

#### 三处"照抄会错"的地方

1. **`response_timeout` 必须是类属性**。我第一版写在 `fetch()` 里改实例属性，两个问题：
   `search()` 里的 `wait_for(timeout=self.response_timeout)` 在 `fetch()` 开始执行前
   就求值了，改了无效；而且插件是共享单例，写实例字段在并发下会串台。
   4 秒窗口对一次真实请求偏紧，改成类属性 `6.0`。
2. **重试耗尽不能包成 `RuntimeError`**。Go 用 `%w` 包装，结果是 500 和限流都变成
   `OTHER`，`/api/health` 里分不出是额度用完还是服务器崩了。改为抛原始异常，
   分类能力才保得住。
3. **200 + 有条目但一条 `shareUrl` 都没有 = 结构变化**。这时返回 `[]` 会被 liveness
   记成 `zero_yield`，把"上游字段改名"错判成"这个源没内容"，健康检查全绿。
   抛 `ValueError`，因为它是失败，不是没内容。

#### 两处 pansou 没有、但不加会白烧钱的处理

- **超时必须显式传 `settings.request_timeout`（75 秒）**。共享客户端默认 30 秒，
  而官方规定请求超时 >60 秒，否则**提前中断照样计费**。这是插件里唯一不能省的一个参数。
- **1004 / 1005 不重试，转 403（FORBIDDEN）**。签名过期和 apikey 无效重试也不会好，
  只会多花两次钱；转 403 是为了让 `/api/health` 一眼看出是凭据问题，而不是混进 OTHER。

#### 测试覆盖的失效形态

不是"能不能跑"，是"跑通了但结果是错的"那些：

- 404 只请求 1 次不重试；429 重试 1 次后成功；500 重试耗尽仍保留 status 500
- `result` 不是对象 / `items` 不是数组 / `total` > 0 但没有 items → `ValueError`
- 有 2 条但全无 `shareUrl` → `ValueError`（字段改名，不能当零结果）
- `code=1004/1005` → `HttpStatusError(403)` 且**只请求 1 次**；`code=1001` → `UpstreamError(code)`
- 第 2 页失败不影响第 1、3 页；全部页失败抛原始异常
- 默认参数下请求数恰好 **1**（对比 pansou 原版的 35，这是改造的全部意义）
- 二次搜索命中缓存，**不再计费**
- `extras` 经缓存往返不丢；`extras` 上线前写入的老缓存也不会崩

### P8 接入 `/api/search`（旧同步通道已删除）

取数从 `await asyncio.to_thread(providers.search, ...)` 换成 `await engine.search(...)`。
`providers.search()` 和那套 `requests.Session` 整体删除，`providers.py` 只剩协议层
三件套（拼请求体 / 鉴权头 / 返回码归一化）——**不发请求**。

**为什么要 adapter**：pansou 的 `SearchResult` 是给网页展示的（title / content / links），
前端要的是 `hsid / shareUrl / shareCode / fileCount / sizeBytes`。`search/adapter.py`
是两者之间唯一的转换点，只在出口做、不写回缓存（缓存里存 `SearchResult`，
出口时才映射，改字段不用洗缓存）。

**分页在本地**：pansou 的 `fetch()` 不返回 `total`（它整个搜索就是"多源抓一批 +
本地切片"）。engine 一次抓 `SEARCH_PAGES`(2) 页 → 合并 → 按 page/page_size 切。
翻页不重复打上游，代价是最多能翻 `2×50/page_size` 页（20 条/页时是 5 页）。

**失败语义**：单源失败不影响其他源，失败原因进 `_meta.sources` 和日志；
**所有源都失败且零结果**才抛 `SearchEngineError` → 504。全挂返回空列表会让用户
以为"网上没这个资源"，而真实原因是源全挂了。

**新增配置**：`ENABLED_SOURCES`（默认 `haisou`）。空 = 全关，这是 pansou 的约定；
但默认值不能是空，否则开箱即 504。

**`MIN_INTERVAL=2s` 的全局串行排队已删除**。它是旧链路时代的产物：当年一次请求打全平台，
上游对紧接的第二个请求会挂到 120 秒超时，只能靠串行+间隔保命。现在并发控制在插件内部
（haisou 的 `MAX_CONCURRENCY=5`），限流有 `1003 → 429` 退避重试兜着，重复查询有 5 分钟
响应缓存挡着。再留一道全局等待只会让每次搜索白等 2 秒，并把多源并发重新串起来——
那正是插件层要解决的问题。实测删除后单次搜索 2.0 秒（原本 4.0 秒）。


### P9 TG 通道接进插件体系（`plugins/tg_searchers.py`）

pansou 里 TG 不是插件，是独立阶段（`search_service.go:1363 searchTG`）。本项目所有源统一
走插件层，所以加了一层**薄包装**：可达性门、单频道抓取、多频道并发、缓存与后台补齐
全部还是 `tg/` 那四件，插件只做转交。

**必须偏离 pansou 的一处**：pansou 的门是判在 `searchTG` 外面的，门关 = 整条通道跳过。
插件化之后"跳过"只能表达成返回空列表，而空列表会被 `Liveness` 记成 `zero_yield`——
把"根本没连上"误判成"这个源改版了，什么都抓不到"。所以门关时抛 `TGUnreachable`
（继承 `httpx.ConnectError`），`classify_failure` 归到 NETWORK：不写缓存这一点保住了，
`/api/health` 也能一眼看出是网络不通而不是源坏了。实测日志：
`[tg_searchers] 抓取失败且无缓存（network）: t.me 不可达…`。

**缓存键对齐**（容易踩且不报错）：插件覆盖 `cache_key()` 直接返回
`tg_cache_key(keyword, channels)`，让外层插件缓存与 `search_tg` 内部缓存用**同一个键**。
不覆盖的话，后台补齐写进 `tg:` 键、外层读 `tg_searchers:` 键，补齐永远读不到——
表现只是"补齐没生效"，没有报错。有一条用例专门钉这个。

**启动接线**：`lifespan` 里只在 TG 源被启用时 `TG.start()`，启动后台探测循环，
搜索路径只读结论不阻塞（pansou 的 `tgProbeLoop`）。默认不启用：`ENABLED_SOURCES=haisou`，
线上能直连 `t.me` 时再加 `tg_searchers`。

### P10 `/api/health` 暴露源健康（观测层落地出口）

移植了 `failureClass` / `liveness` 五级分级，但一直没出口——线上分不清"是额度用完、
被限流、还是源真挂了"。现在 `/api/health` 里新增：

| 字段 | 内容 | 为什么需要 |
|---|---|---|
| `sources.enabled` | **实际加载成功**的源名 | `.env` 写错名字或导入失败时这里是空的，比翻日志快 |
| `sources.configured` | `.env` 里的原值 | 和上面不一致就说明有源没加载起来 |
| `sources.liveness` | 只列"有病"的源（degraded / failing / zero_yield） | `zero_yield` 最隐蔽：可达、从不报错、但一条都抓不到 |
| `sources.lastRound` | 最近一轮每源的 count / pending / failure / message | `liveness` 要攒 5 轮才下结论，刚重启时什么都看不到 |
| `sources.pool` | 缓存命中、后台完成、超时先返 | 判断"慢"是缓存没配上还是上游真慢 |
| `tgGate` | `reachable` / `reason` / `fail_streak` | 空结果到底是因为网络还是因为没内容 |

`lastRound` 特意在**抛错之前**写入：全挂是最需要排查的场景，`raise` 之后就没机会记了。

实测（双源、本机 t.me 不通）：

```json
"lastRound": {
  "haisou":       {"count": 100, "pending": false, "failure": null},
  "tg_searchers": {"count": 0,   "pending": false, "failure": "network",
                   "message": "t.me 不可达：连续 2 次探测失败…"}
}
```

**出口还补了一道平台过滤**：haisou 是"只查选中的平台"，本来不会串；但 TG 频道里的分享
横跨所有网盘，请求里的 `platforms` 对它无效。不过滤的话用户点"百度网盘"会看到一堆夸克链接。
过滤在 `adapter.to_items()` 之后、分页之前——先筛再切页，否则第 1 页可能被别的平台占满。

---

### P11 通道分层：TG 是基础，haisou 只做兜底

**这一节和 pansou 是反着来的，必须说明。**

pansou 的 `sourceType=all` 是 **TG ∥ 插件两条分支并行**（`search_service.go:530` 两个 goroutine
同时起跑），谁先回谁的结果都进合并池。本项目不这么干：

```
TG（免费、无额度）→ 拿到可用结果 → 结束，一次接口都不调
                 ↘ 不可达 / 零结果 / 没有用户要的网盘 → haisou（计费）兜底
```

理由是额度：haisou 按请求计费，而 pansou 的 113 个插件绝大多数免费。**并行等于每次搜索
都无谓地烧掉一份额度**，哪怕 TG 已经给出了同样的结果。

#### 实现

| 改动 | 内容 |
|---|---|
| `BasePlugin.source_kind` | `"plugin"`（默认）/ `"tg"`。**显式声明，不靠插件名猜**——名字是用户可改的配置项，类型不是 |
| `engine._split_tiers()` | 按 kind 拆成 (TG 层, 兜底层) |
| `engine._covers_platforms()` | 判定"这层结果有没有用户要的网盘"。TG 有 50 条夸克、用户要百度，仍然算没拿到 |
| `engine.search(source=...)` | `auto`/`all` = 分层降级；`tg` = 只跑 TG（零成本）；`plugin` = 只跑兜底源 |
| `/api/search?src=` | 默认 `auto`。非法值回 **400**，不静默当 auto |
| 响应 `tier` 字段 | `tg` = 这轮没花钱，`plugin` = 兜底花了 |

#### 三个容易踩的点

1. **响应缓存键必须带通道**。不带的话 `src=tg` 的空结果会顶掉 `auto` 的真实结果，
   反过来 `auto` 的兜底结果会被当成"TG 搜到的"返回。
2. **`_covers_platforms` 不能只看条数**。TG 频道横跨所有网盘，用户选"百度"时它可能
   只返回夸克——这时候"拿到了结果"但对用户没用，必须继续兜底，否则用户拿到空列表。
   这也是为什么 `adapter.platform_of()` 被单独抽出来：判定发生在转换之前。
3. **两层都要留痕**。TG 失败但 haisou 成功时，`lastRound` 里必须同时有 TG 的 `failure`
   和两层的 `tier`，否则查不出"主力通道已经挂了，一直在花钱兜底"。

#### 实测（双源、本机 t.me 不通）

| 请求 | 结果 | 上游调用 |
|---|---|---|
| `src=auto` | 200，20 条，`tier="plugin"`，8.8s（haisou 冷启动） | 只打了 haisou：2 次 |
| `src=tg` | 200，0 条，`tier=""` | **0 次**，TG 门关秒跳过 |
| `src=plugin` | 200，20 条，`tier="plugin"` | 2 次 |
| `src=bogus` | 400 未知的来源类型 | 0 次 |
| `src=tg`（未启用 TG 源） | **400** 通道没有启用任何源 | 0 次 |

`src=tg` 在门关时返回 504「所有搜索源都失败（t.me 不可达）」而不是 200 空结果：
这是"你要的通道挂了"，给明确原因比给空白有用。400 与 504 是分开的两个出口——
配置问题不写失败缓存（改完配置立刻能重试），上游故障才写 20 秒。

---

## 11. 实施顺序（重排：观测层提前）

上一版把观测排到最后，是错的。**没有 `failureClass` 和 `liveness`，前面所有“每源独立报错”的机制都看不见效果**，调试时会退化成“全是 0 条，不知道为什么”。

| 阶段 | 内容 | 验收 | 状态 |
|---|---|---|---|
| P1 | `registry.py` + `types.py` + `observe.py` + `outcome.py` | 注册两个假源，能按白名单启停；失败能分出 429/超时/结构变化 | ✅ 完成 |
| P2 | `base.py`（4秒窗口 / shield / 双客户端 / 工作池降级） | 一快（0.2s）一慢（6s）：慢的不拖累快的；超时后后台任务仍在跑完并写缓存 | ✅ 完成（24 用例） |
| P3 | `cache.py`（L1+L2 / complete / 零产出不写 / 合并写） | 重启后同词秒回；零产出不落库；并发补齐不吞结果 | 🔶 L1+SQLite 与合并写已完成，重启持久化待接 |
| P4 | `clean.py` + `merge.py` | 多平台链接+提取码文本 → 正确 Link 列表；「合集」排前 | ✅ 完成（26 用例） |
| P5 | `tg/parse.py` + `tg/gate.py` | 离线样本解析出正确条数；断网时门关、不写缓存、不计失败 | ✅ 完成（40 用例） |
| P6 | `tg/channel.py` 接真实频道（线上验证） | 线上 8 秒内返回，补齐走后台 | 🔶 代码完成，线上待验 |
| P7 | 第一个真实源 `haisou`（取数走本项目官方 API） | 端到端跑通；一次搜索 1 个请求 | ✅ 插件 + 38 用例完成；**本地 apikey 报 1005，待换有效凭据验真实条数** |
| P8 | `/api/search` 走新链路，旧同步通道删除 | 全源挂掉时仍能出结果 | ✅ `engine` + `adapter` 接进端点，`providers.search` 已删；`MIN_INTERVAL` 全局串行同时删除 |
| P9 | TG 通道插件化（`tg_searchers`） | 与 haisou 并存，TG 挂掉不影响主源；门关时秒跳过 | ✅ 插件 + 13 用例；本机 t.me 不通，门控与失败分类已实测 |
| P10 | `/api/health` 暴露源状态 + 出口平台过滤 | 一眼看出哪个源失败、什么分类 | ✅ `lastRound` / `liveness` / `tgGate` / `pool`；平台过滤 3 个用例 |
| P11 | 通道分层：TG 优先，haisou 只兜底 | TG 命中时一个计费请求都不发 | ✅ `source_kind` + `engine` 分层 + `src` 参数；11 个用例 |
| — | 线上验证 | 真实频道可达性、真实配额 | 🔶 P6/P9 需在部署机复验（本机两块网络路径都不通 t.me） |

---

## 12. 一句话总结

PanSou 真正值钱的不是 113 个源，是这 34 个机制。其中**最容易被漏掉、也最容易出事的三条**：

1. **整批零产出不写缓存**（C3）—— 源改版后返回空列表，会把空结果钉一小时
2. **补齐写缓存必须合并不能覆盖**（C5）—— 实测覆盖写丢掉 74% 并发结果
3. **`zero_yield` 是独立健康状态**（F2）—— 不报错不等于健康

TG 通道（G）不是可选项：它是唯一的“一手来源”，线上直连即可用，代价只是 `lxml` + `cssselect` 两个纯解析依赖。但**本机不可达、线上可达**这个差异必须在代码里正面处理——`trust_env=False` 不写，你会在本地得到一个假的「TG 不通」结论。
