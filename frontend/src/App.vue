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
// 没选网盘就去点搜索时置 true：搜索框不动，把提示打到平台行上，
// 让用户看见"缺的是这一步"，而不是弹一个说完就没的 tostring。
const needPlatform = ref(false)

// ---- 平台偏好 ----
// 只记"用户主动选了哪个网盘"，下次进站默认用它。
//
// **不选 = 不搜**（2026-11-18 改）。以前空值代表"全部平台"，一次搜索背后是
// 5 个平台 × 2 页 = 10 个计费请求；改成"先选网盘再搜"以后，一次搜索最多
// 2 个请求。所以这里再没有"全部"这个选项，空值只表示"还没选"。
const PREF_KEY = 'pt_pref_platform'

const items = ref([])
const page = ref(1)
const totalPages = ref(0)
const total = ref(0)
const tookMs = ref(0)

// 后端单飞锁 / 响应缓存 / 分层通道的回声。用来告诉用户眼前这一屏是
// 刚回源拿的、命中 5 分钟缓存的，还是和同时到达的同键请求合并出来的。
const cached = ref(false)
const shared = ref(false)
const tier = ref('')

// 强制刷新不进骨架屏：结果面板原地更新，只把刷新按钮切成「刷新中」。
// 和 loading 分开是刻意的 —— 刷新失败时 20 条结果必须留在原地，
// 不能因为一次 429 就把用户已经看到的东西换成错误页。
const refreshing = ref(false)
const refreshError = ref('')

const loading = ref(false)
// 等 40 秒的时候，一个跳动的秒数比一句"加载中"有用得多
const elapsed = ref(0)
let elapsedTimer = null

// 防重入统一看这里：刷新期间也不允许切平台 / 翻页 / 再发一次搜索，
// 否则两个请求会互相覆盖，或者白白多花一份额度。
const busy = computed(() => loading.value || refreshing.value)

const resultLabel = computed(() => {
  if (shared.value) return '并发合并'
  if (cached.value) return '缓存结果'
  return '实时结果'
})

const tierLabel = computed(() => {
  if (tier.value === 'tg') return 'TG 免费通道'
  if (tier.value === 'plugin') return '已切换备用源'
  return ''
})

// 序号用于丢弃过期请求：用户连搜两次时，先发那次的后台补拉不能覆盖新结果
let searchSeq = 0
let checkTimers = []

const error = ref('')
const searched = ref(false)

const showEmpty = computed(() => !loading.value && !error.value && searched.value && items.value.length === 0)
const sourceCount = computed(() => platforms.value.length)

// 加载文案里点名在查哪个盘：单平台查询时"正在聚合各平台"是假的，
// 用户也该知道自己这次等的是哪个盘的结果。
const activePlatformName = computed(() => {
  const p = platforms.value.find((x) => x.key === activePlatform.value)
  return p?.name || '网盘'
})

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

