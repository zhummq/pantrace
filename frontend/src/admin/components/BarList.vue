<script setup>
import { computed } from 'vue'

const props = defineProps({
  rows: { type: Array, default: () => [] },
  fill: { type: String, default: '' },
  empty: { type: String, default: '暂无数据' },
})

const max = computed(() => props.rows.reduce((m, r) => Math.max(m, Number(r.n) || 0), 1))

function width(n) {
  return Math.max(2, Math.round(((Number(n) || 0) / max.value) * 100)) + '%'
}
</script>

<template>
  <div v-if="!rows.length" class="empty">{{ empty }}</div>
  <div v-else>
    <div v-for="(r, i) in rows" :key="r.key || r.label || i" class="bar-row" :title="r.title || r.label">
      <span class="name">{{ r.label }}</span>
      <span class="track"><span class="fill" :class="fill" :style="{ width: width(r.n) }"></span></span>
      <span class="n">{{ r.n }}</span>
      <slot name="action" :row="r" />
    </div>
  </div>
</template>
