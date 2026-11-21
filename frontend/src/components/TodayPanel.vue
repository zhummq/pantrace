<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ResultCard from './ResultCard.vue'
import { fetchToday } from '../api.js'

// 今日更新：**只展示 QQ 群里采集进来的资源**。
//
// 这个页面存在的理由不是"再列一遍搜索结果"，而是告诉访客"这个站在动"。
// 一旦混进上游爬来的几千条，它就退化成第二个搜索页，而且把"群里有人分享了
// 什么"这件事彻底淹掉。所以来源固定，不做筛选器。
const props = defineProps({
  // 群弹窗的链接拦截，交给主站统一处理（和搜索结果走同一条路）
  intercept: { type: Boolean, default: false },
})

const emit = defineEmits(['open', 'feedback', 'search', 'group'])

const PAGE_SIZE = 20
// 自动刷新间隔。群里发一条到页面能看到，链路是「攒批 1.5s → 上报 → 入库」，
// 所以一分钟足够跟上节奏；再快只是让浏览器白跑查询。
const REFRESH_MS = 60_000

const items = ref([])
const page = ref(1)
const total = ref(0)
const totalPages = ref(0)
const summary = ref({ total: 0, byPlatform: [] })
const dateText = ref('')
const updatedAt = ref(0)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const loaded = ref(false)

let timer = null
let seq = 0

const empty = computed(() => !loading.value && !refreshing.value && loaded.value && !error.value && !items.value.length)

// 平台分布只留前三：这一行是"今天偏什么盘"的概览，不是报表。
// 全列出来会把标题行挤成两行，反而看不清。
const platformLine = computed(() => {
  const list = (summary.value.byPlatform || []).slice(0, 3)
  return list.map((p) => `${p.name} ${p.count}`).join(' · ')
})

const updatedText = computed(() => {
  if (!updatedAt.value) return ''
  const d = new Date(updatedAt.value * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')} 更新`
})

async function load(targetPage = 1, { silent = false } = {}) {
  const mine = ++seq
  if (silent) refreshing.value = true
  else loading.value = true
  error.value = ''
  try {
    const data = await fetchToday({ page: targetPage, pageSize: PAGE_SIZE })
    if (mine !== seq) return          // 用户快速翻页时丢掉过期响应
    items.value = data.items || []
    total.value = data.pagination?.total || 0
    totalPages.value = data.pagination?.totalPages || 0
    page.value = data.pagination?.page || 1
    summary.value = data.summary || { total: 0, byPlatform: [] }
    dateText.value = data.date || ''
    updatedAt.value = data.updatedAt || 0
    loaded.value = true
  } catch (e) {
    if (mine !== seq) return
    // 静默刷新失败不清列表：已经看到的内容不该因为一次超时就消失
    if (!silent) {
      error.value = e.message || '读取失败，请稍后重试'
      items.value = []
    }
  } finally {
    if (mine === seq) {
      loading.value = false
      refreshing.value = false
    }
  }
}

function manualRefresh() {
  if (refreshing.value || loading.value) return
  load(page.value, { silent: true })
}

function goPage(p) {
  if (p < 1 || (totalPages.value && p > totalPages.value) || p === page.value) return
  load(p)
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function tick() {
  // 标签页在后台时不刷新：那只是白花一次请求，回来时会立刻拉一次
  if (document.hidden) return
  if (loading.value || refreshing.value) return
  load(page.value, { silent: true })
}

function onVisible() {
  if (!document.hidden && loaded.value) load(page.value, { silent: true })
}

onMounted(() => {
  load(1)
  timer = setInterval(tick, REFRESH_MS)
  document.addEventListener('visibilitychange', onVisible)
})

onBeforeUnmount(() => {
  seq += 1
  if (timer) clearInterval(timer)
  document.removeEventListener('visibilitychange', onVisible)
})
</script>

<template>
  <section class="today">
    <header class="today-head">
      <div class="th-main">
        <h2 class="th-title">今日更新</h2>
        <p class="th-desc">
          群里分享的资源，发出后一两秒入库，这里就能看到。<b>只收群主和管理员发的链接。</b>
        </p>
      </div>
      <div class="th-meta mono">
        <span v-if="dateText">{{ dateText }}</span>
        <span v-if="total" class="th-count">{{ total }} 条</span>
        <span v-if="updatedText" class="th-time">{{ updatedText }}</span>
        <button
          class="th-refresh"
          type="button"
          :disabled="refreshing || loading"
          title="重新读取（每分钟也会自动刷新一次）"
          @click="manualRefresh"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 12a9 9 0 1 1-2.64-6.36" />
            <path d="M21 3v6h-6" />
          </svg>
          <span>{{ refreshing ? '读取中' : '刷新' }}</span>
        </button>
      </div>
    </header>

    <p v-if="platformLine" class="th-platforms mono">{{ platformLine }}</p>

    <div v-if="loading" class="today-state">
      <p class="ts-text">正在读取今天的分享…</p>
    </div>

    <div v-else-if="error" class="today-state">
      <p class="ts-title">这次没读出来</p>
      <p class="ts-text">{{ error }}</p>
      <button class="ts-btn" type="button" @click="load(1)">重试</button>
    </div>

    <div v-else-if="empty" class="today-state">
      <p class="ts-title">今天还没人发新东西</p>
      <p class="ts-text">
        群里发了带网盘链接的分享，这里会自动出现，不用手动提交。想找现成的，先去搜。
      </p>
      <div class="ts-actions">
        <button class="ts-btn" type="button" @click="emit('search')">去搜资源</button>
        <button class="ts-link" type="button" @click="emit('group')">进群交流</button>
      </div>
    </div>

    <div v-else class="panel">
      <div class="result-list">
        <ResultCard
          v-for="item in items"
          :key="item.resourceId || item.shareUrl"
          :item="item"
          keyword=""
          :intercept="intercept"
          hide-unknown-size
          @feedback="emit('feedback', $event)"
          @open="emit('open', $event)"
        />
      </div>
    </div>

    <div v-if="!loading && totalPages > 1" class="today-pager">
      <button class="pg" type="button" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
      <span class="pg-info mono">{{ page }} / {{ totalPages }}</span>
      <button class="pg" type="button" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</button>
    </div>
  </section>
</template>

<style scoped>
.today {
  padding-top: var(--sp-2);
}

/* ---------- 头部 ---------- */

.today-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--sp-3);
  padding-bottom: var(--sp-3);
}

