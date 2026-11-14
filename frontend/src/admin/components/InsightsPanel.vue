<script setup>
import { computed } from 'vue'
import Panel from './Panel.vue'
import BarList from './BarList.vue'
import StatGrid from './StatGrid.vue'
import { ms, pct, platformName } from '../format'

const props = defineProps({
  data: { type: Object, default: () => ({}) },
})

const o = computed(() => props.data.overview || {})
const hourly = computed(() => props.data.hourly || [])

const health = computed(() => [
  { k: '失败率', v: pct(o.value.fail_rate) },
  { k: '上游耗时', v: ms(o.value.avg_ms) },
  { k: '缓存命中率', v: pct(o.value.cache_rate) },
  { k: '机器人搜索', v: Number(o.value.bot_searches) || 0 },
])

const keywordRows = computed(() =>
  (props.data.keywords || []).map((r) => ({ label: r.q, n: r.n }))
)
const zeroRows = computed(() =>
  (props.data.zero_keywords || []).map((r) => ({ label: r.q, n: r.n }))
)
const targetRows = computed(() => (props.data.targets || []).map((r) => ({ label: r.t, n: r.n })))
const refererRows = computed(() => (props.data.referers || []).map((r) => ({ label: r.r, n: r.n })))
const platformRows = computed(() =>
  (props.data.platforms || []).map((r) => ({ label: platformName(r.key), n: r.n }))
)
const visitorRows = computed(() => [
  { label: '新客', n: Number(o.value.new_visitors) || 0 },
  { label: '回头客', n: Number(o.value.returning_visitors) || 0 },
])

// 24 根柱子按最大值归一
const hourMax = computed(() => hourly.value.reduce((m, n) => Math.max(m, Number(n) || 0), 1))
function hourHeight(n) {
  return Math.max(2, Math.round(((Number(n) || 0) / hourMax.value) * 100)) + '%'
}
</script>

<template>
  <Panel title="健康指标" tag="出问题才看">
    <StatGrid :items="health" />
    <div class="rank">
      <h2>24 小时搜索分布 <span>按本机时区</span></h2>
      <div class="hours">
        <div
          v-for="(n, h) in hourly"
          :key="h"
          class="col"
          :style="{ height: hourHeight(n) }"
          :title="h + ' 点：' + (n || 0) + ' 次'"
        ></div>
      </div>
      <div class="hours-label">
        <span v-for="h in 24" :key="h">{{ (h - 1) % 6 === 0 ? h - 1 : '' }}</span>
      </div>
    </div>
  </Panel>

  <Panel title="决策参考" tag="补词 / 引流时看">
    <div class="grid2">
      <section class="rank">
        <h2>搜索词 TOP <span>共 {{ keywordRows.length }} 个词</span></h2>
        <BarList :rows="keywordRows" empty="还没有搜索记录" />
      </section>
      <section class="rank">
        <h2>搜不到的词 <span>该补词库了</span></h2>
        <BarList :rows="zeroRows" fill="warn" empty="这个区间没有搜不到的词，好事" />
      </section>
    </div>
    <div class="grid2">
      <section class="rank">
        <h2>被点开最多的资源</h2>
        <BarList :rows="targetRows" fill="alt" empty="还没有点击记录" />
      </section>
      <section class="rank">
        <h2>来源站 <span>从哪引流来的</span></h2>
        <BarList :rows="refererRows" empty="都是直接访问，没有来源站" />
      </section>
    </div>
    <div class="grid2">
      <section class="rank">
        <h2>平台分布 <span>按搜索次数</span></h2>
        <BarList :rows="platformRows" empty="还没有平台筛选记录" />
      </section>
      <section class="rank">
        <h2>新客与回头客 <span>访客口径</span></h2>
        <BarList :rows="visitorRows" />
      </section>
    </div>
  </Panel>
</template>
