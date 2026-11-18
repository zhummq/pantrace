<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ResultCard from './components/ResultCard.vue'
import FeedbackDialog from './components/FeedbackDialog.vue'
import QqGroupDialog from './components/QqGroupDialog.vue'
import { fetchPlatforms, fetchGroup, searchResources } from './api.js'
import { ensureTicket } from './ticket.js'
import { initTracking, track } from './track.js'

const PAGE_SIZE = 20

// 首页搜索框下的快捷词：都是长期有人搜的常青词，点一次直接出结果。
// 别放冷门词——点了没结果，等于在首页摆了个坏掉的按钮。
const sampleKeywords = ['三体', '庆余年', '流浪地球', '哈利波特']

const keyword = ref('')
const lastKeyword = ref('')
const activePlatform = ref('')
const platforms = ref([])

// ---- 结果池 ----
// 一次网络请求拿到的结果存这里，平台筛选只在池内过滤，绝不再打上游。
// 改之前 pickPlatform 直接 doSearch：用户"只想看夸克"要先付一次全平台查询，
// 再付一次夸克查询，点五个平台就是五次全价。platforms 留空 = 上游全平台聚合。
const pool = ref({ items: [], platforms: '', total: 0, totalPages: 0, tookMs: 0 })
const PREF_KEY = 'pt_pref_platform'

const items = ref([])
const page = ref(1)
const totalPages = ref(0)
const total = ref(0)
const tookMs = ref(0)

const loading = ref(false)
// 等 40 秒的时候，一个跳动的秒数比一句"加载中"有用得多
const elapsed = ref(0)
let elapsedTimer = null

// 序号用于丢弃过期请求：用户连搜两次时，先发那次的后台补拉不能覆盖新结果
let searchSeq = 0
let checkTimers = []

const error = ref('')
const searched = ref(false)

const showEmpty = computed(() => !loading.value && !error.value && searched.value && items.value.length === 0)
const sourceCount = computed(() => platforms.value.length)

const totalText = computed(() =>
  total.value > 0 ? total.value.toLocaleString('zh-CN') : String(total.value)
)

// ---- 反馈 / 求资源 ----
// 一个弹层三种形态，共用一个提交接口。上下文（当前关键词、这条结果）
// 由打开时带进去，用户不用再重复描述一遍。
const fbOpen = ref(false)
const fbMode = ref('request')
const fbItem = ref(null)
const fbError = ref('')

function openFeedback(mode, payload = {}) {
  fbMode.value = mode
  fbItem.value = payload.item || null
  fbError.value = payload.error || ''
  fbOpen.value = true
}

function closeFeedback() {
  fbOpen.value = false
}

// ---- 群入口（进站弹一次 / 点开链接时拦一次）----
// 配置从 /api/group 拉，/admin 看板可改，前端不写死。
// 「once」策略由后端下发：session=每会话一次 / day=每天一次 / always=每次都弹。
const groupCfg = ref(null)
const groupOpen = ref(false)
const groupMode = ref('enter')
const pendingUrl = ref('')
// 用户在拦截弹窗里勾了「今日点击链接不再提示」，当天剩余时间不再拦。
// 用 ref 而不是每次读 localStorage：computed 靠它触发重算，勾上之后当前这页就立刻放行。
const acquireMuted = ref(false)

const groupIntercept = computed(() => {
  const cfg = groupCfg.value
  return !!(cfg?.enabled && cfg?.popup?.acquire) && !acquireMuted.value
})

// 拦截弹窗（acquire）：默认每次点「打开链接」都弹，不做"看过了就放行"的自动记忆。
// 早先这里按会话记标记，同一标签页点过一次之后连刷新都再也不弹，看起来跟坏了一样；
// 而"本页只弹一次"又会让第二个链接被静默放行——两种都不对。宁可多点一次。
// 想直接走的人，弹窗里一直有「不进群了，直接打开资源」的出口。
//
// 唯一例外是用户自己勾的「今日点击链接不再提示」（pt_group_mute_acquire_day）：
// 那是他明确表态，按天放行，第二天零点自然恢复。

// 进站弹窗的重复控制：session=每次打开页面弹（默认），day=每天只弹一次，
// always=每次打开都弹。session 与 always 在"一个页面生命周期只挂载一次"的前提下等价，
// 保留两个值只是为了不改动后台已存的配置。
//
// 另有一条用户自己的开关：弹窗里勾「今日不再弹出」（pt_group_mute_day），
// 当天剩余时间不再自动弹。它优先于后台配置——用户明确说了不想看，就按用户来；
// 手动点「进群交流」不读这个标记。
// 两个标记故意分开：在进站弹窗里顺手一勾不该把点链接的拦截也一起关掉。
function todayKey() {
  const d = new Date()
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}

function readMuted(key) {
  try {
    return localStorage.getItem(key) === todayKey()
  } catch {
    return false
  }
}

function writeMuted(key) {
  try {
    localStorage.setItem(key, todayKey())
  } catch {
    /* 拿不到 storage 就只关掉这一次，下次还会弹 */
  }
}

function enterMutedToday() {
  return readMuted('pt_group_mute_day')
}

function enterSeenRecently() {
  if (enterMutedToday()) return true
  const once = groupCfg.value?.popup?.once || 'session'
  if (once !== 'day') return false
  try {
    const t = Number(localStorage.getItem('pt_group_enter') || 0)
    return Date.now() - t < 86400000
  } catch {
    return false
  }
}

function markEnterSeen() {
  if ((groupCfg.value?.popup?.once || 'session') !== 'day') return
  try {
    localStorage.setItem('pt_group_enter', String(Date.now()))
  } catch {
    /* 隐私模式拿不到 storage 就当没看过，多弹一次不是大事 */
  }
}

