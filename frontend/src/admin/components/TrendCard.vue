<script setup>
import { computed } from 'vue'

const props = defineProps({
  hourly: { type: Array, default: () => [] },
})

// 后端给的是 24 个桶（按配置时区），长度不对就补齐，别让图塌掉
const rows = computed(() => {
  const src = props.hourly.length === 24 ? props.hourly : Array(24).fill(0)
  const nums = src.map((n) => Number(n) || 0)
  const max = Math.max(...nums, 1)
  return nums.map((n, h) => ({ h, n, pct: Math.max(2, Math.round((n / max) * 100)) }))
})

const total = computed(() => rows.value.reduce((s, r) => s + r.n, 0))
const peak = computed(() =>
  rows.value.reduce((m, r) => (r.n > m.n ? r : m), rows.value[0] || { h: 0, n: 0 })
)
</script>

<template>
  <section class="card">
    <header class="card-head">
      <div class="card-title">搜索量分布<span class="card-tag">24 小时 · 按小时分桶</span></div>
      <div class="card-extra">
        <span v-if="total" class="card-sub">
          峰值 {{ String(peak.h).padStart(2, '0') }}:00 · {{ peak.n }} 次 · 合计 {{ total }}
        </span>
      </div>
    </header>
    <div class="card-body">
      <div v-if="!total" class="empty">这个区间还没有搜索记录</div>
      <template v-else>
        <div class="hours">
          <div
            v-for="r in rows"
            :key="r.h"
            class="col"
            :style="{ height: r.pct + '%' }"
            :title="`${String(r.h).padStart(2, '0')}:00 · ${r.n} 次`"
          />
        </div>
        <div class="hours-label">
          <span v-for="r in rows" :key="r.h">{{ r.h % 3 === 0 ? String(r.h).padStart(2, '0') : '' }}</span>
        </div>
      </template>
    </div>
  </section>
</template>
