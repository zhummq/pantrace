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

// ---- 平台偏好 ----
// 只记"用户主动选了哪个平台"，下次进站默认就查它。
// 没选过（或点回"全部平台"）就是空 = 全平台，新用户完全不受影响。
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
    items.value = next
  } catch {
    /* 补拉失败不影响主流程，用户手动刷新同样能拿到结论 */
  }
}

async function doSearch(targetPage = 1) {
  const q = keyword.value.trim()
  if (!q) return
  if (loading.value) return

  const seq = ++searchSeq
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
      platforms: activePlatform.value,
      page: targetPage,
      pageSize: PAGE_SIZE,
    })
    items.value = data.items || []
    total.value = data.pagination?.total || 0
    totalPages.value = data.pagination?.totalPages || 0
    tookMs.value = data.tookMs || 0
    syncUrl()
    if (targetPage > 1) window.scrollTo({ top: 0, behavior: 'smooth' })
    // 探活跑在响应之后（后端 BackgroundTasks），这一屏注定还没有角标。
    // 不补拉一次，用户只能靠手动刷新才看得到"可访问 / 已失效"，而多数人不会刷。
    if (targetPage === 1) {
      scheduleCheckRefresh(seq, {
        q,
        platforms: activePlatform.value,
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

// 按当前选中的平台重新查一次上游：单平台查询拿到的是这个平台自己的完整排序，
// 比"从混排的 20 条里挑出属于它的几条"准得多，这是用户点 chip 期待的东西。
const activeName = computed(() => {
  const p = platforms.value.find((x) => x.key === activePlatform.value)
  return p ? shortPlatform(p.name) : ''
})

function pickPlatform(key) {
  if (activePlatform.value === key) return
  // 正在查询时不给切：否则要么结果和选中的 chip 对不上，
  // 要么并发两个请求互相覆盖。一次查询只有一两秒，等它落地再切。
  if (loading.value) return
  activePlatform.value = key
  writePrefPlatform(key)
  // 筛选行为本身值得记：哪个平台被偏爱、筛选后还有没有人继续搜
  track('filter', {
    query: lastKeyword.value || keyword.value.trim(),
    platform: key,
  })
  // 跟以前一样：点平台就是一次真实查询，不是在同一批结果里挑。
  if (searched.value) doSearch(1)
}

// 空结果时点"搜索全部平台"：换平台本身就会重新查，不需要再补一次
function searchAllPlatforms() {
  pickPlatform('')
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
      <div class="wrap hero-inner">
        <template v-if="!searched">
          <p class="eyebrow"><span class="eb-dot"></span>全网网盘聚合搜索</p>
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
            placeholder="搜影视、电子书、学习资料…"
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
        </button>
        <button
          v-for="p in platforms"
          :key="p.key"
          class="chip"
          :class="{ on: activePlatform === p.key }"
          @click="pickPlatform(p.key)"
        >
          <i class="chip-dot" :style="{ background: p.color }"></i>
          <span class="chip-full">{{ p.name }}</span>
          <span class="chip-short">{{ shortPlatform(p.name) }}</span>
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
          <span class="rh-num">{{ totalText }}</span>
          <span class="rh-sep">/</span>
          <span>{{ tookMs }}ms</span>
          <span class="rh-sep">/</span>
          <span>第 {{ page }} 页 · 共 {{ totalPages }} 页</span>
        </div>

        <!-- 时效说明整页只出现一次：贴着结果头，信息完整但不刷屏；
             每张卡片内的「30 分钟内有效」徽标负责就近提醒 -->
        <p class="ttl-note">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 7.5V12l3 2" />
          </svg>
          <span>网盘链接 <b>30 分钟</b>内有效，过期可重新获取。</span>
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

        <div v-if="totalPages > 1" class="pager">
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
  max-width: var(--shell-max);
  margin: 0 auto;
  padding: 0 var(--sp-6);
}

/* ---------- 顶栏 ---------- */
.topbar {
  position: sticky;
  top: 0;
  z-index: 30;
  background: var(--surface-glass);
  backdrop-filter: saturate(180%) blur(14px);
  -webkit-backdrop-filter: saturate(180%) blur(14px);
  border-bottom: 1px solid var(--line);
}

.topbar-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  height: var(--nav-h);
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
  width: 30px;
  height: 30px;
  flex-shrink: 0;
  border-radius: 9px;
  background: var(--brand);
  color: var(--on-brand);
  box-shadow: var(--shadow-brand);
}

.brand-text {
  font-size: 15.5px;
  font-weight: 600;
  letter-spacing: 0.01em;
  white-space: nowrap;
  color: var(--ink);
  transition: color var(--dur-1) var(--ease);
}

.brand:hover .brand-text {
  color: var(--brand-ink);
}

.brand-text em {
  font-family: var(--mono);
  font-style: normal;
  font-size: var(--text-2xs);
  font-weight: 500;
  letter-spacing: 0.12em;
  color: var(--faint);
  margin-left: 8px;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-pill);
  background: var(--surface);
  font-size: var(--text-xs);
  letter-spacing: 0.02em;
  color: var(--muted);
  white-space: nowrap;
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--cyan-500);
  box-shadow: 0 0 0 3px var(--cyan-ring);
  animation: pulse 2.4s ease-in-out infinite;
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}