async function doSearch(targetPage = 1, opts = {}) {
  const force = !!opts.refresh
  const q = keyword.value.trim()
  if (!q) return
  if (busy.value) return
  // 没选网盘就不打上游。以前这里会退化成"全平台"，一次 10 个计费请求，
  // 用户还以为自己只是搜了个词。现在缺这一步就把提示推到平台行上。
  if (!activePlatform.value) {
    needPlatform.value = true
    return
  }
  needPlatform.value = false

  const seq = ++searchSeq
  clearCheckRefresh()
  refreshError.value = ''
  // 刷新只置 refreshing：模板的骨架屏看 loading，结果面板因此不会被闪掉。
  if (force) refreshing.value = true
  else loading.value = true
  error.value = ''
  searched.value = true
  page.value = targetPage
  lastKeyword.value = q

  // 刷新不启动秒表：结果面板没消失，没有"等了多久"这一说。
  if (!force) {
    elapsed.value = 0
    if (elapsedTimer) clearInterval(elapsedTimer)
    elapsedTimer = setInterval(() => {
      elapsed.value += 1
    }, 1000)
  }

  try {
    const data = await searchResources({
      q,
      platforms: activePlatform.value,
      page: targetPage,
      pageSize: PAGE_SIZE,
      refresh: force,
    })
    items.value = data.items || []
    total.value = data.pagination?.total || 0
    totalPages.value = data.pagination?.totalPages || 0
    tookMs.value = data.tookMs || 0
    cached.value = !!data.cached
    shared.value = !!data.shared
    tier.value = data.tier || ''
    if (force) {
      // 后端自己会记一条 search(cached=0)，但分不出是首搜还是主动刷新。
      // 这条事件只记"用户点了刷新"这个意图，用于判断结果是否不够新。
      track('refresh', {
        query: q,
        platform: activePlatform.value,
        resultCount: items.value.length,
        meta: { tier: tier.value, page: targetPage },
      })
    }
    syncUrl()
    if (targetPage > 1) window.scrollTo({ top: 0, behavior: 'smooth' })
    // 探活跑在响应之后（后端 BackgroundTasks），这一屏注定还没有角标。
    // 不补拉一次，用户只能靠手动刷新才看得到"可访问 / 已失效"，而多数人不会刷。
    // 刷新后同样重排：补拉不带 refresh，命中的正是刚写回的那份缓存。
    if (targetPage === 1) {
      scheduleCheckRefresh(seq, {
        q,
        platforms: activePlatform.value,
        page: targetPage,
        pageSize: PAGE_SIZE,
      })
    }
  } catch (e) {
    if (force) {
      // 刷新失败（多为 429 配额 / 上游抖动）保留当前结果。把已看到的 20 条
      // 清空换成错误页是纯粹的倒退，只给一条就地提示。
      refreshError.value = e.message || '刷新失败，请稍后重试'
    } else {
      items.value = []
      total.value = 0
      totalPages.value = 0
      cached.value = false
      shared.value = false
      tier.value = ''
      error.value = e.message || '搜索失败，请稍后重试'
    }
  } finally {
    if (force) refreshing.value = false
    else loading.value = false
    if (elapsedTimer) {
      clearInterval(elapsedTimer)
      elapsedTimer = null
    }
  }
}

