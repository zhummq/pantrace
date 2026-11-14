# 盘迹 PanTrace

网盘资源聚合搜索。前端 Vue 3 + Vite，后端 FastAPI，数据来自套壳 API。

## 结构

```
backend/
  app/main.py         FastAPI 入口：/api/search /api/platforms /api/health
  app/config.py       配置（从 .env 读）
  app/analytics.py    访问埋点：事件表 + 看板聚合
  app/feedback.py     反馈/求资源：工单表、限流、去重、聚合
  app/group.py        群入口配置：二维码/群号/弹窗策略（app_config 表）
  app/own.py          精选链接：自有资源库、搜索匹配、未命中词日志
  app/resolver.py     点击时转存：链接映射、单飞、签名跳转（不认识具体网盘）
  app/pan.py          网盘账号表 + 多账号轮换 + 失败转移 + 空间回收
  app/sdk/            网盘操作 SDK：夸克/UC/百度/迅雷/光鸭/移动139（见「网盘 SDK」）
  .env                apikey 等敏感配置，已被 gitignore
  requirements.txt
frontend/
  src/App.vue         搜索页、平台筛选、分页、反馈入口
  src/components/ResultCard.vue
  src/components/FeedbackDialog.vue   反馈弹层（纠错 / 求资源 / 报错）
  src/components/QqGroupDialog.vue    群入口弹层（进站 / 获取链接拦截）
  src/admin/          看板（Vue 应用，独立入口）
    App.vue           壳：口令门、区间切换、60s 自动刷新
    api.js            统一请求层（口令、401、错误处理）
    components/       Panel/StatGrid/BarList/Pager + 六个业务面板（含「网盘账号」）
  admin.html          看板的 Vite 入口（与 index.html 一起构建）
  src/api.js          接口封装
  vite.config.js      dev 时 /api、/uploads 代理到 8000；build 双入口
start.bat             生产模式：构建前端 + 单端口 8000
start-dev.bat         开发模式：后端 8000 + 前端 5173
```

看板架构说明：

- `/admin` 页面是 Vue 构建产物（`dist/admin.html`），与主站共用一次 `npm run build`；构建产物入库，服务器 `git pull` 即生效，**不需要在服务器上装 Node**
- 后端优先返回 `dist/admin.html`；构建产物缺失时回退到单文件旧版 `backend/app/admin.legacy.html`（仅作回滚保留，不再维护）
- `/admin` 响应带 `Cache-Control: no-store`，改版后不会看到旧页面；assets 文件名带 hash，长期缓存不受影响

## 启动

生产（单端口，浏览器开 http://127.0.0.1:8000）：

```bat
start.bat
```

开发（前后端分离，热更新，浏览器开 http://127.0.0.1:5173）：

```bat
start-dev.bat
```

手动方式：

```bash
# 后端
cd backend
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm run dev
```

首次跑之前确认 `backend/.env` 里 `API_KEY`、`IDR_SIGN`、`IDR_TIME` 三项都已填。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/search?q=关键词&platforms=quark,baidu&page=1&page_size=20` | 搜索 |
| GET | `/api/platforms` | 平台列表（前端筛选用） |
| GET | `/api/health` | 健康检查 |
| GET | `/go/{token}` | 点击跳转过渡页（不缓存、noindex）；解析完自动跳转 |
| POST | `/api/resolve` | token → 可用地址（我们的分享链接，失败则回退原链接），body `{"t":"..."}` |
| POST | `/api/track` | 前端行为上报（内部用，见下节） |
| POST | `/api/feedback` | 提交反馈 / 求资源 |
| GET | `/api/group` | 群入口配置（进站/获取链接弹窗用，公开） |
| GET | `/api/admin/group` | 读群入口配置，需 `X-Admin-Token` |
| POST | `/api/admin/group` | 保存群入口配置（整包提交），需 `X-Admin-Token` |
| POST | `/api/admin/group/upload` | 上传二维码图片（裸字节流，≤2MB），需 `X-Admin-Token` |
| GET | `/api/admin/feedback?days=30&status=new` | 反馈列表，需 `X-Admin-Token` |
| POST | `/api/admin/feedback/{id}` | 标记反馈状态，body `{"status":"done"}` |
| GET | `/api/admin/stats?days=7` | 看板数据，需 `X-Admin-Token` |
| GET | `/api/admin/own?days=30` | 精选链接列表 + 未命中词榜 + 小数字，需 `X-Admin-Token` |
| GET | `/api/admin/plan?days=30&page=1&pageSize=20` | 上架清单（按搜索人数排序，已上架自动剔除；返回 `total`/`totalPages`），需 `X-Admin-Token` |
| POST | `/api/admin/own/parse` | 粘贴的分享文案 → 解析成可入库字段（不写库；夸克链接会调公开接口补元信息），需 `X-Admin-Token` |
| POST | `/api/admin/own/bulk` | 批量保存解析结果（逐条校验，坏条不连坐，上限 50 条），需 `X-Admin-Token` |
| POST | `/api/admin/own` | 新增/更新精选链接（id 为空即新增），需 `X-Admin-Token` |
| DELETE | `/api/admin/own/{id}` | 删除精选链接，需 `X-Admin-Token` |
| GET | `/api/admin/mirror` | 转存映射：成功/失败/命中数/占用体积 + 最近 30 条，需 `X-Admin-Token` |
| POST | `/api/admin/mirror/test` | 手工验一条链接的转存结果（验 cookie 用），需 `X-Admin-Token` |
| GET | `/admin` | 用户行为看板（Vue 构建产物，`no-store` 不缓存） |
| GET | `/docs` | FastAPI 自带接口文档 |

前端只拿到清洗后的字段（`platform` / `title` / `shareUrl` / `sharePwd` / `fileCount` / `sizeBytes`），**拿不到 apikey，也拿不到上游的 `highlightedTitle`**（那是远端 HTML，直接渲染就是 XSS 口子；高亮由前端自己按关键词标记，且先转义）。开了点击转存后，`shareUrl` 是 `/go/<签名token>` 而不是真实链接，`sharePwd` 恒为空。

## 上游实测踩到的坑（改代码前先看）

这几个都不在文档里，是实际调出来的：

1. **不带浏览器 UA → 403**。请求必须带 `User-Agent`。
2. **`platforms: []` 空数组 → 参数错误**。没有平台筛选时必须整个字段省略。
3. **apikey 的位置跟着协议走**：控制台 MAPI 是放在请求体的 `params.apikey` 里（外层还套一层 `{path, method, params}`），早期直连套壳时才是 query string。别凭印象改。
4. **httpx 走本机代理读超时，requests 正常**。所以后端用 requests + `asyncio.to_thread`，别改回 httpx。
5. **平台 key 与文档示例不一致**。实测到的是 `quark` / `baidu` / `ali` / `xunlei` / `guangya` / `yidong`，阿里云盘是 `ali` 不是 `aliyun`。平台中文名一律以上游返回的 `platformName` 为准。
6. **上游对连续请求极敏感**，同一 query 紧接第二次会挂到 120 秒超时。后端做了串行排队（最小间隔 2 秒）+ 5 分钟结果缓存，这是可用性底线，不要去掉。
7. **鉴权只认 `x-idr-sign`**。拆变量验证过：只带 `sid` 通，带 `sid`+`cf_clearance` 但去掉签名报 `1004`，完全不带 cookie 只有签名也通。所以后端不需要维护浏览器会话，也不需要存 `cf_clearance`。
8. **签名会过期**，过期返回 `code 1004`。这条必须报给用户看，不要当成网络错误含糊过去。

## 用户行为埋点

站点没有登录体系，所以做的是**匿名行为统计**：谁来过（随机访客号）、搜了什么、点了什么、从哪来、几点来。

打开 `http://你的域名/admin` 就是看板。没配 `ADMIN_TOKEN` 时只有本机 `127.0.0.1` 能访问，**公网部署必须配一个口令**：