.th-title {
  font-size: var(--text-xl);
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--ink);
}

.th-desc {
  margin-top: 6px;
  max-width: 46em;
  font-size: var(--text-sm);
  line-height: 1.6;
  color: var(--muted);
  text-wrap: pretty;
}

.th-desc b {
  font-weight: 600;
  color: var(--ink-2);
}

.th-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--text-xs);
  color: var(--faint);
}

/* 总数给品牌色：这一行里唯一需要被读到的数字 */
.th-count {
  color: var(--brand-ink);
}

.th-time {
  color: var(--faint);
}

.th-refresh {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 9px;
  border: 1px solid var(--brand-ring);
  border-radius: var(--radius-sm);
  background: transparent;
  font-size: var(--text-xs);
  color: var(--brand-ink);
}

.th-refresh:hover:not(:disabled) {
  border-color: var(--brand);
  background: var(--brand);
  color: var(--on-brand);
}

.th-refresh:disabled {
  opacity: 0.45;
  cursor: default;
}

/* 平台分布：一行灰字，和日期同级，不抢标题。
   用 --muted 而不是 --faint：这行是"今天偏哪个盘"的唯一线索，
   压到 faint 就只剩装饰作用，得凑近才看得清。 */
.th-platforms {
  margin-bottom: var(--sp-4);
  font-size: var(--text-xs);
  color: var(--muted);
}

/* ---------- 面板 ---------- */

.panel {
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--inset-top), var(--shadow-md);
}

.result-list {
  display: flex;
  flex-direction: column;
}

/* ---------- 空 / 错 / 加载 ---------- */

.today-state {
  padding: var(--sp-8) var(--sp-5);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--surface);
  text-align: center;
}

.ts-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--ink);
}

.ts-text {
  margin-top: var(--sp-2);
  font-size: var(--text-base);
  line-height: 1.7;
  color: var(--muted);
  text-wrap: pretty;
}

.ts-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-3);
  margin-top: var(--sp-5);
}

.ts-btn {
  height: 34px;
  padding: 0 18px;
  border: 1px solid var(--brand);
  border-radius: var(--radius-pill);
  background: var(--brand);
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--on-brand);
  box-shadow: var(--shadow-brand);
}

.ts-btn:hover {
  background: var(--brand-hover);
}

.ts-link {
  height: 34px;
  padding: 0 4px;
  border: none;
  background: transparent;
  font-size: var(--text-base);
  color: var(--muted);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.ts-link:hover {
  color: var(--ink);
}

/* ---------- 分页 ---------- */

.today-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  margin-top: var(--sp-5);
}

.pg {
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  font-size: var(--text-sm);
  color: var(--muted);
}

.pg:hover:not(:disabled) {
  color: var(--ink);
  border-color: var(--line-2);
  background: var(--surface-3);
}

.pg:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.pg-info {
  padding: 0 var(--sp-2);
  font-size: var(--text-xs);
  color: var(--faint);
}
</style>