// 跳过 5 分钟缓存重新查一次。后端按 3 份配额计（30 次/分钟 → 最多 10 次），
// 所以按钮只在有结果时可用，且刷新期间禁用，避免连点把额度打光。
function forceRefresh() {
  if (busy.value || !items.value.length) return
  doSearch(page.value, { refresh: true })
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

// 平台图标方块里放的字：用简称，最多两字，115/123 这类纯数字原样显示。
// 没有各家的真实 logo，用品牌色方块 + 简称比清一色的灰点更容易一眼认出。
function platformGlyph(name) {
  const s = shortPlatform(name)
  return s.length > 3 ? s.slice(0, 2) : s
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

// 按选中的网盘查上游：单平台查询拿到的是这个平台自己的完整排序，
// 比"从混排的 20 条里挑出属于它的几条"准得多，这是用户点 chip 期待的东西。
// 也是唯一一次搜索只有 1~2 个请求的原因：一次只问一个盘。
function pickPlatform(key) {
  if (!key) return                 // 没有"全部"档了，空值只表示还没选
  if (activePlatform.value === key) return
  // 正在查询时不给切：否则要么结果和选中的 chip 对不上，
  // 要么并发两个请求互相覆盖（刷新期间同样不让切）。
  if (busy.value) return
  activePlatform.value = key
  needPlatform.value = false
  writePrefPlatform(key)
  // 筛选行为本身值得记：哪个平台被偏爱、筛选后还有没有人继续搜
  track('filter', {
    query: lastKeyword.value || keyword.value.trim(),
    platform: key,
  })
  // 跟以前一样：点平台就是一次真实查询，不是在同一批结果里挑。
  if (searched.value) doSearch(1)
}

// 空结果时用：把视线拉回平台行，让用户换一个盘再试。
// 这里不再有"搜索全部平台"——那正是要砍掉的那个 10 请求动作。
function backToPlatforms() {
  needPlatform.value = true
  document.querySelector('.filter-rows')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function goPage(p) {
  if (p < 1 || p > totalPages.value || p === page.value || busy.value) return
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
  if (busy.value) return
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
    <!-- 背景辉光：两层极低透明度的径向渐变，固定在视口上。
         它们是这个站唯一的装饰，别再加第三层——再多就成了"发光海报" -->
    <div class="aura" aria-hidden="true"></div>

    <header class="topbar">
      <div class="wrap topbar-inner">
        <nav class="topnav" aria-label="主导航">
          <a class="tn on" href="/" @click.prevent="resetHome">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.6-3.6" />
            </svg>
            搜资源
          </a>
          <button class="tn" type="button" @click="openFeedback('request')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
            求资源
          </button>
          <button class="tn" type="button" @click="showGroupManual">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M20 15a3 3 0 0 1-3 3H8l-4 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z" />
            </svg>
            进群交流
          </button>
        </nav>
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
        <a class="lockup" href="/" @click.prevent="resetHome">
          <h1 class="title">盘迹</h1>
          <span class="domain mono">PANTRACE · 全网网盘聚合搜索</span>
        </a>

        <p class="hero-stats mono">
          <span v-if="sourceCount">{{ sourceCount }} 个来源</span>
          <span v-else>来源同步中</span>
          <i>·</i>
          <span>通常 1–3s</span>
          <i>·</i>
          <span>链接 30 分钟内有效</span>
        </p>

        <form class="searchbar" :class="{ loading }" @submit.prevent="doSearch(1)">
          <span class="search-icon" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.6-3.6" />
            </svg>
          </span>
          <input
            v-model="keyword"
            type="search"
            :placeholder="activePlatform ? '搜影视、电子书、学习资料…' : '先在下面选一个网盘，再搜资源'"
            autocomplete="off"
            autocapitalize="off"
            spellcheck="false"
            enterkeyhint="search"
            maxlength="100"
            aria-label="搜索关键词"
            @focus="needPlatform = !activePlatform"
          />
          <button class="sb-go" type="submit" :disabled="busy || !keyword.trim()">
            {{ busy ? '搜索中' : '搜索' }}
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

        <!-- 平台筛选放在搜索框正下方：这一行本身就是"收录了哪些网盘"的说明书，
             不用先去搜一次才知道能筛什么。**必须选一个**——没选就不发搜索请求，
             因为每个网盘都是一次独立计费上游查询。 -->
        <div class="filter-rows" :class="{ 'need-pick': needPlatform }" role="group" aria-label="选择网盘">
          <div class="frow">
            <span class="frow-label">{{ needPlatform ? '先选一个' : '云盘' }}</span>
            <div class="frow-body">
              <button
                v-for="p in platforms"
                :key="p.key"
                class="pchip"
                :class="{ on: activePlatform === p.key }"
                :style="{ '--pc': p.color || 'var(--slate-500)' }"
                :title="p.name"
                @click="pickPlatform(p.key)"
              >
                <i class="pchip-ic" aria-hidden="true">{{ platformGlyph(p.name) }}</i>
                <span>{{ shortPlatform(p.name) }}</span>
              </button>
            </div>
          </div>
          <p v-if="needPlatform" class="frow-hint">
            选一个网盘再搜 —— 每次搜索只查你选的那个，速度更快
          </p>
        </div>
      </div>
    </section>

    <main class="wrap content">
      <div v-if="loading" class="scan-state">
        <div class="scan-head">
          <span class="scan-label mono">SCANNING</span>
          <span class="scan-count mono" v-if="elapsed">{{ elapsed }}s</span>
        </div>
        <p class="loading-hint">正在检索 {{ activePlatformName }}，通常 1–3 秒。</p>
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
        <p class="state-desc">换个关键词试试，或者换一个网盘再搜。</p>
        <button class="retry" @click="backToPlatforms">换个网盘</button>
        <p class="state-more">
          都没有？
          <button class="link" @click="openFeedback('request')">告诉我你要找什么</button>
        </p>
      </div>

      <section v-else-if="items.length" class="panel">
        <header class="panel-head">
          <span class="panel-title"><i class="panel-dot"></i>搜索结果</span>
          <div class="panel-tools">
            <span class="state-chip" :class="{ saved: cached || shared }">{{ resultLabel }}</span>
            <span v-if="tierLabel" class="state-chip">{{ tierLabel }}</span>
            <button
              class="refresh-btn"
              type="button"
              :disabled="busy"
              title="跳过 5 分钟缓存重新查一次（会消耗搜索额度）"
              @click="forceRefresh"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M21 12a9 9 0 1 1-2.64-6.36" />
                <path d="M21 3v6h-6" />
              </svg>
              <span>{{ refreshing ? '刷新中' : '强制刷新' }}</span>
            </button>
            <span class="panel-meta mono">
              {{ totalText }} 条 · <template v-if="shared">复用</template><template v-else>{{ tookMs }}ms</template> · {{ page }}/{{ totalPages }} 页
            </span>
          </div>
        </header>
        <!-- 刷新失败只就地提示，结果不清空：被限流时把已看到的 20 条换成错误页是倒退 -->
        <p v-if="refreshError" class="refresh-note">{{ refreshError }}</p>

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
      </section>

      <div v-if="!loading && items.length && totalPages > 1" class="pager">
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

      <!-- 网站介绍：位置挪到内容之后。首屏留给搜索框，说明文字放在这一屏的下方，
           既不抢注意力，也保住这个站唯一能被搜索引擎抓到的成段内容。 -->
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
/* ==========================================================================
   主站（搜索页）
   --------------------------------------------------------------------------
   深色底 + 青绿强调。首屏从上到下只有四件事：品牌、搜索框、快捷词、平台筛选，
   没有别的。结果装在一块独立面板里，跟首屏在视觉上分开——搜索是"输入"，
   结果是"输出"，两者不该长得一样。
   ========================================================================== */

.page {
  position: relative;
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  background: var(--bg);
  isolation: isolate;
}

/* 背景辉光：固定不随内容滚动，两层径向 + 一道斜向微光。
   已经是这个站全部的装饰了，不要再加 */
.aura {
  position: fixed;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  background:
    radial-gradient(1040px 620px at 4% -12%, var(--glow-mint), transparent 66%),
    radial-gradient(1080px 660px at 96% -4%, var(--glow-cyan), transparent 68%),
    linear-gradient(112deg, transparent 46%, var(--glow-sheen) 60%, transparent 76%);
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
  backdrop-filter: blur(14px) saturate(140%);
  -webkit-backdrop-filter: blur(14px) saturate(140%);
  border-bottom: 1px solid var(--line);
}

.topbar-inner {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  height: var(--nav-h);
}

.topnav {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
}

.tn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 14px;
  border: 1px solid transparent;
  border-radius: var(--radius-pill);
  background: transparent;
  font-size: var(--text-base);
  color: var(--muted);
}

.tn:hover {
  color: var(--ink);
  background: var(--surface-3);
}

.tn:active {
  background: var(--surface-hover);
}

.tn.on {
  color: var(--brand-ink);
  background: var(--brand-soft);
  border-color: var(--brand-ring);
}

.topbar-right {
  position: absolute;
  right: 0;
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}

.status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-pill);
  background: var(--surface-2);
  font-size: var(--text-2xs);
  letter-spacing: 0.08em;
  color: var(--muted);
}

.status-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--brand);
  box-shadow: 0 0 0 3px var(--cyan-ring);
}