function openGroup(mode, url = '') {
  if (mode === 'enter') markEnterSeen()
  groupMode.value = mode
  pendingUrl.value = url
  groupOpen.value = true
  track('group', { meta: { action: 'show', mode } })
}

function closeGroup() {
  groupOpen.value = false
}

// 弹窗里勾了「今日不再弹出」：按形态各记各的标记。
// enter → 今天不再自动弹进站弹窗；acquire → 今天不再拦点击，链接直接放行。
function muteGroupPopup(mode) {
  if (mode === 'acquire') {
    acquireMuted.value = true
    writeMuted('pt_group_mute_acquire_day')
    return
  }
  writeMuted('pt_group_mute_day')
}

function groupContinue() {
  // 拦截弹窗里点了「直接打开资源」：补发跳转，弹窗关掉
  const url = pendingUrl.value
  groupOpen.value = false
  pendingUrl.value = ''
  if (url) window.open(url, '_blank', 'noopener')
}

function showGroupManual() {
  openGroup('enter')
}

// 打开链接的拦截：每次都先看群，弹窗里可「不进群了，直接打开资源」放行这一次
// 地址可能是 /go/ 站内跳转（上游结果已不再下发真实链接），相对地址同样放行
function openable(url) {
  return /^https?:\/\//i.test(url || '') || String(url || '').startsWith('/go/')
}

function handleOpen(item) {
  const url = openable(item.shareUrl) ? item.shareUrl : ''
  if (!url) return
  if (groupIntercept.value) {
    openGroup('acquire', url)
    return
  }
  window.open(url, '_blank', 'noopener')
}

const pageWindow = computed(() => {
  const tp = totalPages.value
  if (tp <= 1) return []
  const cur = page.value
  const size = tp <= 7 ? tp : 7
  let start = Math.max(1, cur - 3)
  if (start + size - 1 > tp) start = Math.max(1, tp - size + 1)
  return Array.from({ length: size }, (_, i) => start + i)
})

function syncUrl() {
  const params = new URLSearchParams()
  if (keyword.value.trim()) params.set('q', keyword.value.trim())
  if (activePlatform.value) params.set('platform', activePlatform.value)
  if (page.value > 1) params.set('page', String(page.value))
  const qs = params.toString()
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname)
}

// ---- 检测结论补拉 ----
// 两轮足够：3 线程 + 同域 0.25s 间隔，一页 20 条同平台约 5~8 秒出全。
// 补拉命中后端搜索缓存（5 分钟内），不碰上游，成本接近零；失败静默。
const CHECK_REFRESH_DELAYS = [6000, 16000]

function clearCheckRefresh() {
  for (const t of checkTimers) clearTimeout(t)
  checkTimers = []
}

function scheduleCheckRefresh(seq, args) {
  clearCheckRefresh()
  for (const delay of CHECK_REFRESH_DELAYS) {
    const timer = setTimeout(() => refreshCheckStates(seq, args), delay)
    checkTimers.push(timer)
  }
}

async function refreshCheckStates(seq, args) {
  // 用户已经翻了页或换了词：这次补拉的结果已经不属于当前视图
  if (seq !== searchSeq) return
  try {
    const data = await searchResources(args)
    if (seq !== searchSeq) return
    const next = data.items || []
    // 结果为空多半是撞上了上游抖动，别把眼前这页清掉
    if (!next.length) return
    // 整页替换而不是逐条合并：/go/ 签名里带时间戳，两次请求的链接串不同，
    // 按链接匹配根本对不上；同一次查询的排序是稳定的，替换不会打乱顺序。
    pool.value = { ...pool.value, items: next }
    // 补拉回来的可能是全平台结果，用户此刻若停在某个平台上，得重新过滤一遍
    renderView()
  } catch {
    /* 补拉失败不影响主流程，用户手动刷新同样能拿到结论 */
  }
}

async function doSearch(targetPage = 1) {
  const q = keyword.value.trim()
  if (!q) return
  if (loading.value) return

  const seq = ++searchSeq
  // 发请求这一刻把平台定死。等待期间用户可能改选 chip，
  // 返回时再读 activePlatform 会把结果记到错的平台名下——池的归属就假了。
  const reqPlatform = activePlatform.value
  clearCheckRefresh()
  loading.value = true
  error.value = ''
  searched.value = true
  page.value = targetPage
  lastKeyword.value = q

  elapsed.value = 0
  if (elapsedTimer) clearInterval(elapsedTimer)
  elapsedTimer = setInterval(() => {
    elapsed.value += 1
  }, 1000)

  try {
    const data = await searchResources({
      q,
      platforms: reqPlatform,
      page: targetPage,
      pageSize: PAGE_SIZE,
    })
    const next = data.items || []
    pool.value = {
      items: next,
      platforms: reqPlatform,
      total: data.pagination?.total || 0,
      totalPages: data.pagination?.totalPages || 0,
      tookMs: data.tookMs || 0,
    }
    items.value = next
    total.value = pool.value.total
    totalPages.value = pool.value.totalPages
    tookMs.value = pool.value.tookMs
    // 等待期间用户切到别的平台了：立刻按新的选择重算，别把全平台结果摊给他看
    if (activePlatform.value !== reqPlatform) renderView()
    syncUrl()
    if (targetPage > 1) window.scrollTo({ top: 0, behavior: 'smooth' })
    // 探活跑在响应之后（后端 BackgroundTasks），这一屏注定还没有角标。
    // 不补拉一次，用户只能靠手动刷新才看得到"可访问 / 已失效"，而多数人不会刷。
    if (targetPage === 1) {
      scheduleCheckRefresh(seq, {
        q,
        platforms: reqPlatform,
        page: targetPage,
        pageSize: PAGE_SIZE,
      })
    }
  } catch (e) {
    items.value = []
    total.value = 0
    totalPages.value = 0
    error.value = e.message || '搜索失败，请稍后重试'
  } finally {
    loading.value = false
    if (elapsedTimer) {
      clearInterval(elapsedTimer)
      elapsedTimer = null
    }
  }
}

