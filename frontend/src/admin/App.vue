<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Gate from './components/Gate.vue'
import TrendCard from './components/TrendCard.vue'
import RankCard from './components/RankCard.vue'
import ZeroPanel from './components/ZeroPanel.vue'
import FeedbackPanel from './components/FeedbackPanel.vue'
import AccountsPanel from './components/AccountsPanel.vue'
import GroupPanel from './components/GroupPanel.vue'
import ClicksPanel from './components/ClicksPanel.vue'
import LinkCheckPanel from './components/LinkCheckPanel.vue'
import * as api from './api'
import { ms, pct, platformName } from './format'

const RANGES = [
  { days: 1, label: '今日' },
  { days: 7, label: '近 7 天' },
  { days: 30, label: '近 30 天' },
]

const ICON = {
  grid: '<svg viewBox="0 0 24 24" fill="none"><rect x="3.5" y="3.5" width="7" height="7" rx="2" stroke="currentColor" stroke-width="1.8"/><rect x="13.5" y="3.5" width="7" height="7" rx="2" stroke="currentColor" stroke-width="1.8"/><rect x="3.5" y="13.5" width="7" height="7" rx="2" stroke="currentColor" stroke-width="1.8"/><rect x="13.5" y="13.5" width="7" height="7" rx="2" stroke="currentColor" stroke-width="1.8"/></svg>',
  inbox: '<svg viewBox="0 0 24 24" fill="none"><path d="M3.5 13.5h4l1.4 2.4h6.2l1.4-2.4h4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M5.6 5.5h12.8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5.6a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2z" stroke="currentColor" stroke-width="1.8"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none"><circle cx="10.5" cy="10.5" r="6.5" stroke="currentColor" stroke-width="1.8"/><path d="M15.4 15.4 20 20" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  link: '<svg viewBox="0 0 24 24" fill="none"><path d="M9.6 14.4 14.4 9.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M11.2 7.2 12.8 5.6a3.6 3.6 0 0 1 5.1 5.1L16.3 12.3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M12.8 16.8 11.2 18.4a3.6 3.6 0 0 1-5.1-5.1L7.7 11.7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  pulse: '<svg viewBox="0 0 24 24" fill="none"><path d="M3 12.5h3.6L9 6l4 12 2.4-5.5H21" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  key: '<svg viewBox="0 0 24 24" fill="none"><circle cx="8.2" cy="8.2" r="4.2" stroke="currentColor" stroke-width="1.8"/><path d="M11.2 11.2 19.6 19.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M16.6 16.6 18.6 14.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  users: '<svg viewBox="0 0 24 24" fill="none"><circle cx="9.2" cy="8.8" r="3.2" stroke="currentColor" stroke-width="1.8"/><path d="M3.6 19.2c0-3 2.5-5 5.6-5s5.6 2 5.6 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M15.8 6.2a3.2 3.2 0 0 1 0 5.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M17.2 14.6c1.9.7 3.2 2.3 3.2 4.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
}

const NAV = [
  { key: 'overview', label: '概览', icon: ICON.grid },
  { key: 'feedback', label: '待办与反馈', icon: ICON.inbox, badge: true },
  { key: 'search', label: '搜索诊断', icon: ICON.search },
  { key: 'check', label: '资源巡检', icon: ICON.link },
  { key: 'clicks', label: '点击日志', icon: ICON.pulse },
  { type: 'group', label: '配置' },
  { key: 'accounts', label: '网盘凭据', icon: ICON.key },
  { key: 'group', label: '群引导配置', icon: ICON.users },
]

const TITLES = {
  overview: '概览',
  feedback: '待办与反馈',
  search: '搜索诊断',
  check: '资源巡检',
  clicks: '点击日志',
  accounts: '网盘凭据',
  group: '群引导配置',
}

const days = ref(7)
const route = ref('overview')
const data = ref({})
const fb = ref({})
const acct = ref({})
const lc = ref({})
const ready = ref(false)
const needToken = ref(false)
const gateMsg = ref('')
const errorMsg = ref('')
const lastLoaded = ref('')

const o = computed(() => data.value.overview || {})
const rangeText = computed(() => (days.value === 1 ? '今天' : `近 ${days.value} 天`))

// ---- 路由：hash 驱动，刷新和分享都能落到同一页 ----