/* ---------- 首屏 ---------- */

.hero {
  padding: var(--sp-7) 0 var(--sp-7);
}

.hero-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.lockup {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-2);
}

.title {
  font-size: clamp(30px, 5vw, 40px);
  font-weight: 700;
  line-height: 1.1;
  letter-spacing: 0.04em;
  color: var(--ink);
  transition: font-size var(--dur-3) var(--ease);
}

.domain {
  font-size: var(--text-xs);
  letter-spacing: 0.14em;
  color: var(--faint);
  text-transform: uppercase;
}

.hero-stats {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: var(--sp-1);
  margin-top: var(--sp-3);
  font-size: var(--text-xs);
  color: var(--muted);
}

.hero-stats i {
  font-style: normal;
  color: var(--line-strong);
}

/* ---------- 搜索框 ---------- */

.searchbar {
  position: relative;
  display: flex;
  align-items: center;
  width: 100%;
  max-width: 680px;
  height: 56px;
  margin-top: var(--sp-6);
  padding: 0 6px 0 18px;
  border: 1px solid var(--line-2);
  border-radius: var(--radius-pill);
  background: var(--surface-2);
  box-shadow: var(--inset-top), var(--shadow-sm);
  transition:
    border-color var(--dur-2) var(--ease),
    box-shadow var(--dur-3) var(--ease),
    background-color var(--dur-2) var(--ease);
}