// 手机端 chip 用短名（夸克 / 百度 / 阿里…），避免"网盘"后缀把 14 个平台
// 挤成四五行；桌面端空间够，仍显示全名。简称只改展示，不改提交给后端的 key。
function shortPlatform(name) {
  const full = (name || '').trim()
  if (!full) return ''
  // 直接按规则截断会得到"腾讯微"（腾讯微云），这类单独指定
  const manual = { 腾讯微云: '微云' }
  if (manual[full]) return manual[full]
  const stripped = full.replace(/(网盘|云盘)$/, '')
  if (stripped !== full && stripped.length >= 2) return stripped
  // 微云这类去掉"云"只剩一个字的，保留原样
  if (full.endsWith('云') && full.length > 2) return full.slice(0, -1)
  return full
}

// 偏好：只记"用户主动选了哪个平台"，下次进站默认就用它。
// 没选过（或主动点回"全部平台"）就是空 = 全平台，新用户完全不受影响。
function readPrefPlatform() {
  try {
    return localStorage.getItem(PREF_KEY) || ''
  } catch {
    return ''
  }
}

function writePrefPlatform(key) {
  try {
    if (key) localStorage.setItem(PREF_KEY, key)
    else localStorage.removeItem(PREF_KEY)
  } catch {
    /* 隐私模式下写不进去，不影响这一次筛选 */
  }
}

// 按当前选中的平台，从池里重算视图。0 请求、0 延迟。
function renderView() {
  const key = activePlatform.value
  const p = pool.value
  items.value = key && key !== p.platforms ? p.items.filter((it) => it.platform === key) : p.items
  total.value = p.total
  totalPages.value = p.totalPages
  tookMs.value = p.tookMs
}

// 池里每个平台各有几条。一次遍历算完，别让模板里 14 个 chip 各扫一遍。
// 只在"全平台查询过"之后才有意义：池本身就是单平台时，另外 13 个全是 0，看着像挂了。
const poolCounts = computed(() => {
  const m = { '': pool.value.items.length }
  for (const it of pool.value.items) m[it.platform] = (m[it.platform] || 0) + 1
  return m
})

const showPoolCounts = computed(
  () => searched.value && !loading.value && !pool.value.platforms && pool.value.items.length > 0
)

// 用户停在一个"池里没单独查过"的平台上：先给本地结果，不够再让他自己决定要不要深搜
const localOnly = computed(
  () => !!activePlatform.value && searched.value && activePlatform.value !== pool.value.platforms
)

const activeName = computed(() => {
  const p = platforms.value.find((x) => x.key === activePlatform.value)
  return p ? shortPlatform(p.name) : ''
})

function pickPlatform(key) {
  if (activePlatform.value === key) return
  activePlatform.value = key
  writePrefPlatform(key)
  // 筛选行为本身值得记：哪个平台被偏爱、筛选后还有没有人继续搜
  track('filter', {
    query: lastKeyword.value || keyword.value.trim(),
    platform: key,
  })
  // 关键：这里不再 doSearch。筛选是看池子里已经有的东西，不是重新去上游要一遍。
  if (!searched.value) return
  renderView()
  syncUrl()
}

function deepSearch() {
  if (!localOnly.value || loading.value) return
  track('filter', {
    query: lastKeyword.value,
    platform: activePlatform.value,
    meta: { deep: true },
  })
  doSearch(1)
}

// 空结果时点"搜索全部平台"：池本来就是单平台的，就必须真去查一次全平台，
// 否则只是把视图切回一个空池，用户点了像没反应。
function searchAllPlatforms() {
  const needQuery = pool.value.platforms !== ''
  pickPlatform('')
  if (needQuery) doSearch(1)
}

function goPage(p) {
  if (p < 1 || p > totalPages.value || p === page.value || loading.value) return
  track('page', {
    query: lastKeyword.value,
    platform: activePlatform.value,
    meta: { page: p },
  })
  doSearch(p)
}

function retry() {
  track('retry', { query: lastKeyword.value, platform: activePlatform.value })
  doSearch(page.value)
}

function quickSearch(word) {
  if (loading.value) return
  keyword.value = word
  doSearch(1)
}

function resetHome() {
  keyword.value = ''
  searched.value = false
  activePlatform.value = ''
  history.replaceState(null, '', '/')
}

onMounted(async () => {
  // 埋点先于一切初始化：页面打开就算一次访问，后面接口失败也不影响计数
  initTracking()

  // 匿名票据预热：进站就静默领票，首次搜索不必等一次领票往返。
  // 领票失败不阻塞（ensureTicket 内部吞异常），交底下的搜索接口自行处理。
  ensureTicket()

  // 平台列表与群配置并行拉：原先串行（先等平台列表返回才发群配置请求），
  // 进站弹窗要白白多等一个 RTT，800ms 延迟叠上去就明显觉得"卡了一下"。
  const [platformsData, groupData] = await Promise.all([
    fetchPlatforms().catch(() => ({ platforms: [] })),
    fetchGroup().catch(() => null),
  ])
  platforms.value = platformsData.platforms || []

  // 进站就定好平台：URL 里的优先（分享链接要还原对方看到的样子），
  // 没有才用上次主动选的那个。两者都要过一遍合法性——平台下线后旧值就失效了。
  // 结果为空 = 全平台，这是第一次进站的人的默认，也是产品的定义。
  const params = new URLSearchParams(location.search)
  const validKeys = new Set(platforms.value.map((p) => p.key))
  const urlPlatform = params.get('platform') || ''
  if (urlPlatform && validKeys.has(urlPlatform)) {
    activePlatform.value = urlPlatform
  } else {
    const pref = readPrefPlatform()
    activePlatform.value = pref && validKeys.has(pref) ? pref : ''
  }

  // 群配置：拉不到就当没有，不影响主流程
  if (
    groupData &&
    groupData.enabled !== false &&
    (groupData.groups || []).some((g) => g.enabled !== false)
  ) {
    groupCfg.value = groupData
    // 拦截开关的用户侧状态：昨天勾的不算，只认同一天
    acquireMuted.value = readMuted('pt_group_mute_acquire_day')
    const popup = groupData.popup || {}
    // 进站弹窗：默认每次打开都弹；once=day 时才按天抑制。
    // 点链接的拦截（acquire）不在这里，它每次点击都会弹。
    if (popup.enter && !enterSeenRecently()) {
      // 不能用 `Number(delay) || 800`：填 0（立刻弹）会被当成空值回退成 800
      const raw = Number(popup.delay)
      const delay = Math.min(Math.max(Number.isFinite(raw) ? raw : 800, 0), 10000)
      setTimeout(() => {
        // 用户可能在这零点几秒里已经点开弹窗了，别叠两个
        if (!groupOpen.value && !fbOpen.value) openGroup('enter')
      }, delay)
    }
  }

  const q = params.get('q')
  if (q) {
    keyword.value = q
    doSearch(Number(params.get('page')) || 1)
  }
})

