<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Gate from './components/Gate.vue'
import StatGrid from './components/StatGrid.vue'
import FeedbackPanel from './components/FeedbackPanel.vue'
import AccountsPanel from './components/AccountsPanel.vue'
import GroupPanel from './components/GroupPanel.vue'
import InsightsPanel from './components/InsightsPanel.vue'
import * as api from './api'
import { ms, pct } from './format'

const RANGES = [
  { days: 1, label: '今日' },
  { days: 7, label: '近 7 天' },
  { days: 30, label: '近 30 天' },
]

const days = ref(7)
const data = ref({})
const ready = ref(false)
const needToken = ref(false)
const gateMsg = ref('')
const errorMsg = ref('')
const lastLoaded = ref('')

const feedbackPanel = ref(null)
const accountsPanel = ref(null)
const groupPanel = ref(null)

const o = computed(() => data.value.overview || {})
const rangeText = computed(() => (days.value === 1 ? '今天' : `近 ${days.value} 天`))

const overviewStats = computed(() => [
  { k: '搜索次数', v: Number(o.value.searches) || 0, hi: true, hint: '业务核心量' },
  { k: '点击转化率', v: pct(o.value.ctr), hint: '点开资源 / 搜索次数' },
  {
    k: '零结果率',
    v: pct(o.value.zero_rate),
    hi: (Number(o.value.zero_rate) || 0) > 0.3,
    hint: '搜了但没结果 · 越低越好',
  },
])

const subStats = computed(() => [
  { k: '访问 PV', v: Number(o.value.pv) || 0 },
  { k: '访客 UV', v: Number(o.value.uv) || 0 },
  { k: '点击次数', v: Number(o.value.clicks) || 0 },
  { k: '平均停留', v: ms(o.value.dwell_ms) },
])

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

function loadAll() {
  loadStats()
  for (const p of [feedbackPanel, accountsPanel]) {
    if (p.value) p.value.reload()
  }
}

function switchRange(d) {
  if (days.value === d) return
  days.value = d
  loadStats()
}

function onToken(value) {
  api.setToken(value)
  gateMsg.value = ''
  // ready 变 true 后子面板会挂载并各自拉数据，这里不用重复请求
  loadStats()
}

function logout() {
  api.setToken('')
  needToken.value = true
  ready.value = false
  gateMsg.value = ''
  data.value = {}
}

let timer = null
onMounted(() => {
  api.setToken(api.readToken())
  loadStats()
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible' || needToken.value) return
    loadStats(true)
    // 只静默刷新列表，不动用户正在编辑的表单和群配置
    if (feedbackPanel.value) feedbackPanel.value.reload()
  }, 60000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <Gate v-if="needToken" :message="gateMsg" @submit="onToken" />

  <div v-else class="wrap">
    <header class="top">
      <div>
        <h1>盘迹 · 访问记录</h1>
        <div class="sub">
          {{ rangeText }} · 自动刷新 60 秒 · 机器人流量已剔除
          <template v-if="lastLoaded"> · 更新于 {{ lastLoaded }}</template>
        </div>
      </div>
      <div class="controls">
        <div class="seg">
          <button
            v-for="r in RANGES"
            :key="r.days"
            :class="{ on: days === r.days }"
            @click="switchRange(r.days)"
          >{{ r.label }}</button>
        </div>
        <button class="ghost" @click="loadAll">刷新</button>
        <button class="ghost" @click="logout">改口令</button>
      </div>
    </header>

    <div v-if="errorMsg" class="err">读取失败：{{ errorMsg }}</div>
    <div v-if="!ready" class="empty">载入中…</div>

    <template v-if="ready">
      <StatGrid :items="overviewStats" size="big" />
      <StatGrid :items="subStats" />

      <FeedbackPanel ref="feedbackPanel" :days="days" />
      <AccountsPanel ref="accountsPanel" />
      <GroupPanel ref="groupPanel" />
      <InsightsPanel :data="data" />
    </template>
  </div>
</template>