.searchbar:hover {
  border-color: var(--line-strong);
}

/* 聚焦时的光圈：整页唯一的"发光"交互，用来说明"现在可以打字了" */
.searchbar:focus-within {
  border-color: var(--brand);
  background: var(--surface-3);
  box-shadow: 0 0 0 4px var(--brand-soft), 0 18px 44px -24px var(--brand-ring);
}

.search-icon {
  display: flex;
  flex-shrink: 0;
  color: var(--faint);
  transition: color var(--dur-2) var(--ease);
}

.searchbar:focus-within .search-icon {
  color: var(--brand);
}

.searchbar input {
  flex: 1;
  min-width: 0;
  height: 100%;
  padding: 0 var(--sp-3) 0 var(--sp-3);
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
  -webkit-appearance: none;
}

.sb-go {
  flex-shrink: 0;
  height: 44px;
  padding: 0 24px;
  border: 1px solid transparent;
  border-radius: var(--radius-pill);
  background: var(--brand);
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--on-brand);
  box-shadow: var(--shadow-brand);
}

.sb-go:hover:not(:disabled) {
  background: var(--brand-hover);
}

.sb-go:active:not(:disabled) {
  background: var(--brand-active);
  box-shadow: none;
}

/* 禁用态不是"消失"，是"还不能点"：文字必须读得出来 */
.sb-go:disabled {
  background: var(--surface-hover);
  color: var(--muted);
  box-shadow: none;
  cursor: not-allowed;
}

/* 搜索时沿输入框跑一圈的青绿扫描线 */
.scanline {
  position: absolute;
  right: 96px;
  bottom: 6px;
  left: 46px;
  height: 1px;
  overflow: hidden;
  border-radius: var(--radius-pill);
}

.scanline::before {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent, var(--brand), transparent);
  transform: translateX(-100%);
  animation: scan 1.4s var(--ease) infinite;
}

@keyframes scan {
  to {
    transform: translateX(100%);
  }
}

/* ---------- 快捷词 ---------- */

.hero-try {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  margin-top: var(--sp-4);
}

.ht-label {
  font-size: var(--text-2xs);
  letter-spacing: 0.14em;
  color: var(--faint);
  text-transform: uppercase;
}

.ht-word {
  padding: 5px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-pill);
  background: transparent;
  font-size: var(--text-sm);
  color: var(--muted);
}

.ht-word:hover {
  color: var(--ink);
  border-color: var(--line-2);
  background: var(--surface-3);
}

/* ---------- 平台筛选 ---------- */

.filter-rows {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr);
  gap: var(--sp-3);
  width: 100%;
  max-width: 760px;
  margin-top: var(--sp-5);
}

.frow {
  display: contents;
}

/* 没选网盘时的提示：跨两列贴在 chip 下方，和 chip 同属一个视觉块，
   不能挤进 44px 的 label 列，也不能把 chip 顶下去。 */
.frow-hint {
  grid-column: 1 / -1;
  margin: calc(-1 * var(--sp-1)) 0 0;
  font-size: var(--text-sm);
  color: var(--brand-400);
}

/* 缺的就是这一步：把标签点亮点，视线自然会落到该点的地方 */
.filter-rows.need-pick .frow-label {
  color: var(--brand-400);
}

.frow-label {
  padding-top: 6px;
  font-size: var(--text-sm);
  color: var(--faint);
  text-align: right;
}

.frow-body {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  min-width: 0;
}