onBeforeUnmount(() => {
  if (elapsedTimer) clearInterval(elapsedTimer)
  clearCheckRefresh()
})
</script>

<template>
  <div class="page">
    <header class="topbar">
      <div class="wrap topbar-inner">
        <a class="brand" href="/" @click.prevent="resetHome">
          <span class="logo">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.6-3.6" />
            </svg>
          </span>
          <span class="brand-text">盘迹<em>PANTRACE</em></span>
        </a>
        <div class="topbar-right">
          <span class="status mono">
            <i class="status-dot"></i>
            {{ sourceCount ? `${sourceCount} SOURCES` : 'SYNC' }}
          </span>
        </div>
      </div>
    </header>

    <section class="hero" :class="{ compact: searched }">
      <div class="hero-deco" aria-hidden="true"></div>
      <div class="wrap hero-inner">
        <template v-if="!searched">
          <p class="eyebrow mono"><span class="tick"></span>SEARCH / 全网网盘聚合</p>
          <h1 class="title">盘迹</h1>
          <p class="subtitle">一次搜索，翻遍所有网盘</p>
        </template>

        <form class="searchbar" :class="{ loading }" @submit.prevent="doSearch(1)">
          <span class="search-icon" aria-hidden="true">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.6-3.6" />
            </svg>
          </span>
          <input
            v-model="keyword"
            type="search"
            placeholder="搜影视剧、电子书、学习资料、软件…"
            autocomplete="off"
            autocapitalize="off"
            spellcheck="false"
            enterkeyhint="search"
            maxlength="100"
            aria-label="搜索关键词"
          />
          <kbd class="kbd mono" aria-hidden="true">↵</kbd>
          <button type="submit" :disabled="loading || !keyword.trim()">
            {{ loading ? '搜索中' : '搜索' }}
          </button>
          <span v-if="loading" class="scanline" aria-hidden="true"></span>
        </form>

        <div v-if="!searched" class="hero-try">
          <span class="ht-label mono">试试</span>
          <button
            v-for="w in sampleKeywords"
            :key="w"
            type="button"
            class="ht-word"
            @click="quickSearch(w)"
          >
            {{ w }}
          </button>
        </div>

        <div v-if="!searched" class="hero-meta mono">
          <span>{{ sourceCount ? `${sourceCount} 个来源` : '来源同步中' }}</span>
          <i>/</i>
          <span>通常 1–3s</span>
          <i>/</i>
          <span>结果实时聚合</span>
        </div>
      </div>
    </section>

    <main class="wrap content">
      <!-- 没搜过就不给筛：不选等于全选，这时的 chip 只是首屏噪音。
           平台偏好在真的产生结果之后才需要被调整 -->
      <div v-if="searched" class="filters" role="group" aria-label="平台筛选">
        <button class="chip" :class="{ on: activePlatform === '' }" @click="pickPlatform('')">
          <span class="chip-full">全部平台</span>
          <span class="chip-short">全部</span>
          <span v-if="showPoolCounts" class="chip-n">{{ poolCounts[''] }}</span>
        </button>
        <button
          v-for="p in platforms"
          :key="p.key"
          class="chip"
          :class="{ on: activePlatform === p.key, dim: showPoolCounts && !poolCounts[p.key] }"
          @click="pickPlatform(p.key)"
        >
          <i class="chip-dot" :style="{ background: p.color }"></i>
          <span class="chip-full">{{ p.name }}</span>
          <span class="chip-short">{{ shortPlatform(p.name) }}</span>
          <span v-if="showPoolCounts" class="chip-n">{{ poolCounts[p.key] || 0 }}</span>
        </button>
      </div>

      <!-- 偏好带进来的平台要明说，否则用户看到结果偏少会以为站坏了 -->
      <p v-if="!searched && activePlatform" class="pref-note">
        这次只搜 <b>{{ activeName }}</b>
        <button type="button" class="link" @click="pickPlatform('')">改成全部平台</button>
      </p>

      <!-- 网站介绍：放在平台筛选下方，只在首页出现。
           它同时是给用户看的说明书，也是这个站唯一成段的可用收录内容——
           没有它，搜索引擎眼里这就是个空壳页 -->
      <section v-if="!searched" class="intro">
        <div class="intro-grid">
          <div class="intro-block">
            <h2 class="intro-h mono">这是什么</h2>
            <p>
              盘迹（PanTrace）是一个网盘资源聚合搜索引擎。夸克、百度、阿里、迅雷、微云等各家网盘的公开分享散落在不同的角落，盘迹把它们收进同一个搜索框——输入一次，全部检索。
            </p>
          </div>
          <div class="intro-block">
            <h2 class="intro-h mono">能搜什么</h2>
            <p>
              影视剧、综艺、动漫、纪录片、电子书、学习资料、软件安装包。只要有人在网盘里分享过，这里就有机会一次翻到，不用再挨个平台碰运气。
            </p>
          </div>
          <div class="intro-block">
            <h2 class="intro-h mono">怎么用</h2>
            <p>
              输入关键词，按平台筛选结果，点开链接即可转存。所有资源均来自第三方公开分享，本站不存储任何文件；失效链接可一键报错，被多人报错后会自动隐藏。
            </p>
          </div>
        </div>
      </section>

      <div v-if="loading" class="scan-state">
        <div class="scan-head">
          <span class="scan-label mono">SCANNING</span>
          <span class="scan-count mono" v-if="elapsed">{{ elapsed }}s</span>
        </div>
        <p class="loading-hint">正在聚合各平台结果，通常 1–3 秒。</p>
        <div class="scan-track" aria-hidden="true"><i></i></div>
        <div class="skeleton-list">
          <div v-for="i in 4" :key="i" class="skeleton">
            <div class="sk-line w60"></div>
            <div class="sk-line w30"></div>
          </div>
        </div>
      </div>

      <div v-else-if="error" class="state error-state">
        <div class="state-icon">!</div>
        <p class="state-title">这次没搜成</p>
        <p class="state-desc">{{ error }}</p>
        <div class="state-actions">
          <button class="retry" @click="retry">重试一次</button>
          <button class="link" @click="openFeedback('bug', { error })">上报这个错误</button>
        </div>
      </div>

      <!-- 池里没有这个平台的东西：不是"搜不到"，是"这次没单独查它"。
           这两件事在用户眼里差别巨大，说清楚，再让他决定要不要花一次查询。 -->
      <div v-else-if="localOnly && !items.length" class="state">
        <div class="state-icon empty">∅</div>
        <p class="state-title">本次结果里没有{{ activeName }}的内容</p>
        <p class="state-desc">
          上面那次是跨平台聚合，这个平台返回的条目不在里面。单独查它要再走一次上游。
        </p>
        <div class="state-actions">
          <button class="retry" @click="deepSearch">只在{{ activeName }}内搜索</button>
          <button class="link" @click="pickPlatform('')">先看全部平台</button>
        </div>
      </div>

      <div v-else-if="showEmpty" class="state">
        <div class="state-icon empty">∅</div>
        <p class="state-title">没有找到「{{ lastKeyword }}」</p>
        <p class="state-desc">换个关键词试试，或者先取消平台筛选看看全部来源。</p>
        <button v-if="activePlatform" class="retry" @click="searchAllPlatforms">搜索全部平台</button>
        <p class="state-more">
          都没有？
          <button class="link" @click="openFeedback('request')">告诉我你要找什么</button>
        </p>
      </div>

      <template v-else-if="items.length">
        <div class="result-head mono">
          <span class="rh-label">RESULT</span>
          <template v-if="localOnly">
            <span class="rh-num">{{ items.length }}</span>
            <span class="rh-sep">/</span>
            <span>本页内筛选 · {{ activeName }}</span>
          </template>
          <template v-else>
            <span class="rh-num">{{ totalText }}</span>
            <span class="rh-sep">/</span>
            <span>{{ tookMs }}ms</span>
            <span class="rh-sep">/</span>
            <span>第 {{ page }} 页 · 共 {{ totalPages }} 页</span>
          </template>
        </div>

        <!-- 时效说明整页只出现一次：贴着结果头，信息完整但不刷屏；
             每张卡片内的「30 分钟内有效」徽标负责就近提醒 -->
        <p class="ttl-note">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 7.5V12l3 2" />
          </svg>
          <span>网盘链接有效期为 <b>30 分钟</b>，请及时转存；失效后可重新获取。</span>
        </p>

        <div class="result-list">
          <ResultCard
            v-for="item in items"
            :key="item.hsid || item.shareUrl"
            :item="item"
            :keyword="lastKeyword"
            :intercept="groupIntercept"
            @feedback="openFeedback('result', { item: $event })"
            @open="handleOpen"
          />
        </div>

        <!-- 本地筛选只覆盖本次聚合拿到的这些条目，说清楚，再给一个显式的深搜入口。
             这里必须由用户点：一次深搜 = 一次上游查询，不能替他自动发。 -->
        <div v-if="localOnly" class="deep-cta">
          <p>
            以上是本次聚合结果里的 <b>{{ items.length }}</b> 条{{ activeName }}内容。
            只想要这个平台的话，可以再单独查一次。
          </p>
          <button type="button" class="deep-btn" :disabled="loading" @click="deepSearch">
            只在{{ activeName }}内搜索
          </button>
        </div>

        <div v-if="totalPages > 1 && !localOnly" class="pager">
          <button class="pg" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
          <button
            v-for="p in pageWindow"
            :key="p"
            class="pg num"
            :class="{ on: p === page }"
            @click="goPage(p)"
          >
            {{ p }}
          </button>
          <span class="pg-info mono">{{ page }} / {{ totalPages }}</span>
          <button class="pg" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</button>
        </div>
      </template>
    </main>

    <footer class="footer">
      <div class="wrap footer-inner">
        <span class="footer-brand mono">PANTRACE · 资源均来自第三方分享，本站不存储任何文件</span>
        <span class="footer-links">
          <button class="link" @click="showGroupManual">进群交流</button>
          <button class="link" @click="openFeedback('request')">求资源</button>
          <button class="link" @click="openFeedback('bug')">反馈问题</button>
        </span>
      </div>
    </footer>

    <FeedbackDialog
      :open="fbOpen"
      :mode="fbMode"
      :item="fbItem"
      :keyword="lastKeyword || keyword"
      :error-text="fbError"
      @close="closeFeedback"
    />

    <QqGroupDialog
      :open="groupOpen"
      :mode="groupMode"
      :config="groupCfg"
      @close="closeGroup"
      @continue="groupContinue"
      @mute="muteGroupPopup"
    />
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  min-height: 100dvh;
}

