<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import Panel from './Panel.vue'
import BarList from './BarList.vue'
import StatGrid from './StatGrid.vue'
import * as api from '../api'
import { platformName, shortTime } from '../format'

const props = defineProps({
  days: { type: Number, default: 7 },
})

const panel = ref(null)
const loading = ref(true)
const error = ref('')
const data = ref(null)
const autoClosed = ref(false)

const counts = computed(() => (data.value && data.value.counts) || {})
const pending = computed(() => Number(counts.value.pending) || 0)
const done = computed(() => Number(counts.value.done) || 0)
const total = computed(() => Number(counts.value.total) || 0)
const items = computed(() => (data.value && data.value.items) || [])
const requests = computed(() =>
  ((data.value && data.value.requests) || []).map((r) => ({ label: r.q, n: r.n }))
)
const flagged = computed(() =>
  ((data.value && data.value.flagged) || []).map((f) => ({
    label: f.target || f.url,
    title: f.url,
    n: f.n,
  }))
)
const tag = computed(() => (pending.value ? `${pending.value} 条待处理` : '没有待办'))
const stats = computed(() => [
  { k: '待处理', v: pending.value },
  { k: '已处理', v: done.value },
  { k: '区间合计', v: total.value },
])

async function reload() {
  loading.value = true
  try {
    data.value = await api.feedback(props.days)
    error.value = ''
    // 没待办就别占首屏；只自动收一次，用户自己展开后尊重他的选择
    if (!pending.value && !autoClosed.value) {
      autoClosed.value = true
      if (panel.value) panel.value.close()
    }
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function markDone(id) {
  try {
    await api.markFeedbackDone(id)
    await reload()
  } catch (e) {
    error.value = e.message
  }
}

function context(it) {
  const ctx = []
  if (it.query) ctx.push('关键词：' + it.query)
  if (it.platform) ctx.push(platformName(it.platform))
  if (it.ip) ctx.push(it.ip)
  ctx.push(it.device === 'mobile' ? '手机' : '桌面')
  return ctx.join(' · ')
}

function isHttp(url) {
  return /^https?:\/\//i.test(url || '')
}

onMounted(reload)
watch(() => props.days, reload)
defineExpose({ reload })
</script>

<template>
  <Panel ref="panel" title="用户反馈与求资源" :tag="tag" :open="true">
    <StatGrid :items="stats" />

    <div v-if="error" class="err">反馈读取失败：{{ error }}</div>

    <div class="grid2">
      <section class="rank">
        <h2>需求清单 <span>该补什么源</span></h2>
        <BarList :rows="requests" fill="warn" empty="还没有人提需求" />
      </section>
      <section class="rank">
        <h2>已被自动隐藏 <span>≥2 人报失效</span></h2>
        <BarList :rows="flagged" empty="暂时没有被多人报失效的链接" />
      </section>
    </div>

    <div class="rank" style="margin-top: 16px">
      <h2>反馈明细 <span>新提交在前，共 {{ items.length }} 条</span></h2>
      <div v-if="!items.length" class="empty">这个区间没有反馈</div>
      <div v-for="it in items" :key="it.id" class="fb-item" :class="{ done: it.status === 'done' }">
        <div class="fb-head">
          <span class="fb-kind" :class="it.kind">{{ it.kindName }}</span>
          <span v-if="it.target" class="fb-title" :title="it.target">{{ it.target }}</span>
          <span class="t">{{ shortTime(it.ts) }}</span>
        </div>
        <div v-if="it.message" class="fb-msg">{{ it.message }}</div>
        <div v-if="isHttp(it.url)" class="fb-meta">
          <a class="fb-link" :href="it.url" target="_blank" rel="noopener noreferrer">{{ it.url }}</a>
        </div>
        <div class="fb-meta">
          {{ context(it) }}<template v-if="it.contact"> · 联系：{{ it.contact }}</template>
        </div>
        <div v-if="it.status === 'new'" class="fb-actions">
          <button class="fb-btn" @click="markDone(it.id)">标为已处理</button>
        </div>
        <div v-else class="fb-meta" style="margin-top: 7px">
          已处理<template v-if="it.note">：{{ it.note }}</template>
        </div>
      </div>
    </div>
  </Panel>
</template>