.pchip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 12px 0 5px;
  border: 1px solid var(--line);
  border-radius: var(--radius-pill);
  background: var(--surface-2);
  font-size: var(--text-sm);
  /* 平台名用主文字色：chip 里已经有一块彩色方块，文字再压暗就整片发灰 */
  color: var(--ink);
}

.pchip:hover {
  border-color: var(--line-2);
  background: var(--surface-3);
  color: var(--ink);
}

.pchip.on {
  border-color: var(--brand-ring);
  background: var(--brand-soft);
  color: var(--brand-ink);
}

/* 品牌色方块：没有各家真实 logo，用品牌色 + 简称，比清一色灰点好认 */
.pchip-ic {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 20px;
  padding: 0 4px;
  border-radius: 6px;
  background: var(--pc, var(--slate-500));
  font-family: var(--mono);
  font-size: 9.5px;
  font-style: normal;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--on-color);
}

.pchip-ic.all {
  background: var(--surface-hover);
  color: var(--muted);
}

.pchip.on .pchip-ic.all {
  background: var(--brand);
  color: var(--on-brand);
}

/* ---------- 首屏收窄（已有结果时） ---------- */

.hero.compact {
  padding: var(--sp-5) 0 var(--sp-5);
}

.hero.compact .lockup {
  flex-direction: row;
  align-items: baseline;
  gap: var(--sp-3);
}

.hero.compact .title {
  font-size: var(--text-2xl);
  letter-spacing: 0.02em;
}

.hero.compact .hero-stats {
  display: none;
}

.hero.compact .searchbar {
  height: 52px;
  margin-top: var(--sp-4);
}

.hero.compact .filter-rows {
  margin-top: var(--sp-4);
}

/* ---------- 内容区 ---------- */

.content {
  flex: 1;
  padding-bottom: var(--sp-8);
}

/* ---------- 结果面板 ---------- */

.panel {
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--inset-top), var(--shadow-md);
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: 13px var(--sp-5);
  border-bottom: 1px solid var(--line);
}

.panel-title {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--ink);
}

.panel-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-soft);
}

.panel-meta {
  font-size: var(--text-xs);
  color: var(--muted);
}

/* 结果头右侧：状态徽标 + 强制刷新 + 计数。窄屏靠 flex-wrap 自然换行，
   不把 panel-head 拆成定高两行，行数由内容自己决定。 */
.panel-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}

.state-chip {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border: 1px solid var(--line-2);
  border-radius: 999px;
  background: var(--surface-2);
  font-size: var(--text-2xs);
  line-height: 1;
  color: var(--muted);
  white-space: nowrap;
}

/* 缓存命中 / 同键搭车 = 这一轮没花额度，用品牌色点出来。
   --warn 只留给真正的警示，不用在这里。 */
.state-chip.saved {
  border-color: var(--brand-ring);
  background: var(--brand-soft);
  color: var(--brand-ink);
}

.refresh-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 9px;
  border: 1px solid var(--brand-ring);
  border-radius: var(--radius-sm);
  background: transparent;
  font-size: var(--text-xs);
  color: var(--brand-ink);
  cursor: pointer;
  transition: background .15s ease, color .15s ease, border-color .15s ease;
}

.refresh-btn svg {
  flex-shrink: 0;
}

/* 静止态淡青绿描边，hover 才填满：结果头只该有一个实心亮块 */
.refresh-btn:hover:not(:disabled) {
  border-color: var(--brand);
  background: var(--brand);
  color: var(--on-brand);
}

.refresh-btn:disabled {
  opacity: .45;
  cursor: default;
}

.refresh-note {
  margin: 0;
  padding: 8px var(--sp-5);
  border-bottom: 1px solid var(--warn-soft);
  background: var(--warn-soft);
  font-size: var(--text-sm);
  color: var(--warn-ink);
}

.ttl-note {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px var(--sp-5);
  border-bottom: 1px solid var(--line);
  background: var(--surface-2);
  font-size: var(--text-sm);
  line-height: 1.5;
  color: var(--muted);
  text-wrap: pretty;
}

