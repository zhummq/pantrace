# PanSou 插件选型清单

> 目标：从 113 个插件里挑出真正值得引进的几类，不做全量引入。
> 所有可达性数据为**本机直连**实测（`trust_env=False`），与线上 `use_env_proxy=False` 口径一致；但本机 ISP/网络路径与服务器不同，**上线前必须在部署机重跑**。

---

## 0. 结论先行

1. **113 个插件只有 5 种技术形态，但真正该按「接口稳定性」分类**——不是 HTML 还是 JSON 的区别，而是**对方会不会把人机验证放上来**。
2. **只选 3 类，共 5~6 个源**。同类站数据高度重合，且大量使用同一套建站程序，多选只是多担一份「一起死」的风险。
3. **明确排除两类**：反爬型（实测直接返回 428 人机验证）、二级聚合套壳（我们直接调被套的那家更好）。
4. **首选「开放 JSON API」型**。实测 `qkpanso` 系接口直连可用、无鉴权、900 条结果、1.2 秒返回，字段直接含 `link` + `disk_pass` + `files`，零解析成本。

---

## 1. 113 个插件的技术形态

| 形态 | 数量 | Python 侧成本 | 判断 |
|---|---|---|---|
| HTML 解析 | 61 | requests + lxml，一个通用模板 | 可做，需逐站适配选择器 |
| JSON 接口 | 31 | 最省事，直接取字段 | **首选** |
| 混合（HTML+JSON） | 12 | 两步：列表页 + 详情页 | 可做，成本翻倍 |
| 反爬（cloudscraper/goja） | 4 | 需额外依赖，且随时失效 | **排除** |
| 其他/无外链 | 5 | — | 排除 |

> `discourse`（linux.do）没有任何外部域名，走内置 API，属特例。

### 可达性实测

- 提取独立域名 140 个（同一插件可能引用多个镜像）
- **可达 75 个（54%）**，其余 DEAD / TIMEOUT / SSL
- 叠上「自己解析网盘链接 + 非二级套壳」后，**真正候选 50 个**

不可达的里面，`TIMEOUT` 占多数（`haisou.cc`、`www.91panta.cn`、`jsnoteclub.com`、`pan666.net` 等）。上一轮用代理测 `haisou.cc` 是 200，说明**其中一部分是本机网络路径问题，不是站点死亡**——所以这份名单只能当"初筛"，不能当"判决书"。

---

## 2. 按接口稳定性分四类（选型的真正维度）

### A. 开放 JSON API —— 必选

| 站点 | 接口 | 实测 |
|---|---|---|
| `qkpanso.com` | `POST /v1/search/disk` | ✅ 200，900 条，61ms（首查）/1.2s |
| `www.misoso.cc` | `POST /v1/search/disk` | ✅ 200，8 条（书库搜） |
| `hunhepan.com` | `POST /open/search/disk` | ⚠️ 空返回，可能已废弃 |
| `kuake8.com` | `POST /v1/search/disk` | ❌ 404 |

返回结构（`data.list[]`）：

```
disk_id  disk_name  disk_pass  disk_type  files  share_user
shared_time  tags  link  size  weight  status
```

`link` 直接是网盘分享地址，`disk_pass` 是提取码，`disk_type` 是 `QUARK`/`BAIDU` 等。**这是产品化接口，对方靠它导流，不会轻易关。**

> 注意：短时间重复请求会超时（实测第二次 15s 超时，隔 5 秒重试恢复）。**必须加节流，不能并发刷。**

### B. HTML 页面解析 —— 选 2~3 个

服务端渲染，`lxml` 抽链接即可。代表：`www.ahhhhfs.com`（资源博客，一手分享）、`xiaokupan.com`、`www.panta`（已死）、`panta`/`panwiki`。

成本：每站一套选择器；优点是这类站不设防，长期可用。

### C. 安全验证 / 反爬 —— 排除

| 站点 | 实测结果 |
|---|---|
| `sopanya.com` | ❌ `code:428`，`"为保障正常使用，请完成安全验证后继续搜索"` |
| `buerchen.top` | ❌ 404，`控制器不存在:app\controller\404` |
| `sdso.top` | ❌ JS 挑战（随机 cookie + btoa + reload） |
| `panzun.cc` / `diduan` / `gying` | cloudscraper，Python 侧需额外依赖 |