function readHash() {
  const key = String(location.hash || '').replace(/^#\/?/, '')
  return TITLES[key] ? key : 'overview'
}

function go(key) {
  const hash = '#/' + key
  if (location.hash === hash) route.value = key
  else location.hash = hash
}

function onHashChange() {
  route.value = readHash()
}

// ---- 健康带：失败率 / 响应 / 凭据 / 检测空转 ----

const acctOk = computed(() =>
  (acct.value.items || []).filter((a) => a.status && !a.lastError && !(a.failCount > 0)).length
)
const acctEnabled = computed(() => Number(acct.value.enabled) || 0)

const lcLive = computed(() => lc.value.live || {})
const lcTotal = computed(() =>
  Object.values(lcLive.value).reduce((s, n) => s + (Number(n) || 0), 0)
)
const lcSpin = computed(() => {
  if (!lcTotal.value) return null
  return Math.round(((Number(lcLive.value.uncertain) || 0) / lcTotal.value) * 100)
})

const health = computed(() => {
  const v = o.value
  const fail = Number(v.fail_rate) || 0
  const avg = Number(v.avg_ms) || 0
  const credBad = acctEnabled.value > 0 && acctOk.value < acctEnabled.value
  const spinBad = lcSpin.value !== null && lcSpin.value >= 30
  const failBad = fail >= 0.02
  const avgBad = avg >= 2000
  const level = failBad || credBad || spinBad ? 'warn' : 'ok'
  return {
    level,
    label: level === 'ok' ? '运行正常' : '需要看一眼',
    fail: pct(fail),
    failTone: failBad ? 'bad' : '',
    avg: ms(avg),
    avgTone: avgBad ? 'warn' : '',
    cred: acctEnabled.value ? `${acctOk.value}/${acctEnabled.value}` : '—',
    credTone: credBad ? 'bad' : '',
    spin: lcSpin.value === null ? '-' : lcSpin.value + '%',
    spinTone: spinBad ? 'warn' : '',
  }
})

// ---- KPI：每个数都配一个参照，否则看不出好坏 ----

function ratio(cur, prev) {
  const c = Number(cur) || 0
  const p = Number(prev) || 0
  if (!p) return null
  return (c - p) / p
}

function deltaText(d) {
  if (d === null) return ''
  const sign = d >= 0 ? '+' : ''
  return `较上周期 ${sign}${(d * 100).toFixed(1)}%`
}

const pendingItems = computed(() => (fb.value.items || []).filter((i) => i.status === 'new'))
const pendingCount = computed(() => Number((fb.value.counts || {}).pending) || 0)

const pendingKinds = computed(() => {
  const map = {}
  for (const it of pendingItems.value) {
    const name = it.kindName || '其他'
    map[name] = (map[name] || 0) + 1
  }
  return Object.entries(map)
    .sort((a, b) => b[1] - a[1])
    .map(([name, n]) => `${n} ${name}`)
    .join(' · ')
})

const kpis = computed(() => {
  const v = o.value
  const p = v.prev || {}

  const dSearch = ratio(v.searches, p.searches)
  const dCtr = ratio(v.ctr, p.ctr)
  const dZero = ratio(v.zero_rate, p.zero_rate)

  const zero = Number(v.zero_rate) || 0
  const ctr = Number(v.ctr) || 0
  const zeroGap = (zero - 0.2) * 100

  return [
    {
      k: '搜索次数',
      v: (Number(v.searches) || 0).toLocaleString('zh-CN'),
      tone: 'brand',
      hint: dSearch === null ? '区间内有效搜索' : deltaText(dSearch),
      hintTone: dSearch === null ? '' : dSearch >= 0 ? 'up' : 'down',
    },
    {
      k: '点击转化率',
      v: pct(ctr),
      tone: '',
      hint:
        ctr < 0.25
          ? `健康区间 25–35% · 低 ${((0.25 - ctr) * 100).toFixed(1)} 个点`
          : ctr > 0.35
            ? '健康区间 25–35% · 高于上限'
            : '健康区间 25–35% · 区间内',
      hintTone: ctr < 0.25 ? 'warn' : '',
      extra: dCtr === null ? '' : deltaText(dCtr),
    },
    {
      k: '零结果率',
      v: pct(zero),
      tone: zero > 0.3 ? 'warn' : '',
      hint: zero > 0.2 ? `健康线 20% · 高出 ${zeroGap.toFixed(1)} 个点` : '低于健康线 20%',
      hintTone: zero > 0.2 ? 'warn' : '',
      extra: dZero === null ? '' : deltaText(dZero),
    },
    {
      k: '待处理',
      v: pendingCount.value,
      tone: pendingCount.value > 0 ? 'bad' : '',
      hint: pendingKinds.value || '没有待处理的反馈',
      hintTone: '',
    },
  ]
})

const refererRows = computed(() =>
  (data.value.referers || []).map((r) => ({ label: r.r || '直接访问', n: r.n }))
)
const keywordRows = computed(() =>
  (data.value.keywords || []).map((r) => ({ label: r.q, n: r.n }))
)
const platformRows = computed(() =>
  (data.value.platforms || []).map((r) => ({ label: platformName(r.key), n: r.n }))
)
const targetRows = computed(() =>
  (data.value.targets || []).map((r) => ({ label: r.t, n: r.n }))
)

// ---- 数据加载 ----

async function loadStats(silent = false) {
  try {
    data.value = await api.stats(days.value)
    ready.value = true
    needToken.value = false
    errorMsg.value = ''
    lastLoaded.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (e) {
    if (e.status === 401) {
      needToken.value = true
      ready.value = false
      gateMsg.value = api.hasToken() ? '口令无效，请重新输入' : ''
      return
    }
    if (!silent) errorMsg.value = e.message
  }
}

async function loadFeedback(silent = false) {
  try {
    fb.value = await api.feedback(days.value)
  } catch (e) {
    if (e.status === 401) return
    if (!silent) errorMsg.value = errorMsg.value || e.message
  }
}

async function loadHealth() {
  try {
    acct.value = await api.accounts()
  } catch (e) {
    /* 单个模块挂了不该拖垮整个看板 */
  }
  try {
    const d = await api.linkCheck('', 0)
    lc.value = d.summary || {}
  } catch (e) {
    /* 检测关掉时这里 503，按"没有数据"处理 */
  }
}

async function loadAll() {
  await loadStats()
  await Promise.all([loadFeedback(), loadHealth()])
}

async function markDone(id) {
  try {
    await api.markFeedbackDone(id)
    await loadFeedback()
  } catch (e) {
    errorMsg.value = e.message
  }
}

function switchRange(d) {
  if (days.value === d) return
  days.value = d
  loadStats()
  loadFeedback()
}

function onToken(value) {
  api.setToken(value)
  gateMsg.value = ''
  loadAll()
}

function logout() {
  api.setToken('')
  needToken.value = true
  ready.value = false
  gateMsg.value = ''
  data.value = {}
  fb.value = {}
}

let timer = null
onMounted(() => {
  api.setToken(api.readToken())
  route.value = readHash()
  window.addEventListener('hashchange', onHashChange)
  if (!location.hash) location.replace('#/overview')
  loadAll()
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible' || needToken.value) return
    loadStats(true)
    loadFeedback(true)
  }, 60000)
})