.wrap {
  width: 100%;
  max-width: 960px;
  margin: 0 auto;
  padding: 0 24px;
}

/* ---------- 顶栏 ---------- */
.topbar {
  position: sticky;
  top: 0;
  z-index: 30;
  background: rgba(245, 247, 249, 0.88);
  backdrop-filter: saturate(150%) blur(14px);
  -webkit-backdrop-filter: saturate(150%) blur(14px);
  border-bottom: 1px solid var(--line);
}

.topbar-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: 60px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.logo {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  border-radius: var(--radius-sm);
  background: var(--ink);
  color: #fff;
}

.brand-text {
  font-size: 15px;
  font-weight: 650;
  letter-spacing: 0.08em;
  white-space: nowrap;
}

.brand-text em {
  font-family: var(--mono);
  font-style: normal;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.16em;
  color: var(--faint);
  margin-left: 9px;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 10px;
  letter-spacing: 0.14em;
  color: var(--muted);
  white-space: nowrap;
}

.status-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 3px rgba(0, 169, 157, 0.14);
  animation: pulse 2.6s ease-in-out infinite;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}

/* ---------- 首屏 ---------- */
.hero {
  position: relative;
  isolation: isolate;
  padding: 88px 0 48px;
  background: linear-gradient(180deg, #fbfcfd 0%, var(--bg) 100%);
  border-bottom: 1px solid var(--line);
}

/* 网格刻度：只在首屏出现，向下淡出，像是仪器的底板 */
.hero-deco {
  position: absolute;
  inset: 0;
  z-index: -1;
  background-image:
    linear-gradient(to right, rgba(11, 16, 23, 0.045) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(11, 16, 23, 0.045) 1px, transparent 1px);
  background-size: 56px 56px;
  -webkit-mask-image: radial-gradient(72% 94% at 50% 0%, #000 0%, transparent 76%);
  mask-image: radial-gradient(72% 94% at 50% 0%, #000 0%, transparent 76%);
}

.hero-inner {
  text-align: center;
}

.eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 18px;
  font-size: 10px;
  letter-spacing: 0.18em;
  color: var(--faint);
  text-transform: uppercase;
}

.eyebrow .tick {
  width: 22px;
  height: 1px;
  background: var(--line-2);
}

.title {
  margin: 0 0 14px;
  font-size: clamp(38px, 6vw, 52px);
  line-height: 1.1;
  font-weight: 600;
  letter-spacing: 0.2em;
  text-indent: 0.2em; /* 抵消字距在右侧多出的空隙，视觉居中 */
  color: var(--ink);
}

.subtitle {
  margin: 0 auto 38px;
  max-width: 520px;
  font-size: 14.5px;
  font-weight: 400;
  letter-spacing: 0.06em;
  color: var(--muted);
}

/* 搜索条 */
.searchbar {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 640px;
  margin: 0 auto;
  padding: 6px 6px 6px 14px;
  background: var(--surface);
  border: 1px solid var(--line-2);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}

/* 四个角的定位标记：仪器取景框那味道，只在聚焦时亮 */
.searchbar::before,
.searchbar::after {
  content: '';
  position: absolute;
  width: 7px;
  height: 7px;
  border: 1px solid var(--accent);
  opacity: 0;
  transition: opacity 0.2s ease;
}

.searchbar::before {
  top: -4px;
  left: -4px;
  border-right: 0;
  border-bottom: 0;
}

.searchbar::after {
  right: -4px;
  bottom: -4px;
  border-left: 0;
  border-top: 0;
}

.searchbar:focus-within {
  border-color: var(--ink-2);
  box-shadow: 0 0 0 3px rgba(0, 169, 157, 0.1), var(--shadow-md);
}

.searchbar:focus-within::before,
.searchbar:focus-within::after {
  opacity: 1;
}

.search-icon {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  color: var(--faint);
}

.searchbar input {
  flex: 1;
  min-width: 0;
  padding: 9px 0;
  border: none;
  outline: none;
  background: transparent;
  font-size: 15px;
  color: var(--ink);
}

.searchbar input::placeholder {
  color: var(--faint);
}

.searchbar input::-webkit-search-cancel-button {
  cursor: pointer;
}

.kbd {
  flex-shrink: 0;
  padding: 1px 5px;
  font-size: 10px;
  color: var(--faint);
  border: 1px solid var(--line);
  border-radius: 4px;
  line-height: 1.5;
}

.searchbar button {
  flex-shrink: 0;
  height: 38px;
  padding: 0 22px;
  border: none;
  border-radius: var(--radius-sm);
  background: var(--ink);
  color: #fff;
  font-size: 13.5px;
  font-weight: 500;
  letter-spacing: 0.08em;
  transition: background 0.16s ease, opacity 0.16s ease;
}

.searchbar button:hover:not(:disabled) {
  background: #1b2430;
}

.searchbar button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 搜索时沿下沿走的一道光，比转圈更安静 */
.scanline {
  position: absolute;
  left: 8px;
  bottom: -1px;
  width: 34%;
  height: 1.5px;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: scan 1.5s linear infinite;
}

@keyframes scan {
  0% {
    transform: translateX(-30%);
    opacity: 0;
  }
  20%,
  80% {
    opacity: 1;
  }
  100% {
    transform: translateX(240%);
    opacity: 0;
  }
}

.hero-meta {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  flex-wrap: wrap;
  margin-top: 18px;
  font-size: 10px;
  letter-spacing: 0.12em;
  color: var(--faint);
}

.hero-meta i {
  font-style: normal;
  color: var(--line-2);
}

/* 快捷搜索词：点一次直接出结果，省掉输入这步 */
.hero-try {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 14px;
}

.ht-label {
  font-size: 10px;
  letter-spacing: 0.16em;
  color: var(--faint);
}

.ht-word {
  padding: 0;
  border: none;
  background: none;
  font-size: 12.5px;
  color: var(--muted);
  border-bottom: 1px dashed var(--line-2);
  line-height: 1.6;
  transition: color 0.16s ease, border-color 0.16s ease;
}

.ht-word:hover {
  color: var(--accent);
  border-color: var(--accent);
}

/* ---------- 首页介绍区 ---------- */
.intro {
  margin-top: 4px;
  padding: 20px 0 6px;
  border-top: 1px solid var(--line);
}

.intro-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 30px;
}

.intro-h {
  margin: 0 0 9px;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.18em;
  color: var(--faint);
}

.intro-block p {
  margin: 0;
  font-size: 13px;
  line-height: 1.78;
  color: var(--muted);
  text-align: justify;
}

/* 已搜索：搜索条吸顶，翻长列表随时能改词 */
.hero.compact {
  position: sticky;
  top: 60px;
  z-index: 20;
  padding: 10px 0;
  background: rgba(245, 247, 249, 0.92);
  backdrop-filter: saturate(150%) blur(14px);
  -webkit-backdrop-filter: saturate(150%) blur(14px);
}

.hero.compact .hero-deco {
  display: none;
}

.hero.compact .searchbar {
  max-width: none;
}

/* ---------- 内容区 ---------- */
.content {
  flex: 1;
  padding-bottom: 64px;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  padding: 18px 0 20px;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 11px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink-2);
  font-size: 12.5px;
  letter-spacing: 0.01em;
  white-space: nowrap;
  transition: border-color 0.16s ease, background 0.16s ease, color 0.16s ease;
}