.ttl-note svg {
  flex-shrink: 0;
  color: var(--warn);
}

.ttl-note b {
  font-weight: 600;
  color: var(--ink-2);
}

.result-list {
  display: flex;
  flex-direction: column;
}

/* ---------- 分页 ---------- */

.pager {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: var(--sp-6);
}

.pg {
  min-width: 32px;
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  font-size: var(--text-sm);
  color: var(--muted);
}

.pg:hover:not(:disabled):not(.on) {
  color: var(--ink);
  border-color: var(--line-2);
  background: var(--surface-3);
}

.pg.on {
  border-color: var(--brand-ring);
  background: var(--brand-soft);
  font-weight: 600;
  color: var(--brand-ink);
}

.pg:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.pg.num {
  padding: 0 8px;
  font-variant-numeric: tabular-nums;
}

.pg-info {
  padding: 0 var(--sp-2);
  font-size: var(--text-xs);
  color: var(--faint);
}

/* ---------- 加载 / 空 / 错误 ---------- */

.scan-state {
  padding-top: var(--sp-2);
}

.scan-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-3);
}

.scan-label {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--text-2xs);
  letter-spacing: 0.16em;
  color: var(--brand-ink);
}

.scan-label::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--brand);
  animation: pulse 1.2s var(--ease) infinite;
}

@keyframes pulse {
  50% {
    opacity: 0.35;
  }
}

.scan-count {
  font-size: var(--text-xs);
  color: var(--faint);
}

.loading-hint {
  margin-bottom: var(--sp-4);
  font-size: var(--text-base);
  color: var(--muted);
}

.scan-track {
  height: 2px;
  margin-bottom: var(--sp-5);
  overflow: hidden;
  border-radius: var(--radius-pill);
  background: var(--surface-3);
}

.scan-track i {
  display: block;
  width: 34%;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, transparent, var(--brand), transparent);
  animation: track 1.3s var(--ease) infinite;
}

@keyframes track {
  from {
    transform: translateX(-100%);
  }
  to {
    transform: translateX(320%);
  }
}

.skeleton-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.skeleton {
  padding: var(--sp-5);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
}

.sk-line {
  height: 10px;
  border-radius: 4px;
  background: linear-gradient(
    90deg,
    var(--surface-3) 25%,
    var(--surface-hover) 37%,
    var(--surface-3) 63%
  );
  background-size: 400% 100%;
  animation: shimmer 1.5s var(--ease) infinite;
}

.sk-line + .sk-line {
  margin-top: 10px;
}

@keyframes shimmer {
  from {
    background-position: 100% 50%;
  }
  to {
    background-position: 0 50%;
  }
}

.w60 {
  width: 60%;
}

.w30 {
  width: 30%;
}

.state {
  padding: var(--sp-9) var(--sp-5);
  text-align: center;
}

.state-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  margin: 0 auto var(--sp-4);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-3);
  font-size: var(--text-lg);
  color: var(--muted);
}

.state-icon.empty {
  color: var(--faint);
}

.error-state .state-icon {
  border-color: var(--danger-line);
  background: var(--danger-soft);
  color: var(--danger-ink);
}

.state-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--ink);
}

.state-desc {
  max-width: 440px;
  margin: var(--sp-2) auto 0;
  font-size: var(--text-base);
  line-height: 1.7;
  color: var(--muted);
}

.state-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: var(--sp-3);
  margin-top: var(--sp-5);
}

.state-more {
  margin-top: var(--sp-4);
  font-size: var(--text-base);
  color: var(--muted);
}

.retry {
  height: 36px;
  padding: 0 18px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: var(--brand);
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--on-brand);
}

.retry:hover {
  background: var(--brand-hover);
}

.retry:active {
  background: var(--brand-active);
}

.link {
  padding: 0;
  border: none;
  background: none;
  font-size: var(--text-base);
  color: var(--brand-ink);
  text-decoration: underline;
  text-decoration-color: var(--brand-ring);
  text-underline-offset: 3px;
}

.link:hover {
  color: var(--brand);
  text-decoration-color: var(--brand);
}