```bash
python -c "import secrets;print(secrets.token_urlsafe(18))"   # 生成
# 写进 backend/.env 的 ADMIN_TOKEN=，重启服务
```

### 采了什么

| 事件 | 来源 | 记录内容 |
|---|---|---|
| `pv` / `leave` | 前端 | 进页面、停留时长、来源 referer、设备类型 |
| `search` | **后端** | 关键词、平台筛选、页码、结果数、耗时、是否缓存、失败原因 |
| `click` | 前端 | 点开了哪条资源（标题 + 平台），带 `sendBeacon` 保证跳转前发出 |
| `copy_pwd` | 前端 | 复制了提取码 |
| `mirror` | **后端** | 点击转存的落地结果：`ok`/耗时/`source=cached/fresh/fallback`/失败原因 |
| `filter` / `page` / `retry` | 前端 | 换平台、翻页、失败重试 |

搜索走服务端记录，不怕广告拦截插件；点击这类动作必须前端报，所以用 `navigator.sendBeacon`，页面跳走也能发出去。

**机器人过滤**：按 UA 识别已知爬虫/脚本（curl、python-requests、spider 等），事件落库时打 `bot=1` 标记。识别只打标不拦截——误伤真人比多记几行代价大。看板汇总一律剔除 bot 行，机器人搜索量单独显示一个数。

### 看板里能看到

分三层，避免一屏全是数字：

- **首屏（每天必看）**：搜索次数、**点击转化率**、**自有占比**、**零结果率**；下面一行小字是 PV/UV、点击次数、平均停留时长。
- **健康指标（出问题才看，折叠）**：失败率、上游平均耗时、缓存命中率、机器人搜索量、24 小时搜索分布。
- **决策参考（要拍板才看，折叠）**：关键词榜（带次数）、**搜不到的词**（这个词库缺什么，最有用）、被点开最多的资源、来源站、平台分布、新客与回头客；另有最近 30 条行为流水。

几个口径记一下：

- **UV** = 去重访客号。访客号是 `localStorage` 里的随机 UUID，清缓存就是新访客，所以 UV 偏大属于正常。
- **点击转化率** = 点击次数 / 搜索次数。不是人数口径。
- **零结果率** = 成功返回但 0 条的搜索 / 成功的搜索。这个数高说明要么关键词太偏，要么上游覆盖不够。看板上的"搜不到的词"榜直接列出这些词，补词库看它。
- 所有汇总数字均排除机器人流量；机器人搜索量单独显示。
- 会话 30 分钟无操作算断。

### 数据在哪

`backend/data/analytics.db`，SQLite 单文件，已在 `.gitignore` 里。默认留 180 天，每天自动清一次。

```bash
# 备份
cp backend/data/analytics.db ~/pantrace-analytics-$(date +%F).db
# 清空重来（服务停掉再删，否则 WAL 文件会留半条）
systemctl stop pantrace && rm -f backend/data/analytics.db* && systemctl start pantrace
```

### 隐私边界

- 不接任何第三方统计，数据不出服务器。
- 不写 cookie、不取指纹、不存精确位置；只有 IP（用于去重和排障）和 UA。
- 看板上 IP 一律脱敏成 `1.2.3.x` 再展示。
- 关键词只存搜索词本身，不存用户身份信息——本来也没有。

## 用户反馈与求资源

只有一个功能站，用户能反馈的其实很集中：链接失效、提取码不对、内容不符、页面报错，以及**搜不到**——最后这类本质上是在提交需求。所以反馈和求资源共用一张工单表，因为落地动作是同一个。

### 入口

四个触点，全部自动带上上下文，用户不用重复描述：

