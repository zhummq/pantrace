<script setup>
import { computed } from 'vue'
import Panel from './Panel.vue'
import BarList from './BarList.vue'
import StatGrid from './StatGrid.vue'
import { KIND_NAMES, clock, ms, pct, platformName } from '../format'

const props = defineProps({
  data: { type: Object, default: () => ({}) },
})

const o = computed(() => props.data.overview || {})
const hourly = computed(() => props.data.hourly || [])
const recent = computed(() => props.data.recent || [])

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

function contentOf(r) {
  if (r.kind === 'click' || r.kind === 'copy_pwd') return r.target || '-'
  if (['search', 'filter', 'page', 'retry'].includes(r.kind)) return r.query || '-'
  return '-'
}

function kindClass(r) {
  if (r.kind === 'search' && !r.ok) return 'kind fail'
  return 'kind ' + r.kind
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

  <Panel title="最近行为流水" tag="最新 30 条">
    <div v-if="!recent.length" class="empty">这个区间没有行为记录</div>
    <table v-else class="stream">
      <thead>
        <tr><th>时间</th><th>行为</th><th>内容</th><th>平台</th><th>设备</th><th>IP</th></tr>
      </thead>
      <tbody>
        <tr v-for="(r, i) in recent" :key="i">
          <td class="t">{{ clock(r.ts) }}</td>
          <td><span :class="kindClass(r)">{{ KIND_NAMES[r.kind] || r.kind }}</span></td>
          <td class="ellip" :title="contentOf(r)">{{ contentOf(r) }}</td>
          <td>{{ platformName(r.platform) }}</td>
          <td>{{ r.device === 'mobile' ? '手机' : '桌面' }}</td>
          <td class="t">{{ r.ip || '-' }}</td>
        </tr>
      </tbody>
    </table>
  </Panel>
</template>