/* ---------- 首屏 ---------- */
.hero {
  position: relative;
  padding: 76px 0 44px;
  /* 一层极淡的顶部光晕：给首屏一点纵深，不参与信息表达 */
  background:
    radial-gradient(920px 340px at 50% -140px, var(--hero-glow), transparent 72%),
    var(--bg);
  border-bottom: 1px solid var(--line);
}

.hero-inner {
  text-align: center;
}

.eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 var(--sp-5);
  padding: 4px 12px 4px 10px;
  border: 1px solid var(--brand-100);
  border-radius: var(--radius-pill);
  background: var(--brand-25);
  font-size: var(--text-xs);
  font-weight: 500;
  letter-spacing: 0.02em;
  color: var(--brand-ink);
}

.eb-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-ring);
}

.title {
  margin: 0 0 var(--sp-3);
  font-size: clamp(34px, 5.2vw, 46px);
  line-height: 1.12;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--ink);
}

.subtitle {
  margin: 0 auto var(--sp-7);
  max-width: 520px;
  font-size: var(--text-md);
  line-height: 1.7;
  color: var(--muted);
}

/* ---------- 搜索条 ---------- */
.searchbar {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  max-width: 620px;
  margin: 0 auto;
  padding: 7px 7px 7px var(--sp-4);
  background: var(--surface);
  border: 1px solid var(--line-2);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  transition:
    border-color var(--dur-2) var(--ease),
    box-shadow var(--dur-2) var(--ease);
}

.searchbar:focus-within {
  border-color: var(--brand-400);
  box-shadow: var(--ring), var(--shadow-md);
}

.search-icon {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  color: var(--faint);
}

.searchbar:focus-within .search-icon {
  color: var(--brand);
}

.searchbar input {
  flex: 1;
  min-width: 0;
  padding: 10px 0;
  border: none;
  outline: none;
  background: transparent;
  font-size: var(--text-md);
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
  padding: 2px 6px;
  font-size: var(--text-2xs);
  line-height: 1.4;
  color: var(--faint);
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--surface-3);
}

.searchbar button {
  flex-shrink: 0;
  height: 40px;
  padding: 0 var(--sp-6);
  border: none;
  border-radius: var(--radius);
  background: var(--brand);
  color: var(--on-brand);
  font-size: var(--text-base);
  font-weight: 600;
  letter-spacing: 0.02em;
  box-shadow: var(--shadow-brand);
}

.searchbar button:hover:not(:disabled) {
  background: var(--brand-hover);
}

.searchbar button:active:not(:disabled) {
  background: var(--brand-active);
}

.searchbar button:disabled {
  background: var(--surface-3);
  color: var(--muted);
  box-shadow: none;
  transform: none;
}