| 位置 | 形态 | 带上什么 |
|---|---|---|
| 结果卡「这条有问题」 | 三选一，可以不打字 | 标题、平台、分享链接、当时的搜索词 |
| 零结果页「告诉我你要找什么」 | 资源名 + 补充说明 | 关键词 |
| 报错页「上报这个错误」 | 错误原文 + 一句说明 | 错误信息、关键词 |
| 页脚「求资源 / 反馈问题」 | 兜底入口 | — |

结果卡上那个最重要。用户判断"这条有问题"的时机只在他看结果的那一屏，放到页脚或者独立页面等于没有。

### 类型（kind）

`dead_link` 链接失效 · `pwd_wrong` 提取码不对 · `wrong_content` 内容不符 · `missing` 求资源 · `bug` 页面报错 · `other` 其他。

固定枚举，不收自由文本分类：十条"打不开"没法聚合，十个 `dead_link` 能直接定位到一条该隐藏的链接。

### 防滥用

- 单 IP 10 分钟 5 条，**只对通过校验的提交计数**。用户忘填关键词不该消耗额度，重复点同一条也不该（同访客 + 同链接 24 小时内只存一条，重复提交直接返回成功）。
- 链接只放行 `http/https`，`javascript:` 之类在入口就清空。
- 字段统一截断：说明 500 字、关键词 100 字、联系方式 120 字。
- 联系方式选填，只进库，前后端都不展示给第三方。

### 自动闭环

同一条链接被 **2 个不同访客** 报 `dead_link`，30 天内从搜索结果里隐藏。阈值取 2 而不是 3：这个站流量小，凑齐 3 个人可能永远等不到。

隐藏列表在内存里缓存 60 秒。过滤只发生在响应出口，缓存里存的仍是上游原始结果，否则隐藏要等缓存过期（最长 5 分钟）才生效。`total` 保持上游原值——本地过滤和上游分页本来就对不齐，为了数字好看去改它只会让翻页出现空洞。

看板顶部的「已被自动隐藏」区块会列出这批链接，自动隐藏误伤了能立刻看见。

### 数据在哪

和埋点同一个 SQLite 文件 `backend/data/analytics.db`，表名 `feedback`，备份一份就够。

```bash
# 看全部待办（本机装了 sqlite3 才需要）
sqlite3 backend/data/analytics.db \
  "select id, datetime(ts,'unixepoch','+8 hours'), kind, target, query \
   from feedback where status='new' order by ts desc;"

# 看某个词的求资源人数
sqlite3 backend/data/analytics.db \
  "select query, count(*) from feedback where kind='missing' group by query order by 2 desc;"
```

`FEEDBACK_ENABLED=false` 可以单独关掉反馈，不影响埋点——统计和工单是两件事，不该互相绑死。

## 精选链接（自有资源库）

目标只有一句话：**把搜索流量导向站方自己的分享链接，而不是上游 API 给的第三方链接。** 上游结果照常显示，自有链接在命中时置顶、带"本站精选"徽章——用位置和站方担保赢，不做点击偷换。

### 机制

| 件 | 说明 |
|---|---|
| `own_links` 表 | 主关键词 + 别名（逗号分隔）+ 分享链接/提取码/文件数/大小/备注，和埋点、反馈共用同一个 SQLite |
| 匹配 | 归一化（小写、全角转半角、去空白）后**双向包含**：搜索词含关键词（接住"尸兄动画全集"这类变体），或关键词含搜索词（接住短词搜长标题）；标题兜底。不搞分词——量级小，简单规则 + 未命中日志闭环的覆盖率碾压复杂算法，且出问题一眼能定位 |
| 置顶 | 命中的卡固定排结果最前，最多 3 张（`own.MAX_MATCH`），上游结果紧随其后。匹配在响应时做、不进缓存：改配置/补别名立刻生效 |
| 平台筛选 | 用户筛选了平台时，只出同平台的精选卡——筛选名存实亡等于没有筛选 |
| 未命中日志 | 第一页搜索没接住任何精选卡的词全部落 `own_misses` 表（去重计数，翻页不记）。看板"搜过但没接住的词"榜按次数降序，**补了别名的词自动从榜上消失**——榜单永远是纯待办。补录按钮直接把词填进表单 |
| 埋点 | `click` 事件带 `source`（own / upstream），看板首屏"自有占比" = 精选卡点击 / 总点击。这是这套机制唯一的北极星指标 |
| 失效兜底 | 复用 dead_link 自动隐藏机制：精选卡和上游卡一样带 shareUrl，被 ≥2 个独立访客报失效后由 `_apply_hidden` 统一摘掉，"已被自动隐藏"区块里能看到。**没有单独做永久停用**——30 天窗口过了会回来，所以看板出现提醒要当天补新分享 |
| 上架清单 | `GET /api/admin/plan`：近 N 天被搜过的词，**按去重人数从高到低排**。不看点击、不看有没有结果、不分类。**用 `match()` 判定覆盖，已上架的词直接从清单消失**。每页 20 条，底部分页条显示 `1–20 / 共 N 个词`；上架导致总数变少时会自动回退到最后一页。可「复制本页清单」或点「补录」填表 |
| 计数口径 | 排序按 `u`（`COUNT(DISTINCT visitor)`，多少人搜过），`n`（搜索次数）只做同分排序——一个人刷 20 次不该压过 5 个人各搜一次；同一词的不同写法按归一化合并。全库都没有访客号时（老数据）退回按 `n` 排 |

### 快速补录（贴链接就行）

2026-11 加的：原来录一条要填主关键词、别名、标题、平台、链接、提取码、文件数、大小、备注 9 个格子，其中 7 个能从链接本身推出来。现在看板的录入表单只有一个粘贴框——把分享文案（或裸链接）贴进去，其余字段自动识别，人工只剩两件事：确认关键词、点保存。