/* ---------- 网站介绍（首屏之下） ---------- */

.intro {
  margin-top: var(--sp-9);
  padding-top: var(--sp-6);
  border-top: 1px solid var(--line);
}

.intro-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--sp-7);
}

.intro-h {
  margin-bottom: var(--sp-2);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.14em;
  color: var(--muted);
  text-transform: uppercase;
}

.intro-block p {
  font-size: var(--text-base);
  line-height: 1.8;
  color: var(--muted);
}

/* ---------- 页脚 ---------- */

.footer {
  border-top: 1px solid var(--line);
  padding: var(--sp-5) 0 var(--sp-7);
}

.footer-inner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
}

.footer-brand {
  font-size: var(--text-xs);
  color: var(--faint);
}

.footer-links {
  display: flex;
  align-items: center;
  gap: var(--sp-5);
}

.footer-links .link {
  font-size: var(--text-sm);
  color: var(--muted);
  text-decoration: none;
}

.footer-links .link:hover {
  color: var(--brand-ink);
}

/* ---------- 平板 ---------- */

@media (max-width: 860px) {
  .wrap {
    padding: 0 var(--sp-5);
  }

  .hero {
    padding: var(--sp-7) 0 var(--sp-6);
  }

  .searchbar {
    max-width: none;
  }

  .filter-rows {
    grid-template-columns: 40px minmax(0, 1fr);
    max-width: none;
  }

  .intro-grid {
    grid-template-columns: 1fr;
    gap: var(--sp-5);
  }

  .panel-head {
    padding: 12px var(--sp-4);
  }

  .ttl-note {
    padding: 9px var(--sp-4);
  }

  .state {
    padding: var(--sp-8) var(--sp-4);
  }
}

/* ---------- 手机 ---------- */

@media (max-width: 560px) {
  .topbar {
    position: static;
  }

  .topbar-inner {
    justify-content: space-between;
    height: 56px;
  }

  .topnav {
    gap: 0;
  }

  .tn {
    gap: 4px;
    padding: 0 9px;
    font-size: var(--text-sm);
  }

  .tn svg {
    width: 12px;
    height: 12px;
  }

  /* 来源数在手机上占地方，且对手机用户没有决策价值 */
  .status {
    display: none;
  }

  .hero {
    padding: var(--sp-6) 0 var(--sp-5);
  }

  .hero.compact .lockup {
    flex-direction: column;
    align-items: center;
    gap: 2px;
  }

  .hero.compact .title {
    font-size: var(--text-xl);
  }

  .hero-stats {
    font-size: var(--text-2xs);
  }

  .searchbar {
    height: 50px;
    margin-top: var(--sp-5);
    padding-left: var(--sp-4);
  }

  .searchbar input {
    font-size: var(--text-base);
  }

  .sb-go {
    height: 40px;
    padding: 0 18px;
    font-size: var(--text-sm);
  }

  .scanline {
    right: 74px;
    left: 40px;
  }

  /* 手机上标签改到 chip 上方一行：44px 的标签列会白白吃掉十分之一屏宽 */
  .filter-rows {
    grid-template-columns: 1fr;
    gap: var(--sp-2);
    margin-top: var(--sp-5);
  }

  .frow-label {
    padding-top: 0;
    text-align: left;
  }

  /* 手机上十几个平台自然排成四行。不做限高滚动：
     切成半行比多占 40px 更像 bug */
  .frow-body {
    gap: 6px;
    justify-content: flex-start;
  }

  .pchip {
    height: 28px;
    padding: 0 10px 0 4px;
  }

  .panel-head {
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
  }

  .panel-tools {
    justify-content: flex-start;
    gap: 5px;
  }

  .pg {
    min-width: 30px;
    height: 30px;
    padding: 0 9px;
  }

  .pg.num {
    padding: 0 6px;
  }

  .pg-info {
    display: none;
  }

  .intro {
    margin-top: var(--sp-7);
  }

  .footer-inner {
    flex-direction: column;
    align-items: flex-start;
  }

  .footer-links {
    gap: var(--sp-4);
  }
}
</style>