/* 搜索中的进度条：安静的一条，不抢注意力 */
.scanline {
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: -1px;
  height: 2px;
  overflow: hidden;
  border-radius: 2px;
  background: var(--brand-50);
}

.scanline::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 36%;
  height: 100%;
  border-radius: 2px;
  background: linear-gradient(90deg, transparent, var(--brand), transparent);
  animation: scan 1.3s ease-in-out infinite;
}

@keyframes scan {
  0% {
    transform: translateX(-100%);
    opacity: 0;
  }
  25%,
  75% {
    opacity: 1;
  }
  100% {
    transform: translateX(280%);
    opacity: 0;
  }
}

.hero-meta {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: var(--sp-4);
  font-size: var(--text-xs);
  color: var(--faint);
}

/* 分隔符本身不用「/」——那个字符太小，在弱色下像渲染残渣 */
.hero-meta i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--line-2);
  font-size: 0;
  line-height: 0;
}

/* ---------- 快捷搜索词 ---------- */
.hero-try {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-top: var(--sp-4);
}

.ht-label {
  font-size: var(--text-xs);
  color: var(--faint);
  margin-right: 2px;
}

.ht-word {
  height: 30px;
  padding: 0 var(--sp-3);
  border: 1px solid var(--line);
  border-radius: var(--radius-pill);
  background: var(--surface);
  font-size: var(--text-sm);
  color: var(--ink-2);
  box-shadow: var(--shadow-xs);
}

.ht-word:hover {
  border-color: var(--brand-200);
  background: var(--brand-25);
  color: var(--brand-ink);
  transform: translateY(-1px);
}

/* ---------- 首页介绍区 ---------- */
.intro {
  margin-top: var(--sp-6);
  padding-top: var(--sp-6);
  border-top: 1px solid var(--line);
}

.intro-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--sp-7);
}

.intro-h {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 var(--sp-2);
  font-family: var(--sans);
  font-size: var(--text-md);
  font-weight: 600;
  letter-spacing: 0;
  color: var(--ink);
}

.intro-h::before {
  content: '';
  width: 3px;
  height: 12px;
  border-radius: 2px;
  background: var(--brand);
}

.intro-block p {
  margin: 0;
  font-size: var(--text-base);
  line-height: 1.75;
  color: var(--muted);
}

/* 已搜索：搜索条吸顶，翻长列表随时能改词 */
.hero.compact {
  position: sticky;
  top: var(--nav-h);
  z-index: 20;
  padding: var(--sp-2) 0;
  background: rgba(255, 255, 255, 0.9);
  backdrop-filter: saturate(180%) blur(14px);
  -webkit-backdrop-filter: saturate(180%) blur(14px);
}

.hero.compact .searchbar {
  max-width: none;
}

/* ---------- 内容区 ---------- */
.content {
  flex: 1;
  padding-bottom: var(--sp-9);
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  padding: var(--sp-5) 0;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-pill);
  background: var(--surface);
  color: var(--ink-2);
  font-size: var(--text-sm);
  white-space: nowrap;
  box-shadow: var(--shadow-xs);
  transition:
    background-color var(--dur-1) var(--ease),
    border-color var(--dur-1) var(--ease),
    color var(--dur-1) var(--ease),
    transform var(--dur-1) var(--ease),
    box-shadow var(--dur-2) var(--ease);
}

.chip:hover:not(.on) {
  border-color: var(--brand-200);
  background: var(--brand-25);
  color: var(--brand-ink);
  transform: translateY(-1px);
}

.chip:active {
  transform: translateY(0);
}

.chip.on {
  background: var(--brand-soft);
  border-color: var(--brand-300);
  color: var(--brand-ink);
  font-weight: 600;
  box-shadow: none;
}

.chip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

/* 从偏好带进来的平台，进站就说清楚，别让用户以为结果漏了 */
.pref-note {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: -4px 0 var(--sp-4);
  font-size: var(--text-sm);
  color: var(--muted);
}

.pref-note b {
  color: var(--ink);
  font-weight: 600;
}