| 字段 | 怎么来 |
|---|---|
| 平台 | 域名判定（pan.quark.cn→夸克、pan.baidu.com→百度、alipan→阿里…15 家） |
| 分享链接 | 正则抽取，顺手修 `?pwdabcd` 这种丢了等号的畸形参数；夸克/百度/迅雷/阿里会把提取码拼进 URL |
| 提取码 | 先看 URL 参数，再认文案里的"提取码/访问码/密码/口令"（`?pwd2hBE` 这种无分隔符的也认） |
| 分享标题 | 夸克链接调公开接口取真标题；其他平台从文案的「」《》"" 里取 |
| 文件数/大小 | 夸克 `sharepage/detail` 顶层全是文件时才填（含文件夹时算不准，留空比写错强） |
| 到期时间 | 夸克接口的 `expired_at`，剩余 ≤7 天在预览里标出来 |
| 主关键词 | 从标题推断（剥掉季数/清晰度/格式/网盘名等噪音）；从上架清单点进来的单条直接用清单里那个词——那是用户真正搜过的 |
| 别名 | 不填。双向包含 + 标题兜底已经接住大部分变体，真接不住的词会出现在未命中榜里再补 |

解析行为在 `own.parse_share`（纯本地正则）+ `_quark_meta`（夸克公开接口，无需登录，实测可用；任何失败都只记 error 不拦保存）。切段规则：按空行分块，一块一条；没有空行的连续列表里，提取码归它前面最近的链接——微信导出的文案里"上一条的提取码"正好落在这条链接前面是常态，按段内第一条取必错。

预览时同 URL 已在库里会标「已收录，跳过」，重复粘贴不会录重。多行一次贴多条，一次保存全部。旧的手动表单收在「手动录入 / 编辑已有链接」折叠里，编辑已有链接仍走它。

### 运营节奏（每周 30-60 分钟）

1. 打开 `/admin` 的「上架清单」：按搜索人数从高到低排好了，已上架的自动剔除。点「复制本页清单」拿去转存，人数明显变薄的尾部不用做；
2. 上游搜 → **验货**（看文件列表，上游大量挂羊头卖狗肉）→ 转存自己夸克 → 分享（选永久）；
3. 看板补录：贴分享文案 → 确认关键词 → 保存，即时生效；
4. 前台用主关键词 + 一个变体验证置顶卡出现；
5. 来源于求资源工单的，工单标"已处理"。

上游链接的信任预期是"网友随手分享"；精选卡的预期是"站方担保"。**验货那步不能省，失效要当天补**——徽章的信用错一次就掉一截。

`OWN_ENABLED=false` 是总闸，关掉只影响精选卡，埋点和反馈不受影响。

## 点击时转存（链接不外露）

2026-11 的主路径：**不再人工补录关键词库**，改成用户点开时才动手——把上游那条分享用我们自己的网盘账号转存一份，建一个我们名下的分享，再把用户送过去。

### 为什么不补录

补录的前提是"提前知道用户会搜什么"，实际覆盖率永远追不上长尾，而且一条条贴链接是纯人力。改成点击时转存后，**长尾自动被覆盖**：只要有第一个人点，这条资源就进了我们的库，第二个人点开是 0 延迟。

### 链路

```
搜索 → 返回 /go/<签名token>（不是真实链接）
  ↓ 用户点开
过渡页「正在准备资源」
  ↓ POST /api/resolve
  ├ 命中 mirrors 表 → 直接给我们的分享链接
  ├ 未命中 → 转存 → 建分享 → 落库 → 给我们的分享链接
  └ 失败 → 回退到上游原链接（自动带上提取码）
```

| 环节 | 位置 | 说明 |
|---|---|---|
| 签名 token | `resolver.make_token` | HMAC 签名，只放跳转必需字段。**没有签名就是开放重定向**，别人能拿本站当钓鱼跳板；域名还有白名单，非网盘域名签不出来 |
| 过渡页 | `GET /go/{token}` | 转存要 1–3 秒，直接 302 会把这段时间变成白屏。页面上有"资源较大，稍等"和失败时的返回/反馈出口 |
| 转存 | `pan.transfer` → `sdk/adapters/*` | 挑账号（按失败次数升序）→ 解析分享 → 转存进临时目录 → 建永久分享。失败按语义换账号：登录态失效/空间满才换；链接失效、违规、体积超限直接抛，不折腾账号 |
| 映射表 | `mirrors` | `src_key` 是忽略 `?pwd` 的链接指纹，同一份分享带不同提取码不会重复转存。同时记 `account_id` 和 `fids`，回收时才知道去哪个号删哪些文件 |
| 结果 | `_public_body` | 只在响应出口替换链接，**不写进缓存**：缓存留着原始 URL，`_apply_hidden` 才有东西可比，改密钥/关开关也立刻生效 |

前端只拿到 `/go/xxx`，拿不到真实链接。提取码也不再下发到卡片上（落地页给的码和上游的码可能不是一个），卡片上的"复制提取码"会自动消失。

### 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `MIRROR_ENABLED` | `true` | 总闸。关掉恢复老行为（直接给上游链接） |
| `MIRROR_MAX_GB` | `30` | 单条资源超过这个体积就不转，直接回退。**别为了多覆盖调大**——网盘空间是硬成本 |
| `MIRROR_FAIL_TTL` | `600` | 失败结果缓存 10 分钟，避免每个点击都去打接口 |
| `MIRROR_MAX_ATTEMPTS` | `3` | 一次转存最多试几个账号，失败换下一个 |
| `MIRROR_MIN_INTERVAL` | `3` | 同一平台两次转存的最小间隔（秒）。**别调小**，并发转存是最容易被风控的行为 |
| `MIRROR_COLD_DAYS` | `30` | 零点击超过这么多天的映射，连文件一起回收 |
| `MIRROR_TIMEOUT` | `15` | 单次请求超时（秒） |
| `MIRROR_SECRET` | 空 | token 签名密钥，留空由 `API_KEY` 派生。换掉 = 已发出的旧链接全部失效（回退，不 404） |
| `MIRROR_FALLBACK` | `true` | 失败是否回退原链接。`false` 则提示"资源暂时打不开" |
| `MIRROR_QUARK_COOKIE` | 空 | **只用于升级迁移**：库里一个夸克账号都没有时导入一次，之后以表为准。新部署别填 |

