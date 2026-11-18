<script setup>
import { computed } from 'vue'

const props = defineProps({
  title: { type: String, required: true },
  tag: { type: String, default: '' },
  rows: { type: Array, default: () => [] },
  empty: { type: String, default: '没有数据' },
})

const max = computed(() => props.rows.reduce((m, r) => Math.max(m, Number(r.n) || 0), 1))

function width(n) {
  return Math.max(3, Math.round(((Number(n) || 0) / max.value) * 100)) + '%'
}
</script>

<template>
  <section class="card">
    <header class="card-head">
      <div class="card-title">{{ title }}<span v-if="tag" class="card-tag">{{ tag }}</span></div>
    </header>
    <div class="card-body">
      <div v-if="!rows.length" class="empty">{{ empty }}</div>
      <div v-for="(r, i) in rows" :key="i" class="tbl-row">
        <span class="tbl-idx">{{ String(i + 1).padStart(2, '0') }}</span>
        <span class="tbl-name" :title="r.label">{{ r.label }}</span>
        <span class="mini-bar"><i :style="{ width: width(r.n) }" /></span>
        <span class="tbl-num mute">{{ r.n }}</span>
      </div>
    </div>
  </section>
</template>
