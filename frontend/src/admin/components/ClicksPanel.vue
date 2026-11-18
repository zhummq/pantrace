<script setup>
import { computed } from 'vue'
import Panel from './Panel.vue'
import { platformName, shortTime } from '../format'

/**
 * 点击记录：用户点开了哪条资源。
 *
 * 每条只留三个有用的事实——点的是什么、原链接、我们给出去的转存链接还能用多久。
 * IP 已经脱敏成 a.b.c.x，设备占比也做不出动作，早先占了两行位置。
 */
const props = defineProps({
  data: { type: Object, default: () => ({}) },
  limit: { type: Number, default: 0 },
})

const emit = defineEmits(['more'])

const all = computed(() => props.data.recent_clicks || [])
const rows = computed(() => (props.limit > 0 ? all.value.slice(0, props.limit) : all.value))
const storable = computed(() => all.value.filter((r) => r.ourUrl).length)

function expired(r) {
  const exp = Number(r.ourExpiresAt) || 0
  return exp > 0 && exp - Date.now() / 1000 <= 0
}

function ttlText(r) {
  const exp = Number(r.ourExpiresAt) || 0
  if (exp <= 0) return '有效期未知'
  const left = exp - Math.floor(Date.now() / 1000)
  if (left <= 0) return '已过期'
  if (left < 60) return '不到 1 分钟'
  return `剩 ${Math.round(left / 60)} 分钟`
}
</script>

<template>
  <Panel
    title="最近点击"
    :tag="all.length ? `${all.length} 条 · ${storable} 条给了转存链接` : '还没有点击'"
  >
    <template v-if="limit > 0" #extra>
      <a v-if="all.length > limit" @click="emit('more')">查看全部</a>
    </template>

    <div v-if="!rows.length" class="empty">这个区间还没有点击记录</div>

    <div v-for="(r, i) in rows" :key="i" class="clk" :class="{ more: i === rows.length - 1 }">
      <div class="fb-head">
        <span class="ck-pan">{{ platformName(r.platform) }}</span>
        <span class="fb-title" :title="r.target">{{ r.target || '（无标题）' }}</span>
        <span class="t">{{ shortTime(r.ts) }}</span>
      </div>

      <div class="clk-line">
        <span class="ck-lbl">原链接</span>
        <a v-if="r.url" class="fb-link" :href="r.url" target="_blank" rel="noopener noreferrer">{{ r.url }}</a>
        <span v-else class="clk-none">旧版埋点没记这条</span>
      </div>

      <div class="clk-line">
        <span class="ck-lbl">转存链接</span>
        <template v-if="r.ourUrl">
          <a class="fb-link" :href="r.ourUrl" target="_blank" rel="noopener noreferrer">{{ r.ourUrl }}</a>
          <span class="ck-ttl" :class="{ bad: expired(r) }">{{ ttlText(r) }}</span>
        </template>
        <span v-else-if="r.ourStatus === 'fail'" class="clk-none">转存失败，用户拿到的是原链接</span>
        <span v-else class="clk-none">未转存，直出原链接</span>
      </div>

      <div v-if="r.query" class="clk-line">
        <span class="ck-lbl">来源</span>
        <span class="clk-none">搜「{{ r.query }}」找过来的</span>
      </div>
    </div>
  </Panel>
</template>

<style scoped>
.ck-pan {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 5px;
  background: var(--brand-soft);
  color: var(--brand);
  flex-shrink: 0;
}

.clk {
  padding: 9px 0;
  border-bottom: 1px solid #f2f5f8;
}
.clk.more { border-bottom: 0; }
.clk:last-child { border-bottom: 0; }

.clk-line {
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--muted);
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
}

.ck-lbl {
  min-width: 42px;
  color: var(--faint);
  flex-shrink: 0;
}

.ck-none { color: var(--faint); }

.ck-ttl {
  font-size: 10.5px;
  padding: 0 6px;
  border-radius: 999px;
  background: #eef2f6;
  color: var(--muted);
  white-space: nowrap;
}

.ck-ttl.bad {
  background: #fdecea;
  color: var(--red);
}
</style>