### 账号在哪配

在 `/admin` 的「网盘账号」面板，**不再靠环境变量**。同一个平台可以放多个账号。

- **测试**：发一次轻量请求验登录态，失败会累加 `fail_count` 并记下原因，不会静默失灵
- **测试与维护 → 手工验一条链接**：绕开失败缓存，真跑一遍「解析 → 转存 → 建分享」。新接的网盘先用它验，别等真人点击
- **怎么选号**：`fail_count` 升序 + 同分随机打散，最多试 `MIRROR_MAX_ATTEMPTS` 个。只有「登录态失效」和「空间满」会换号；链接失效、内容违规、体积超限换号也没用，直接抛
- **凭据自动回写**：夸克响应头里的 `__puus`、百度轮换的 refresh_token、迅雷 / 光鸭刷新出的 access_token 都会落库，重启不用重填

| 平台 | 必填 | 备注 |
|---|---|---|
| 夸克 / UC | Cookie（含 `__puus`、`__uid`） | 请求中自动续期 |
| 百度 | Cookie（含 BDUSS + STOKEN）或 refresh_token | 有 refresh_token 可自愈过期 |
| 迅雷 | refresh_token | 每请求都要现换 access_token 并带 `x-captcha-token` |
| 光鸭 | refresh_token 或 access_token | 转存不回 fid，靠目录前后 diff 找文件，别并发 |
| 移动 139 | Cookie（含 `authorization`）+ 临时目录 | 请求体 AES-128-CBC 加密，临时目录必须先在盘里建好 |

抓 cookie：浏览器登录对应网盘 → F12 → Network → 任意接口请求 → 复制整条 `Cookie` 请求头。填完先验一条：

```bash
curl -s -X POST http://127.0.0.1:8000/api/admin/mirror/test \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://pan.quark.cn/s/xxxx","pwd":""}'
# {"code":0,"data":{"ok":true,"url":"https://pan.quark.cn/s/yyyy","reason":"","cached":false}}
```

看映射表状态：`GET /api/admin/mirror` → `ok` / `fail` / `hits` / `bytes` + 最近 30 条。
`fail` 里清一色"资源不存在或已失效"就是源链接烂了，不用管账号；清一色"登录态"才该换 cookie；清一色"超出转存容量阈值"就调 `MIRROR_MAX_GB` 或换大容量账号。

### 接口从哪来（改之前必读）

夸克**没有**这套 API 的开放文档。`sharepage/token`、`save`、`task`、`share` 全部来自社区逆向，公开出处是 alist 的夸克驱动与 issue（贴过完整请求/响应体）、`Cp0204/quark-auto-save` 这类转存脚本。可信度分档：

| 接口 | 可信度 | 依据 |
|---|---|---|
| `sharepage/token`、`detail` | 高 | `own._quark_meta` 已在线上跑了几个月，公开接口、无需登录 |
| `sharepage/save`、`task` | 高 | alist issue #2968 里有原始请求体，参数逐字对得上（fid_list / fid_token_list / stoken / pdir_save_all / status=2 / save_as_top_fids） |
| `POST /share` | 中 | 路径确定，但 `url_type` / `expired_type` 的取值各来源**说法冲突**（有资料写 1 = 永久，代码用的是 4） |
| cookie 字段 | 中 | `__pus`/`__puus` 是社区共识，实际可能还需要 `__kp` |

正因为中可信度那两行，代码里有到期时间自检：建完分享读回真实 `expired_at`，不是永久就写日志并在 `/api/admin/mirror` 的 `reason` 里留一条。**别凭记忆改这些常量**——没有文档，唯一可信的是在自己账号上跑一次。

要自己核实时抓一次包就行：DevTools → Network → 手动转存一次 → 看 `/share/sharepage/save` 的请求体和 `/task` 的响应，和我们代码里的字段逐条比。

### 三个必须知道的约束

1. **容量是硬成本**。转存永久占我们账号空间，而且**分享出去后不能删源文件**（删了分享立刻断）。30G 阈值就是为这个设的——宁可这一条给原链接，也不要把账号塞爆。
2. **账号会一起死**。同一个平台的多个账号往往是用同一批小号一起注册的，风控一波带走一片。多账号能扛住"单个号被限"，扛不住"平台清场"。cookie 全失效时点击会回退到原链接——这比签名过期更隐蔽（用户照样能打开，只是链接不是我们的），所以看板把 `mirror` 事件单独记了 `source=cached/fresh/fallback`，`fallback` 占比突然升高就是账号出事了。
3. **责任主体变了**。上游链接的性质是"网友随手分享"，转存后是**我们名下的分享**——违规内容、侵权投诉的接收方从别人变成你。这一条在技术上无解，只能靠验货时把关。

### 与精选链接的关系

`own_links` 那套（人工补录 + 置顶卡）保留但不再是主路径：它现在只用于"我愿意为这个词做站方担保"的高价值词。**未命中榜和上架清单的运营逻辑照旧**，只是不必再为了覆盖率一条条补——长尾交给点击转存兜。

## 网盘 SDK（`backend/app/sdk/`）

把"下载分享 → 转存到自己盘 → 建新分享"这套动作抽象成统一接口，业务层不认识任何一家网盘。目前接入了 **夸克 / UC / 百度 / 迅雷 / 光鸭 / 移动 139**。

```
sdk/
  errors.py     语义异常（按"该做什么"分类，不按网盘、不按错误码）
  types.py      Account / ShareInfo / File / ShareLink / TaskResult
  cookie.py     cookie 解析、合并、脱敏
  http.py       请求层：注入公共头、回收 Set-Cookie、网络重试
  urls.py       分享链接识别（7 种 URL 形态 + 域名白名单）
  base.py       PanClient 抽象基类 + mirror() 统一流水线
  registry.py   类型注册表 + 工厂
  adapters/
    quark/  baidu/  xunlei/  guangya/  c139/
```

