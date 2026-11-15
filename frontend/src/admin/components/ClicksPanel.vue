<script setup>
import { computed } from 'vue'
import Panel from './Panel.vue'
import { platformName, shortTime } from '../format'

const props = defineProps({
  data: { type: Object, default: () => ({}) },
})

// 后端 summary().recent_clicks：最新 100 条点击（bot 已剔除）。
// url 是后端解 /go/ token 后的真实网盘链接；ourUrl 是我们转存出去的那条，
// 它只活 30 分钟（mirrors.expires_at），所以要把剩余有效期说清楚。
const rows = computed(() => props.data.recent_clicks || [])

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
  <Panel title="点击记录" tag="用户点开了什么资源" :open="true">
    <div v-if="!rows.length" class="empty">这个区间还没有点击记录</div>
    <div v-for="(r, i) in rows" :key="i" class="fb-item">
      <div class="fb-head">
        <span class="ck-pan">{{ platformName(r.platform) }}</span>
        <span class="fb-title" :title="r.target">{{ r.target || '（无标题）' }}</span>
        <span class="t">{{ shortTime(r.ts) }}</span>
      </div>
      <div class="fb-meta">
        <span class="ck-lbl">原链接</span>
        <a
          v-if="r.url"
          class="fb-link"
          :href="r.url"
          target="_blank"
          rel="noopener noreferrer"
        >{{ r.url }}</a>
        <span v-else>链接未记录（旧版埋点数据）</span>
      </div>
      <div class="fb-meta">
        <template v-if="r.ourUrl">
          <span class="ck-lbl">转存链接</span>
          <a
            class="fb-link"
            :href="r.ourUrl"
            target="_blank"
            rel="noopener noreferrer"
          >{{ r.ourUrl }}</a>
          <span class="ck-ttl" :class="{ bad: expired(r) }">{{ ttlText(r) }}</span>
        </template>
        <template v-else-if="r.ourStatus === 'fail'">
          <span class="ck-lbl">转存链接</span>转存失败，用户拿到的是原链接
        </template>
        <template v-else>
          <span class="ck-lbl">转存链接</span>尚未转存
        </template>
      </div>
      <div class="fb-meta">
        <template v-if="r.query">搜索词：{{ r.query }} · </template>{{ r.device === 'mobile' ? '手机' : '桌面' }}<template v-if="r.ip"> · {{ r.ip }}</template>
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

.ck-lbl {
  display: inline-block;
  min-width: 48px;
  margin-right: 4px;
  color: var(--muted);
}

.ck-ttl {
  margin-left: 6px;
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