/* 手机端才显示简称，桌面端沿用全名 */
.chip-short {
  display: none;
}

/* ---------- 结果头 ---------- */
.result-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin: 0 0 var(--sp-3);
  padding-bottom: var(--sp-3);
  border-bottom: 1px solid var(--line);
  font-size: var(--text-sm);
  color: var(--muted);
}

.rh-label {
  padding: 2px 8px;
  border-radius: var(--radius-xs);
  background: var(--surface-3);
  font-size: var(--text-2xs);
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--muted);
}

.rh-num {
  color: var(--ink);
  font-weight: 600;
}

.rh-sep {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--line-2);
  font-size: 0;
}

/* 一次性时效说明：弱化正文色，重点数字用暖色 */
.ttl-note {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0 0 var(--sp-3);
  font-size: var(--text-sm);
  line-height: 1.5;
  color: var(--muted);
  /* 窄屏折行时把词均分到两行，避免最后一行只剩一两个字 */
  text-wrap: pretty;
}

.ttl-note svg {
  flex-shrink: 0;
  margin-top: 2px;
  color: var(--warn);
}

.ttl-note b {
  font-weight: 600;
  color: var(--warn);
}

.result-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

/* ---------- 加载态 ---------- */
.scan-state {
  padding-top: var(--sp-1);
}

.scan-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
}

.scan-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--brand-ink);
}

.scan-label::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--brand);
  animation: pulse 1.4s ease-in-out infinite;
}

.scan-count {
  font-size: var(--text-xs);
  color: var(--faint);
}

.loading-hint {
  margin: 0 0 var(--sp-3);
  font-size: var(--text-sm);
  color: var(--muted);
}

.scan-track {
  position: relative;
  height: 3px;
  margin-bottom: var(--sp-5);
  border-radius: 2px;
  background: var(--surface-3);
  overflow: hidden;
}

.scan-track i {
  position: absolute;
  top: 0;
  left: 0;
  width: 30%;
  height: 100%;
  border-radius: 2px;
  background: linear-gradient(90deg, transparent, var(--brand), transparent);
  animation: scan 1.5s ease-in-out infinite;
}

.skeleton-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.skeleton {
  padding: 18px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-xs);
}

.sk-line {
  height: 11px;
  border-radius: var(--radius-xs);
  background: linear-gradient(90deg, var(--surface-3) 25%, var(--slate-25) 37%, var(--surface-3) 63%);
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
  padding: var(--sp-9) var(--sp-5);
  text-align: center;
}

.state-icon {
  display: inline-grid;
  place-items: center;
  width: 48px;
  height: 48px;
  margin-bottom: var(--sp-4);
  border-radius: var(--radius-lg);
  background: var(--surface-3);
  color: var(--muted);
  font-family: var(--mono);
  font-size: var(--text-lg);
  font-weight: 600;
}

.state-icon.empty {
  font-weight: 400;
  color: var(--faint);
}

.error-state .state-icon {
  background: var(--danger-soft);
  color: var(--danger);
}

.state-title {
  margin: 0 0 6px;
  font-size: var(--text-lg);
  font-weight: 600;
  color: var(--ink);
}

.state-desc {
  margin: 0 auto;
  max-width: 440px;
  font-size: var(--text-base);
  line-height: 1.65;
  color: var(--muted);
}

.retry {
  height: 38px;
  margin-top: var(--sp-4);
  padding: 0 var(--sp-5);
  border: 1px solid var(--line-2);
  border-radius: var(--radius);
  background: var(--surface);
  font-size: var(--text-base);
  font-weight: 500;
  color: var(--ink-2);
  box-shadow: var(--shadow-xs);
}

.retry:hover {
  border-color: var(--brand-300);
  background: var(--brand-25);
  color: var(--brand-ink);
}

.state-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-4);
  flex-wrap: wrap;
  margin-top: var(--sp-4);
}

.state-actions .retry {
  margin-top: 0;
}

.state-more {
  margin: var(--sp-4) 0 0;
  font-size: var(--text-base);
  color: var(--muted);
}