### 唯一要记住的东西：异常决定动作

上层只判断四件事，不查任何错误码：

| 异常 | `account_bad` | `share_bad` | 上层动作 |
|---|---|---|---|
| `AuthError` | ✓ | | 标记账号失效，换下一个，别重试 |
| `QuotaExceededError` | ✓ | | 换账号，不重试 |
| `ShareInvalidError` / `ShareExpiredError` | | ✓ | 下架这条资源，回退原链接 |
| `PasscodeRequired/WrongError` | | ✓ | 提示用户补码，**不下架** |
| `ViolationError` | | ✓ | 下架 + 删已转存的文件 |
| `TooLargeError` | | | 直接回退。换账号没用，也别记在账号头上 |
| `TaskTimeoutError` / `UpstreamError` | | | 可以重试一次 |
| `ConfigError` / `UnsupportedError` | | | 我们自己的问题，重试没意义，要报警 |

新增网盘时**只做一件事**：在它的 `ERROR_MAP` 里把码映射到上面已有的类。映射不出来说明遇到了新情况——先加类，别塞进 `UpstreamError`，否则线上排查又只剩一句"转存失败"。

### 加一个网盘

1. 新建 `sdk/adapters/<名字>/client.py`，继承 `PanClient`，实现 `share_info` / `transfer` / `list_files` / `delete` / `_build_requester`，建议覆盖 `probe`
2. 类上写 `type = "<名字>"`，加 `@register`
3. 在 `sdk/adapters/__init__.py` 里 import 一次
4. 如果搜索上游用的平台 key 和这里的 type 不一致，在 `registry._ALIASES` 里对齐（比如 `yidong → c139`）

业务层、后台面板、清理任务都不用改——注册表就是为这个存在的。

### 几条踩出来的坑（改之前必读）

- **顺序敏感的 cookie 串**：`cookie.merge` 用 dict 保序。乱序会导致明明填了正确 cookie 却一直 401。
- **`trust_env = False`**：本机开着代理时 requests 会走代理，表现成"读超时"而不是连接失败，排查半天看不出是代理。
- **每线程一个 Session**：`requests.Session` 不是线程安全的（cookie jar 会竞争），但每请求新建又丢掉连接复用。转存一次要打六七次同一个域名，复用很值。
- **HTTP 404 ≠ 上游故障**：分享被删时夸克直接回 404。分享侧的请求要传 `gone_error=ShareInvalidError`，否则会变成"上游故障"，还会把账号的失败计数加上去。
- **转存失败要能看出来是哪一头坏了**：后台「手工验链」默认绕开失败缓存，否则改完配置再验，拿到的还是上次那句缓存原因。


## 群入口（进站 / 获取链接弹二维码）

两个触发点，配置全在 `/admin` 看板的「群入口配置」面板里改，**改完即时生效，不用重启**：

| 触发点 | 行为 |
|---|---|
| 进站 | 页面打开后延迟（默认 800ms）弹一次；默认每次打开都弹，频率设为「每天一次」时按天抑制 |
| 点「打开链接」 | **每次点击都弹**，弹窗里有「不进群了，直接打开资源」兜底，该出口只管这一次，下一条链接仍会弹 |
| 页脚「进群交流」 | 手动入口，随时能看 |

配置存在同一个 SQLite（`app_config` 表），支持最多 4 个群（QQ / 微信 / 其他），每个群可传二维码（落到 `backend/data/uploads/`，通过 `/uploads/<文件名>` 访问）、填群号（弹窗里可复制）、填手机端一键加群链接。`GROUP_ENABLED=false` 是总闸。

点击行为记在埋点里，kind 是 `group`（show / close / copy_number / continue），看板流水里能看到。

> **弹层的类名别用 `popup` / `modal` / `mask` / `overlay` / `dialog` 这类词。** 广告拦截器（uBlock、AdGuard 等）的“烦人元素”规则会按类名批量隐藏，命中的结果是**蒙层还在、卡片消失**——看起来像弹窗坏了，其实是 CSS 被注入的隐藏规则盖掉了。当前用的是 `pt-veil`（蒙层）+ `pt-panel`（卡片），前缀 `pt-` 是项目缩写，不含任何可疑词。2026-11 线上弹窗不显示就是 `.gg-mask` / `.gg-box` 被拦导致的。

## 已知问题

- 上游单次查询耗时波动大：快的 2 秒，慢的接近 60 秒。后端超时设为 75 秒（官方要求 >60 秒，否则提前中断照样扣费）。
- `credits` 字段实测多次为 0，计费逻辑以上游账单为准，不要用这个字段估算成本。
- **只有一条通道，没有降级**。签名过期时搜索直接失败并提示重新抓取。这是刻意的：以前挂过降级链，签名失效的请求会被悄悄转到慢通道，日志里只剩“慢”，看不出真因。

## 上线前要做的

- [ ] `backend/.env` 不要提交到任何仓库
- [ ] 设 `ADMIN_TOKEN`，否则公网打不开 `/admin` 看板
- [ ] 在 `/admin` 的「网盘账号」里至少加一个账号并点「测试」通过，否则点击转存全走回退（等于没开）
- [ ] 用「测试与维护 → 手工验一条链接」跑通一次完整转存，别等真人点击来发现配置错了
- [ ] 关掉 `--reload`，保持 `--workers 1`（多进程会让缓存和串行锁各自独立，详见下文 Linux 部署说明）
- [ ] 前面挂 Nginx，加 HTTPS
- [ ] 加访问频率限制，否则一个脚本就能把 apikey 的余额刷空
- [ ] `CORS_ORIGINS` 改成正式域名，别留 localhost