.chip:hover {
  border-color: var(--line-2);
  background: var(--surface-2);
}

.chip.on {
  background: var(--ink);
  border-color: var(--ink);
  color: #fff;
}

.chip-dot {
  width: 5px;
  height: 5px;
  border-radius: 1px;
}

/* 池里的命中数：筛选从"装饰"变成"信息"。数字用 mono，视觉上比平台名更弱一档 */
.chip-n {
  font-family: var(--mono);
  font-size: 10.5px;
  font-weight: 500;
  color: var(--faint);
  transform: translateY(0.5px);
}

.chip.on .chip-n {
  color: rgba(255, 255, 255, 0.62);
}

/* 全平台查询过、但这家一条没有：留着可见（说明真的搜了），只是退到背景里 */
.chip.dim {
  opacity: 0.45;
}

.chip.dim:hover {
  opacity: 1;
}

/* 从偏好带进来的平台，进站就说清楚，别让用户以为结果漏了 */
.pref-note {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: -8px 0 16px;
  font-size: 11.5px;
  color: var(--muted);
}

.pref-note b {
  color: var(--ink);
  font-weight: 600;
}

/* 本地筛选的出口：明说只有这么多，再把"要不要花一次查询"交给用户 */
.deep-cta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 14px;
  padding: 12px 14px;
  border: 1px dashed var(--line-2);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
}