onUnmounted(() => {
  window.removeEventListener('hashchange', onHashChange)
  if (timer) clearInterval(timer)
})
</script>

<template>
  <Gate v-if="needToken" :message="gateMsg" @submit="onToken" />

  <div v-else class="shell">
    <aside class="rail">
      <div class="rail-brand">
        <div class="rail-logo">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M6 15.5 12 7l6 8.5" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
            <circle cx="12" cy="16.6" r="1.5" fill="#fff" />
          </svg>
        </div>
        <div>
          <div class="rail-name">盘迹</div>
          <div class="rail-sub">运营后台</div>
        </div>
      </div>

      <nav class="nav">
        <template v-for="(n, i) in NAV" :key="i">
          <div v-if="n.type === 'group'" class="nav-group">{{ n.label }}</div>
          <button
            v-else
            class="nav-item"
            :class="{ on: route === n.key }"
            @click="go(n.key)"
          >
            <span class="nav-ico" v-html="n.icon" />
            <span class="nav-label">{{ n.label }}</span>
            <span v-if="n.badge && pendingCount" class="nav-badge">{{ pendingCount }}</span>
          </button>
        </template>
      </nav>

      <div class="rail-foot">
        <div class="rail-status" :class="health.level === 'ok' ? '' : 'warn'">
          <span class="dot" />
          <span>{{ health.label }}<template v-if="acctEnabled"> · {{ health.cred }} 凭据</template></span>
        </div>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="tb-title">
          <div class="tb-name">{{ TITLES[route] }}</div>
          <div class="tb-sub">
            {{ rangeText }}<template v-if="lastLoaded"> · 更新于 {{ lastLoaded }}</template>
            · 自动刷新 60 秒 · 机器人流量已剔除
          </div>
        </div>
        <div class="tb-actions">
          <div class="seg">
            <button
              v-for="r in RANGES"
              :key="r.days"
              :class="{ on: days === r.days }"
              @click="switchRange(r.days)"
            >{{ r.label }}</button>
          </div>
          <button class="ghost" @click="loadAll">刷新</button>
          <button class="ghost plain" @click="logout">改口令</button>
        </div>
      </header>

      <div v-if="errorMsg" class="err" style="padding: 10px 24px 0">{{ errorMsg }}</div>
      <div v-if="!ready" class="empty">载入中…</div>

      <div v-if="ready" class="page">
        <!-- 概览 -->
        <template v-if="route === 'overview'">
          <div class="health">
            <span class="health-badge" :class="health.level === 'ok' ? '' : 'warn'">
              <i class="health-dot" />{{ health.label }}
            </span>
            <i class="health-sep" />
            <span class="health-m"><span>上游失败</span><b :class="health.failTone">{{ health.fail }}</b></span>
            <span class="health-m"><span>响应</span><b :class="health.avgTone">{{ health.avg }}</b></span>
            <span class="health-m"><span>凭据</span><b :class="health.credTone">{{ health.cred }}</b></span>
            <span class="health-m"><span>检测空转</span><b :class="health.spinTone">{{ health.spin }}</b></span>
            <span class="health-right">{{ health.spin === '-' ? '还没有检测样本' : '按检测记录算' }}</span>
          </div>

          <div class="kpis">
            <div v-for="k in kpis" :key="k.k" class="kpi">
              <div class="kpi-k">{{ k.k }}</div>
              <div class="kpi-v" :class="k.tone">{{ k.v }}</div>
              <div class="kpi-hint" :class="k.hintTone">
                {{ k.hint }}<template v-if="k.extra"> · {{ k.extra }}</template>
              </div>
            </div>
          </div>

          <div class="grid-main">
            <TrendCard :hourly="data.hourly || []" />
            <ZeroPanel :data="data" :limit="5" @more="go('search')" />
          </div>

          <div class="grid-main">
            <FeedbackPanel :data="fb" compact @done="markDone" @more="go('feedback')" />
            <ClicksPanel :data="data" :limit="5" @more="go('clicks')" />
          </div>
        </template>

        <!-- 待办与反馈 -->
        <template v-else-if="route === 'feedback'">
          <FeedbackPanel :data="fb" @done="markDone" />
        </template>

        <!-- 搜索诊断 -->
        <template v-else-if="route === 'search'">
          <ZeroPanel :data="data" />
          <div class="grid-half">
            <RankCard title="搜索最多的词" tag="供给已经满足的部分" :rows="keywordRows" empty="这个区间还没有搜索" />
            <RankCard title="平台分布" tag="用户在哪几个盘里找" :rows="platformRows" empty="还没有平台数据" />
          </div>
          <div class="grid-half">
            <RankCard title="流量来源" tag="referer 前 10" :rows="refererRows" empty="没有来源记录（多为直接访问）" />
            <RankCard title="热门点击" tag="被点开最多的资源" :rows="targetRows" empty="这个区间还没有点击" />
          </div>
        </template>

        <!-- 资源巡检 -->
        <template v-else-if="route === 'check'">
          <LinkCheckPanel />
        </template>

        <!-- 点击日志 -->
        <template v-else-if="route === 'clicks'">
          <ClicksPanel :data="data" />
        </template>

        <!-- 配置 -->
        <template v-else-if="route === 'accounts'">
          <AccountsPanel />
        </template>

        <template v-else-if="route === 'group'">
          <GroupPanel />
        </template>
      </div>
    </div>
  </div>
</template>