---

# 部署到 Linux

## 1. 服务器依赖

需要 **Python 3.10+**（代码用了 `X | None` 语法）和 venv。

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y python3 python3-venv python3-pip nginx

# Rocky / CentOS / Alma
sudo dnf install -y python3 python3-pip nginx
```

建一个专用账号，别用 root 跑：

```bash
sudo useradd -r -s /sbin/nologin pantrace
sudo mkdir -p /opt/pantrace
sudo chown -R pantrace:pantrace /opt/pantrace
```

## 2. 拉取代码

代码托管在 GitHub（私有仓库 `zhummq/pantrace`），服务器直接 clone。前端产物 `frontend/dist` **已经入库**（构建只在本地做，服务器不用装 Node），所以拉下来就是完整的：

```bash
cd /opt
sudo git clone git@github.com:zhummq/pantrace.git pantrace
sudo chown -R pantrace:pantrace /opt/pantrace
```

> 服务器 clone 私有仓库需要 deploy key：`ssh-keygen` 生成一对，公钥加到 GitHub 仓库的 Settings → Deploy keys（只读即可）。装 Nginx 前先 `sudo ./deploy/install-nginx.sh` 的前置检查会兜住大部分配置问题。

clone 下来的东西**没有**（都不在 git 里，需要手动补）：

- `backend/.env`——下一节自己建
- `backend/.venv/`——按第 1 节建 venv 装依赖
- `backend/data/`——埋点库，服务启动后自动生成

## 3. 配置

```bash
cd /opt/pantrace
cp backend/.env.example backend/.env
vim backend/.env          # 填 API_KEY、IDR_SIGN、IDR_TIME 三项
chmod 600 backend/.env    # 里面是钱和凭据，别人别读
```

三个必填项：

| 变量 | 说明 |
|---|---|
| `API_KEY` | `idr_` 开头的 36 位 key |
| `IDR_SIGN` | `v1:` + 32 位十六进制，浏览器控制台抓的签名 |
| `IDR_TIME` | 与签名同一次请求里的 `x-idr-time` |

`IDR_SIGN` 会过期。过期后搜索会返回「IDR 签名已失效（1004）」，重新抓法：

> 控制台随便搜一次 → F12 → Network → 找 `mapi` 请求 → 复制请求头 `x-idr-sign` 和 `x-idr-time` → 填进 `.env` → 重启服务

前端会把这个提示原样显示给用户，不会假装是网络问题。

`SERVE_FRONTEND` 保持默认 `true`：Nginx 是纯反代，前端页面全靠后端托管（见第 6 节）。改成 false 页面就 404。

## 4. 先手动跑一遍

```bash
cd /opt/pantrace
chmod +x start.sh
./start.sh
```

看到 `Uvicorn running on http://0.0.0.0:8000` 就正常，`Ctrl+C` 停掉，交给 systemd。

## 5. systemd 守护（关掉 SSH 也不停）

`./start.sh` 跑的是当前会话的前台进程，**关掉 SSH 窗口就会收到 SIGHUP 挂掉**。交给 systemd 才是正解：断线不受影响、崩溃自动重启、服务器重启自动拉起。

用脚本装，路径和用户按实际情况自动生成（不写死 `/opt/pantrace`）：

```bash
cd /opt/pantrace
sudo ./deploy/install-service.sh          # 装好并启动
# 项目在别的目录就加 --root，端口不是 8000 就加 --port
```

脚本会先做前置检查（venv、依赖、`.env`、`frontend/dist`、端口占用），通过后写 `/etc/systemd/system/pantrace.service` 并 `enable`。装完自动打印状态和健康检查结果。

```bash
sudo ./deploy/install-service.sh --print   # 只想看会生成什么，不安装
```

日常命令：

```bash
systemctl status pantrace            # 看状态
systemctl restart pantrace           # 改完代码/配置后重启
journalctl -u pantrace -f            # 实时日志
journalctl -u pantrace -n 100        # 最近 100 行
systemctl disable --now pantrace     # 停止并取消开机自启
```

> 注意：装的路径如果在 `/root` 或 `/home` 下，脚本会自动把 `ProtectHome` 设为 `false`，否则 systemd 会屏蔽这些目录，服务读不到代码。手动改 unit 时别漏了这条。

## 6. Nginx

Nginx 是**纯反代**：所有请求（页面、静态资源、API、看板）都转给后端，前端静态文件由 FastAPI 直接托管（`SERVE_FRONTEND=true`，默认开着）。没有任何"前端产物拷到别的目录"的步骤。

```bash
cd /opt/pantrace
sudo ./deploy/install-nginx.sh                      # 默认 server_name _
sudo ./deploy/install-nginx.sh --domain example.com # 绑域名
sudo ./deploy/install-nginx.sh --domain example.com --ssl --email you@mail.com
                                                    # 绑域名 + 申请证书 + 强制跳 HTTPS
sudo ./deploy/install-nginx.sh --print              # 只看会写什么配置
```

脚本做四件事：准备证书续期校验目录（`/var/www/letsencrypt-acme`，只装一次）、写 `/etc/nginx/conf.d/pantrace.conf`、`nginx -t` 校验（失败自动回滚）、reload 并自测三个路径；带 `--ssl` 时再向 Let's Encrypt 申请证书并把 HTTP 301 到 HTTPS。

`--ssl` 的几个约束：域名必须已经解析到本机、80 端口能公网访问、不能是 IP（Let's Encrypt 不给 IP 签证书）。证书申请失败不会把站点搞挂——脚本会保留 HTTP 配置继续服务。证书续期由 certbot 装好时自动启用的 `certbot.timer` 负责，到期前 30 天自动续，不用管。

**为什么不走"Nginx 托管静态文件"的常规做法**：Nginx worker 读不了项目目录（在 `/root`、`/home` 下就是 403），旧方案为此把 dist 拷一份到 `/var/www`，代价是前端每次改动都要多一步同步。现在这个站流量小、产物只有 80KB，全反代换来部署一步到位，值得。