.deep-cta p {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--muted);
}

.deep-cta b {
  color: var(--ink);
  font-weight: 600;
}

.deep-btn {
  flex-shrink: 0;
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--line-2);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink);
  font-size: 12px;
  white-space: nowrap;
  transition: border-color 0.16s ease, background 0.16s ease;
}

.deep-btn:hover:not(:disabled) {
  border-color: var(--ink);
}

.deep-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

/* 手机端上下排，按钮占满，别和文字挤在一行 */
@media (max-width: 640px) {
  .deep-cta {
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }

  .deep-btn {
    width: 100%;
  }
}

/* 手机端才显示简称，桌面端沿用全名 */
.chip-short {
  display: none;
}

/* ---------- 结果头 ---------- */
.result-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 0 14px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
  font-size: 10.5px;
  letter-spacing: 0.08em;
  color: var(--muted);
}

.rh-label {
  color: var(--faint);
  letter-spacing: 0.16em;
}

.rh-num {
  color: var(--ink);
  font-weight: 600;
}

.rh-sep {
  color: var(--line-2);
}

/* 一次性时效说明：弱化正文色，重点数字用暖色 */
.ttl-note {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: -4px 0 12px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--muted);
}

.ttl-note svg {
  flex-shrink: 0;
  color: var(--warn);
  opacity: 0.85;
}

.ttl-note b {
  font-weight: 600;
  color: var(--warn);
}

.result-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* ---------- 加载态 ---------- */
.scan-state {
  padding-top: 4px;
}

.scan-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.scan-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 10px;
  letter-spacing: 0.2em;
  color: var(--accent-deep);
}

.scan-label::before {
  content: '';
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  animation: pulse 1.4s ease-in-out infinite;
}

.scan-count {
  font-size: 10.5px;
  color: var(--faint);
}

.loading-hint {
  margin: 0 0 12px;
  font-size: 12.5px;
  color: var(--muted);
}

.scan-track {
  position: relative;
  height: 1px;
  margin-bottom: 18px;
  background: var(--line);
  overflow: hidden;
}

.scan-track i {
  position: absolute;
  top: 0;
  left: 0;
  width: 28%;
  height: 100%;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: scan 1.6s ease-in-out infinite;
}

.skeleton-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.skeleton {
  padding: 18px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
}

