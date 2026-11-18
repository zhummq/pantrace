<script setup>
import { computed, ref } from 'vue'
import BarList from './BarList.vue'
import FeedbackDetail from './FeedbackDetail.vue'
import { shortTime } from '../format'

/**
 * 反馈与待办。
 *
 * 数据由 App 统一持有（一次请求同时喂导航角标和这个面板），这里只负责渲染：
 * - compact：概览页的待办卡，只列待处理的，点一条跳整页
 * - 完整：左队列右详情。列表负责扫，详情负责判，"标为已处理"只在详情里出现一次
 */
const props = defineProps({
  data: { type: Object, default: () => ({}) },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['done', 'more'])

const selectedId = ref(0)

const counts = computed(() => props.data.counts || {})
const items = computed(() => props.data.items || [])
const pending = computed(() => Number(counts.value.pending) || 0)
const done = computed(() => Number(counts.value.done) || 0)
const total = computed(() => Number(counts.value.total) || 0)

const pendingItems = computed(() => items.value.filter((i) => i.status === 'new'))
const topPending = computed(() => pendingItems.value.slice(0, 6))

const selected = computed(
  () => items.value.find((i) => i.id === selectedId.value) || items.value[0] || null
)

const requests = computed(() =>
  (props.data.requests || []).map((r) => ({ label: r.q, n: r.n }))
)
const flagged = computed(() =>
  (props.data.flagged || []).map((f) => ({ label: f.target || f.url, title: f.url, n: f.n }))
)

function pick(it) {
  selectedId.value = it.id
}

function title(it) {
  return it.target || it.message || '（用户没填标题）'
}
</script>

<template>
  <!-- 概览：只列待处理，动作在整页里做 -->
  <section v-if="compact" class="card">
    <header class="card-head">
      <div class="card-title">
        待办与反馈<span class="card-tag" :class="{ bad: pending }">
          {{ pending ? `${pending} 条待处理` : '没有待办' }}
        </span>
      </div>
      <div class="card-extra"><a @click="emit('more')">全部处理</a></div>
    </header>
    <div class="card-body tight">
      <div v-if="!topPending.length" class="empty" style="padding: 14px 0">
        没有待处理的反馈——这就是最好的状态
      </div>
      <div v-for="it in topPending" :key="it.id" class="q-row" @click="emit('more')">
        <span class="fb-kind" :class="it.kind">{{ it.kindName }}</span>
        <span class="q-title">{{ title(it) }}</span>
        <span class="q-meta">{{ shortTime(it.ts) }}</span>
      </div>
      <div v-if="total" class="card-sub" style="margin-top: 10px">
        区间共 {{ total }} 条 · 已处理 {{ done }} 条
      </div>
    </div>
  </section>

  <!-- 整页：队列 + 详情 -->
  <template v-else>
    <div class="grid-main">
      <section class="card">
        <header class="card-head">
          <div class="card-title">
            反馈队列<span class="card-tag">{{ items.length }} 条 · 待办在前</span>
          </div>
        </header>
        <div class="card-body tight">
          <div v-if="!items.length" class="empty">这个区间没有反馈</div>
          <div v-else style="max-height: 520px; overflow-y: auto; padding-right: 2px">
            <div
              v-for="it in items"
              :key="it.id"
              class="q-row"
              :class="{ on: selected && selected.id === it.id, done: it.status === 'done' }"
              @click="pick(it)"
            >
              <span class="fb-kind" :class="it.kind">{{ it.kindName }}</span>
              <span class="q-title">{{ title(it) }}</span>
              <span class="q-meta">{{ shortTime(it.ts) }}</span>
            </div>
          </div>
        </div>
      </section>

      <FeedbackDetail :item="selected" @done="emit('done', $event)" />
    </div>

    <div class="grid-main">
      <section class="card">
        <header class="card-head">
          <div class="card-title">用户在要什么<span class="card-tag">该补什么源</span></div>
        </header>
        <div class="card-body">
          <BarList :rows="requests" fill="warn" empty="还没有人提需求" />
        </div>
      </section>

      <section class="card">
        <header class="card-head">
          <div class="card-title">已被自动隐藏<span class="card-tag">≥2 人报失效后下线</span></div>
        </header>
        <div class="card-body">
          <BarList :rows="flagged" empty="暂时没有被多人报失效的链接" />
          <div class="card-sub" style="margin-top: 10px">复核通过可在「资源巡检」里恢复</div>
        </div>
      </section>
    </div>
  </template>
</template>