配置里一条不能动：

- `proxy_read_timeout 90s` 必须大于后端的 75 秒，否则上游还没返回 Nginx 先断，用户看到 504 但那笔已经扣费。

**`SERVE_FRONTEND` 必须保持 `true`**（默认就是）。改成 false 后端只做 API，反代出来的页面全是 404——脚本会检测这种情况并拒绝安装。真想收紧安全面，把 systemd unit 里的 `--host 0.0.0.0` 改成 `127.0.0.1`，但那样 Nginx 一挂就彻底进不去了。

## 7. 防火墙与 HTTPS

```bash
sudo ufw allow 80,443/tcp          # 云服务器还要在控制台安全组放行 80 和 443
```

HTTPS 不用手工跑 certbot，直接用脚本（见上一节）：

```bash
sudo ./deploy/install-nginx.sh --domain example.com --ssl --email you@mail.com
```

> 只绑了域名没配证书时，浏览器敲域名会默认先试 HTTPS，443 没人监听就直接报"无法访问此网站"——手动敲 `http://` 前缀能进，但这不是长久方案，把证书配上。

## 8. 更新代码

本地和服务器两边流程：

```bash
# 本地：改代码 → 构建（改了前端才需要）→ 提交推送
cd frontend && npm run build          # dist 已入库，build 完一起提交
git add -A && git commit -m "..." && git push

# 服务器：拉取 + 重启，完事
cd /opt/pantrace && sudo -u pantrace git pull
sudo systemctl restart pantrace
```

后端、前端、配置的更新方式完全一样——静态页面由后端托管，`git pull` 带下来的新 dist 靠重启生效。

> **缓存策略由后端发头，不写在 nginx 里。** 站点是纯反代（`/docs`、`/openapi.json` 都能通），静态文件由 FastAPI 的 `StaticFiles` 托管，所以 `location = /index.html` 这种写法对用户实际访问的 `/` 是无效的，写错还会让首页 404。现在的规则在 `main.py` 的 `cache_control` 中间件里：
> - `/assets/*` → `public, max-age=31536000, immutable`（文件名带内容 hash，内容永不变）
> - `/` 与 `/index.html` → `no-cache`（每次带 ETag 回源，没变返回 304）
>
> 首页必须禁掉启发式缓存。否则浏览器按 `Last-Modified` 自行估算缓存时长，发版后老访客会继续跑旧 bundle，表现是“新功能没上线”——2026-11 群入口弹窗不出现就是这个原因。

两个注意：

- **服务器上别手改代码**。本地改完 push，服务器 pull。服务器上的 `.env` 和 `data/` 不在 git 里，`git pull` 不会碰它们。
- `git pull` 提示冲突，说明服务器上有本地改动没提交——先 `git status` 看是什么，确认没用就 `git checkout -- <文件>` 丢弃，别在服务器上做开发。

> 记得先 push 再发版：GitHub 上的提交历史就是回滚的底气，改坏了 `git checkout <旧提交>` 找回上一版。

## 9. 从旧部署迁移（一次性）

服务器上跑的还是旧方案（Nginx 托管 /var/www、代码靠上传）时，按这个顺序收编：

```bash
cd /opt/pantrace

# 1. 看现状：有没有在服务器上手改过的文件
git status

# 2. 丢弃服务器上的本地改动（.env 和 data/ 不在 git 里，不受影响）
git checkout -- .

# 3. 拉最新代码（dist 已入库，这一步连前端产物一起带下来）
git pull origin master

# 4. 核对 .env：SERVE_FRONTEND 必须是 true（或干脆没有这一行）；公网必须配 ADMIN_TOKEN
grep -E 'SERVE_FRONTEND|ADMIN_TOKEN' backend/.env

# 5. 重启后端并直连验证——此时静态页面已经改由后端托管
sudo systemctl restart pantrace
curl -s http://127.0.0.1:8000 | grep -o 盘迹 && curl -s http://127.0.0.1:8000/api/health

# 6. 重写 Nginx 配置为纯反代（已有证书会自动跳过申请）
sudo ./deploy/install-nginx.sh --domain 你的域名 --ssl --email 你的邮箱

# 7. 关键：certbot 的续期配置里记着旧的 webroot 路径（/var/www/pantrace），
#    不改的话证书到期会静默续期失败。改成新路径并验证：
sudo sed -i 's#/var/www/pantrace#/var/www/letsencrypt-acme#g' /etc/letsencrypt/renewal/*.conf
sudo certbot renew --dry-run     # 看到 "Congratulations" 或 "simulated renewal" 即成功

# 8. 旧发布目录清掉
sudo rm -rf /var/www/pantrace

# 9. 公网自测：浏览器开 https://域名 、/admin 看板、搜一次真词
```

埋点库不用动：`backend/data/analytics.db` 留在原地，新版启动时会自动给旧表补 `bot` 列。

---

## Linux 上三个必须注意的点

| 点 | 说明 |
|---|---|
| **.env 用绝对路径读取** | 代码已按文件位置定位 `backend/.env`，不再依赖当前工作目录。如果你改了启动方式（比如 `cd /` 再起 uvicorn），也照样能读到。 |
| **只能单 worker** | 缓存和上游串行锁都在进程内。`--workers 4` 会让四个进程各排各的队，等于没排队，上游立刻把你打到超时。要横向扩展必须先把这两样换成 Redis。 |
| **Nginx 超时要大于 75 秒** | `proxy_read_timeout 90s`。Nginx 默认 60 秒，会在后端还没返回时先断，用户看到 504，但上游其实已经扣了费。 |

另一个容易忽略的：**别在 `.env` 里开 `USE_ENV_PROXY=true`**，除非服务器确实只能通过代理出网。海外机器和大部分云主机都能直连，走代理反而慢。
