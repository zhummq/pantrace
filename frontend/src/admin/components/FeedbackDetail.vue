<script setup>
import { computed } from 'vue'
import { platformName, shortTime } from '../format'

const props = defineProps({
  item: { type: Object, default: null },
})
defineEmits(['done'])

const isHttp = computed(() => /^https?:\/\//i.test((props.item && props.item.url) || ''))

// 只留判断用得上的上下文。IP 已脱敏成 a.b.c.x，照着它做不了任何事，早先一直在占位置。
const context = computed(() => {
  const it = props.item
  if (!it) return []
  const out = []
  if (it.query) out.push('搜「' + it.query + '」')
  if (it.platform) out.push(platformName(it.platform))
  if (it.device) out.push(it.device === 'mobile' ? '手机' : '桌面')
  if (it.contact) out.push('联系：' + it.contact)
  return out
})
</script>

<template>
  <section class="card">
    <header class="card-head">
      <div class="card-title">详情</div>
      <div class="card-extra">
        <span v-if="item" class="card-sub">{{ shortTime(item.ts) }} · #{{ item.id }}</span>
      </div>
    </header>

    <div class="card-body" style="display: flex; flex-direction: column">
      <div v-if="!item" class="empty">左边选一条</div>

      <template v-else>
        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px">
          <span class="fb-kind" :class="item.kind">{{ item.kindName }}</span>
          <span v-if="item.status === 'done'" class="quick-badge ok">已处理</span>
        </div>

        <div style="font-size: 15px; font-weight: 600; line-height: 1.45">
          {{ item.target || '（用户没填标题）' }}
        </div>

        <template v-if="item.message">
          <div class="detail-label">用户描述</div>
          <div class="detail-msg">{{ item.message }}</div>
        </template>

        <template v-if="context.length">
          <div class="detail-label">上下文</div>
          <div class="detail-msg" style="color: var(--muted); font-size: 12px">
            {{ context.join(' · ') }}
          </div>
        </template>

        <template v-if="isHttp">
          <div class="detail-label">原链接</div>
          <a class="detail-url" :href="item.url" target="_blank" rel="noopener noreferrer">{{ item.url }}</a>
        </template>

        <template v-if="item.status === 'done'">
          <div class="detail-label">处理结果</div>
          <div class="detail-msg" style="color: var(--muted)">{{ item.note || '已标为处理完成' }}</div>
        </template>

        <div class="detail-actions">
          <button
            v-if="item.status === 'new'"
            class="btn-primary"
            @click="$emit('done', item.id)"
          >标为已处理</button>
          <a
            v-if="isHttp"
            class="btn-ghost"
            style="display: inline-flex; align-items: center; text-decoration: none"
            :href="item.url"
            target="_blank"
            rel="noopener noreferrer"
          >打开原链接</a>
          <span class="card-sub" style="margin-left: auto">处理后自动归档</span>
        </div>
      </template>
    </div>
  </section>
</template>
