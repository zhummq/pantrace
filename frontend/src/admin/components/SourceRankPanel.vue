<script setup>
import { computed } from 'vue'

const props = defineProps({
  data: { type: Object, default: () => ({}) },
  loading: { type: Boolean, default: false },
})

const rows = computed(() => props.data.rows || [])
const total = computed(() => Number(props.data.total) || 0)

// 频道和插件拆成两张榜：它们不是一种东西，混在一起排，插件名会被频道名淹没。
function toRow(r) {
  return { label: r.name || r.key, n: Number(r.score) || 0 }
}
const tgRows = computed(() => rows.value.filter((r) => r.kind === 'tg').map(toRow))
const pluginRows = computed(() => rows.value.filter((r) => r.kind === 'plugin').map(toRow))

// 和 RankCard 同一套条宽算法；不为了一个函数去改公共组件。
function barWidth(n, list) {
  const max = list.reduce((m, r) => Math.max(m, Number(r.n) || 0), 1)
  return Math.max(3, Math.round(((Number(n) || 0) / max) * 100)) + '%'
}

const sinceText = computed(() => {
  const ts = Number(props.data.since) || 0
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
})
</script>

<template>
  <section class="card">
    <header class="card-head">
      <div class="card-title">
        来源贡献榜
        <span v-if="sinceText" class="card-tag">自 {{ sinceText }} 起 · 累计 {{ total }} 次点击</span>
      </div>
    </header>
    <div class="card-body">
      <p class="hint">用户点开一条资源，就给它背后的频道或插件加一分。只统计启用本功能之后的点击。</p>
      <div v-if="loading" class="empty">加载中…</div>
      <template v-else>
        <div class="sub">TG 频道</div>
        <div v-if="!tgRows.length" class="empty small">还没有频道得分</div>
        <div v-for="(r, i) in tgRows" :key="'t' + i" class="tbl-row">
          <span class="tbl-idx">{{ String(i + 1).padStart(2, '0') }}</span>
          <span class="tbl-name" :title="r.label">{{ r.label }}</span>
          <span class="mini-bar"><i :style="{ width: barWidth(r.n, tgRows) }" /></span>
          <span class="tbl-num mute">{{ r.n }}</span>
        </div>
        <div class="sub second">插件</div>
        <div v-if="!pluginRows.length" class="empty small">还没有插件得分</div>
        <div v-for="(r, i) in pluginRows" :key="'p' + i" class="tbl-row">
          <span class="tbl-idx">{{ String(i + 1).padStart(2, '0') }}</span>
          <span class="tbl-name" :title="r.label">{{ r.label }}</span>
          <span class="mini-bar"><i :style="{ width: barWidth(r.n, pluginRows) }" /></span>
          <span class="tbl-num mute">{{ r.n }}</span>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.hint {
  margin: 0 0 14px;
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--muted);
}
.sub {
  margin: 2px 0 8px;
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--muted);
}
.sub.second {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--line);
}
.empty.small {
  padding: 10px 0;
}
</style>