.link {
  padding: 0;
  border: none;
  background: none;
  font-size: inherit;
  color: var(--brand-ink);
  text-decoration: underline;
  text-decoration-color: var(--brand-200);
  text-underline-offset: 3px;
}

.link:hover {
  color: var(--brand-hover);
  text-decoration-color: var(--brand-400);
}

/* ---------- 分页 ---------- */
.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: var(--sp-6);
}

.pg {
  min-width: 36px;
  height: 36px;
  padding: 0 var(--sp-3);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink-2);
  font-size: var(--text-sm);
  box-shadow: var(--shadow-xs);
}

.pg.num {
  font-family: var(--mono);
}

.pg:hover:not(:disabled):not(.on) {
  border-color: var(--brand-200);
  background: var(--brand-25);
  color: var(--brand-ink);
}

.pg.on {
  background: var(--brand);
  border-color: var(--brand);
  color: var(--on-brand);
  font-weight: 600;
  box-shadow: var(--shadow-brand);
}

.pg:disabled {
  opacity: 0.45;
  box-shadow: none;
}

.pg-info {
  display: none;
  font-size: var(--text-xs);
  color: var(--muted);
}

/* ---------- 页脚 ---------- */
.footer {
  padding: var(--sp-5) 0 var(--sp-6);
  border-top: 1px solid var(--line);
  background: var(--surface);
}

.footer-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  flex-wrap: wrap;
}

.footer-brand {
  font-size: var(--text-xs);
  color: var(--faint);
}

.footer-links {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-4);
  font-size: var(--text-sm);
}

/* ---------- 平板 860 ---------- */
@media (max-width: 860px) {
  .page {
    --nav-h: 56px;
  }

  .wrap {
    padding: 0 var(--sp-4);
  }

  .brand-text em {
    display: none;
  }

  .hero {
    padding: 52px 0 var(--sp-6);
  }

  .subtitle {
    margin-bottom: var(--sp-6);
    font-size: var(--text-base);
  }

  .searchbar {
    padding: 6px 6px 6px var(--sp-3);
  }

  .kbd {
    display: none;
  }

  .hero-meta {
    margin-top: var(--sp-3);
  }

  .intro-grid {
    grid-template-columns: 1fr;
    gap: var(--sp-5);
  }

  .hero.compact {
    top: 0;
  }

  /* 手机端品牌栏不吸顶（滚走），只留搜索条贴顶——
     双行吸顶要吃掉 130px+，长列表可视区太小 */
  .topbar {
    position: static;
  }

  .filters {
    padding: var(--sp-3) 0;
    gap: 6px;
  }

  .content {
    padding-bottom: var(--sp-7);
  }

  .state {
    padding: var(--sp-7) var(--sp-2);
  }

  /* 手机分页只留前后翻 + 页码读数，数字按钮排开太挤 */
  .pg.num {
    display: none;
  }

  .pg-info {
    display: inline;
    margin: 0 var(--sp-1);
  }

  .pager {
    gap: var(--sp-2);
  }

  .footer-inner {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--sp-3);
  }

  .footer-links {
    gap: var(--sp-5);
  }
}

/* ---------- 手机 560 ---------- */
@media (max-width: 560px) {
  .hero {
    padding: 40px 0 var(--sp-5);
  }

  .title {
    font-size: 32px;
  }

  .eyebrow {
    margin-bottom: var(--sp-4);
  }

  /* iOS 输入框 <16px 会触发自动缩放，这条不能省 */
  .searchbar input {
    font-size: 16px;
    padding: 9px 0;
  }

  .searchbar button {
    height: 42px;
    padding: 0 var(--sp-4);
  }

  .hero-try {
    gap: 6px;
  }

  .chip {
    height: 32px;
    padding: 0 11px;
    font-size: var(--text-sm);
  }

  .chip-full {
    display: none;
  }

  .chip-short {
    display: inline;
  }

  .result-head {
    gap: 6px;
  }

  .footer-links {
    font-size: var(--text-base);
  }
}
</style>