.sk-line {
  height: 11px;
  border-radius: 2px;
  background: linear-gradient(90deg, #eef1f5 25%, #f7f9fb 37%, #eef1f5 63%);
  background-size: 400% 100%;
  animation: shimmer 1.4s ease-in-out infinite;
}

.sk-line + .sk-line {
  margin-top: 11px;
}

.w60 {
  width: 60%;
}

.w30 {
  width: 26%;
}

@keyframes shimmer {
  0% {
    background-position: 100% 50%;
  }
  100% {
    background-position: 0 50%;
  }
}

/* ---------- 空 / 错误 ---------- */
.state {
  padding: 64px 20px;
  text-align: center;
}

.state-icon {
  display: inline-grid;
  place-items: center;
  width: 42px;
  height: 42px;
  margin-bottom: 16px;
  border: 1px solid var(--line-2);
  border-radius: 50%;
  background: var(--surface);
  color: var(--muted);
  font-family: var(--mono);
  font-size: 18px;
  font-weight: 600;
}

.state-icon.empty {
  font-weight: 400;
  color: var(--faint);
}

.error-state .state-icon {
  border-color: #efd2cd;
  background: var(--danger-soft);
  color: var(--danger);
}

.state-title {
  margin: 0 0 6px;
  font-size: 15px;
  font-weight: 600;
  color: var(--ink);
}

.state-desc {
  margin: 0 auto;
  max-width: 420px;
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--muted);
}

.retry {
  height: 34px;
  margin-top: 18px;
  padding: 0 16px;
  border: 1px solid var(--line-2);
  border-radius: var(--radius-sm);
  background: var(--surface);
  font-size: 13px;
  color: var(--ink-2);
  transition: border-color 0.16s ease, color 0.16s ease;
}

.retry:hover {
  border-color: var(--ink-2);
  color: var(--ink);
}

.state-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 18px;
}

.state-actions .retry {
  margin-top: 0;
}

.state-more {
  margin: 16px 0 0;
  font-size: 13px;
  color: var(--muted);
}

.link {
  padding: 0;
  border: none;
  background: none;
  font-size: inherit;
  color: var(--accent-deep);
  text-decoration: underline;
  text-decoration-color: rgba(0, 169, 157, 0.35);
  text-underline-offset: 3px;
}

.link:hover {
  color: var(--ink);
  text-decoration-color: var(--ink);
}

/* ---------- 分页 ---------- */
.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  flex-wrap: wrap;
  margin-top: 28px;
}

.pg {
  min-width: 34px;
  height: 34px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink-2);
  font-size: 12.5px;
  transition: border-color 0.16s ease, background 0.16s ease, color 0.16s ease;
}

.pg.num {
  font-family: var(--mono);
  font-size: 12px;
}

.pg:hover:not(:disabled):not(.on) {
  border-color: var(--line-2);
}

.pg.on {
  background: var(--ink);
  border-color: var(--ink);
  color: #fff;
}

.pg:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.pg-info {
  display: none;
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--muted);
}

/* ---------- 页脚 ---------- */
.footer {
  padding: 22px 0 28px;
  border-top: 1px solid var(--line);
  background: var(--surface-2);
}

.footer-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.footer-brand {
  font-size: 10px;
  letter-spacing: 0.1em;
  color: var(--faint);
}

.footer-links {
  display: inline-flex;
  align-items: center;
  gap: 16px;
  font-size: 12px;
}

/* ---------- 平板 ---------- */
@media (max-width: 760px) {
  .wrap {
    padding: 0 16px;
  }

  .topbar-inner {
    height: 54px;
  }

  .brand-text em {
    display: none;
  }

  .hero {
    padding: 52px 0 32px;
  }

  .title {
    font-size: 34px;
    letter-spacing: 0.16em;
    text-indent: 0.16em;
  }

  .subtitle {
    margin-bottom: 28px;
    font-size: 13.5px;
  }

  .eyebrow {
    margin-bottom: 14px;
    font-size: 9.5px;
  }

  /* iOS 输入框 <16px 会触发自动缩放，这条不能省 */
  .searchbar input {
    font-size: 16px;
    padding: 10px 0;
  }

  .kbd {
    display: none;
  }

  .searchbar button {
    height: 40px;
    padding: 0 18px;
  }

  .hero-meta {
    margin-top: 14px;
    font-size: 9.5px;
    gap: 7px;
  }

  .hero-try {
    margin-top: 12px;
    gap: 10px;
  }

  .ht-word {
    font-size: 12px;
  }

  .intro {
    padding: 14px 0 4px;
  }

  .intro-grid {
    grid-template-columns: 1fr;
    gap: 18px;
  }

  .intro-block p {
    font-size: 12.5px;
  }

  .hero.compact {
    top: 54px;
    padding: 8px 0;
  }

  /* 手机端品牌栏不吸顶（滚走），只留搜索条贴顶——
     双行吸顶要吃掉 130px+，长列表可视区太小 */
  .topbar {
    position: static;
  }

  .hero.compact {
    top: 0;
    border-bottom: 1px solid var(--line);
  }

  .filters {
    flex-wrap: wrap;
    margin: 0;
    padding: 12px 0 14px;
    gap: 6px;
  }

  .chip {
    flex: 0 0 auto;
    height: 30px;
    padding: 0 9px;
    gap: 5px;
    font-size: 12px;
  }

  .chip-full {
    display: none;
  }

  .chip-short {
    display: inline;
  }

  .content {
    padding-bottom: 48px;
  }

  .state {
    padding: 48px 8px;
  }

  .result-head {
    font-size: 10px;
    gap: 6px;
  }

  /* 手机分页只留前后翻 + 页码读数，数字按钮排开太挤 */
  .pg.num {
    display: none;
  }

  .pg-info {
    display: inline;
    margin: 0 4px;
  }

  .pager {
    gap: 8px;
  }

  .footer-inner {
    flex-direction: column;
    align-items: flex-start;
    gap: 14px;
  }

  .footer-links {
    gap: 20px;
    font-size: 13px;
  }
}

@media (max-width: 380px) {
  .title {
    font-size: 30px;
  }

  .searchbar button {
    padding: 0 14px;
  }
}
</style>