**关键发现**：`sopanya` 和 `buerchen` 的接口路径都是 `/api/other/web_search`，是**同一套 ThinkPHP 建站程序**。一个加了人机验证，一个接口已删。说明这批站同源模板，**一家升级、全体变异**——选它们等于把稳定性押在别人是否升级上。

### D. 二级聚合套壳 —— 排除

`haisou`、`pan666`、`pansearch`、`melost`、`sousou`、`xys`、`yunso`、`yunsou`、`ash`、`ikantv`、`sdso`（11 个）。

它们本身就是聚合站，调它们等于做**三级聚合**：数据经过二手加工、延迟叠加、且我们无法控制其源质量。要这家的数据，不如找它的上游。

---

## 3. 按站点性质分五类

| 类型 | 数量 | 可达代表 | 与「盘迹」匹配度 |
|---|---|---|---|
| 综合网盘搜索 | ~40 | qkpanso、misoso、xiaokupan、haitunsou、leso、aipan | **高**，直接对口 |
| 资源博客 | ~5 | ahhhhfs、jsnoteclub（死）、xuexizhinan（SSL） | **高**，一手分享、不与盘搜重合 |
| 影视资源 | ~25 | aikanzy、quarktv、feikuai、hdmoli（死）、5266ys | 中，多为在线播放而非网盘直链 |
| 短剧 | ~4 | djgou、duanjuw | 中，新品类，量小 |
| BT/磁力/社交 | ~15 | nyaa（死）、tpb（SSL）、cilixiong、weibo、nsgame | **低**，不出网盘链接 |

---

## 4. 推荐方案：3 类 6 个源

| 序 | 类型 | 选谁 | 为什么 |
|---|---|---|---|
| 1 | 综合网盘搜索 | `qkpanso` | 唯一实测出 900 条且结构完整的开放接口 |
| 2 | 综合网盘搜索 | `misoso` | 同源程序不同库，可作补充 |
| 3 | 资源博客 | `ahhhhfs` | 一手分享，不与盘搜站重合，站不设防 |
| 4 | 资源博客 | `jsnoteclub`（待线上复测） | 技术教程类，品类差异化 |
| 5 | 影视 | `aikanzy` 或 `quarktv` | 补影视品类，**需先确认给的是网盘链接还是在线播放** |
| 6 | 短剧 | `djgou`（第二期） | 单集短剧分享，年轻用户需求高 |

**不选的理由要写清楚**：

- 综合盘搜只选 2 个，不是 10 个：这类站抓的是同一批微信群/TG 频道的分享，**数据重合度极高**。3 个和 10 个的召回差异很小，但并发压力和失效面翻 3 倍。
- 影视只选 1 个：`aikanzy` 这类站搜出来常是「在线播放地址」，不是网盘直链。**先验证产出物再决定要不要第二个。**
- 短剧放第二期：品类新、站少、稳定性未知，不占第一期名额。

---

## 5. 实施时必须守的三条

1. **按「每源独立状态」返回**。不要学 xiageba2 的静默降级（`catch` 后返回 `[]`）——搜索场景里源挂了必须让用户/日志看见，否则「全网没有」和「源挂了」无法区分。
2. **同类站限并发 + 加节流**。`qkpanso` 实测连续请求会超时；pansou 用 4 秒窗口 + 工作池不是没有原因的。
3. **每个源配一个「存活指纹」**。每天探一次，指纹变了（返回长度突降、出现 `challenge`/`security`/`404` 关键字）就告警。这批站死之前通常有征兆。

---

## 6. 复测命令

```bash
# 一批域名只看可达性（直连，和线上一致）
cd backend && ./.venv/Scripts/python.exe scripts/probe_source.py \
  --hosts qkpanso.com www.misoso.cc www.ahhhhfs.com --timeout 8

# 具体搜索接口，看能不能捞出网盘链接
./.venv/Scripts/python.exe scripts/probe_source.py \
  --url "https://qkpanso.com/v1/search/disk" --kw 三体 --show
```

> `probe_source.py` 目前只支持 GET；`qkpanso` 需要 POST + JSON body，实测脚本见 `.workbuddy/probe/` 下的记录，正式接入时应把 POST 支持补进探针。
