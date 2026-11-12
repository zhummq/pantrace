<script setup>
import { computed, onMounted, ref } from 'vue'
import ResultCard from './components/ResultCard.vue'
import FeedbackDialog from './components/FeedbackDialog.vue'
import { fetchPlatforms, searchResources } from './api.js'
import { initTracking, track } from './track.js'

const PAGE_SIZE = 20

const keyword = ref('')
const lastKeyword = ref('')
const activePlatform = ref('')
const platforms = ref([])

const items = ref([])
const page = ref(1)
const totalPages = ref(0)
const total = ref(0)
const tookMs = ref(0)

const loading = ref(false)
const error = ref('')
const searched = ref(false)

const showEmpty = computed(() => !loading.value && !error.value && searched.value && items.value.length === 0)

// ---- 反馈 / 求资源 ----
// 一个弹层三种形态，共用一个提交接口。上下文（当前关键词、这条结果）
// 由打开时带进去，用户不用再重复描述一遍。
const fbOpen = ref(false)
const fbMode = ref('request')
const fbItem = ref(null)
const fbError = ref('')

function openFeedback(mode, payload = {}) {
  fbMode.value = mode
  fbItem.value = payload.item || null
  fbError.value = payload.error || ''
  fbOpen.value = true
}

function closeFeedback() {
  fbOpen.value = false
}

const pageWindow = computed(() => {
  const tp = totalPages.value
  if (tp <= 1) return []
  const cur = page.value
  const size = tp <= 7 ? tp : 7
  let start = Math.max(1, cur - 3)
  if (start + size - 1 > tp) start = Math.max(1, tp - size + 1)
  return Array.from({ length: size }, (_, i) => start + i)
})

function syncUrl() {
  const params = new URLSearchParams()
  if (keyword.value.trim()) params.set('q', keyword.value.trim())
  if (activePlatform.value) params.set('platform', activePlatform.value)
  if (page.value > 1) params.set('page', String(page.value))
  const qs = params.toString()
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname)
}

async function doSearch(targetPage = 1) {
  const q = keyword.value.trim()
  if (!q) return
  if (loading.value) return

  loading.value = true
  error.value = ''
  searched.value = true
  page.value = targetPage
  lastKeyword.value = q

  try {
    const data = await searchResources({
      q,
      platforms: activePlatform.value,
      page: targetPage,
      pageSize: PAGE_SIZE,
    })
    items.value = data.items || []
    total.value = data.pagination?.total || 0
    totalPages.value = data.pagination?.totalPages || 0
    tookMs.value = data.tookMs || 0
    syncUrl()
    if (targetPage > 1) window.scrollTo({ top: 0, behavior: 'smooth' })
  } catch (e) {
    items.value = []
    total.value = 0
    totalPages.value = 0
    error.value = e.message || '搜索失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

function pickPlatform(key) {
  if (activePlatform.value === key) return
  activePlatform.value = key
  // 筛选行为本身值得记：哪个平台被偏爱、筛选后还有没有人继续搜
  track('filter', {
    query: lastKeyword.value || keyword.value.trim(),
    platform: key,
  })
  if (searched.value) doSearch(1)
}

function goPage(p) {
  if (p < 1 || p > totalPages.value || p === page.value || loading.value) return
  track('page', {
    query: lastKeyword.value,
    platform: activePlatform.value,
    meta: { page: p },
  })
  doSearch(p)
}

function retry() {
  track('retry', { query: lastKeyword.value, platform: activePlatform.value })
  doSearch(page.value)
}

onMounted(async () => {
  // 埋点先于一切初始化：页面打开就算一次访问，后面接口失败也不影响计数
  initTracking()

  try {
    const data = await fetchPlatforms()
    platforms.value = data.platforms || []
  } catch {
    /* 平台列表拉不到不影响搜索，前端会自动使用返回里的 platformName */
  }

  const params = new URLSearchParams(location.search)
  const q = params.get('q')
  if (q) {
    keyword.value = q
    activePlatform.value = params.get('platform') || ''
    doSearch(Number(params.get('page')) || 1)
  }
})
</script>

<template>
  <div class="page">
    <header class="topbar">
      <div class="wrap topbar-inner">
        <a class="brand" href="/" @click.prevent="keyword = ''; searched = false; activePlatform = ''; history.replaceState(null, '', '/')">
          <span class="logo">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.6-3.6" />
            </svg>
          </span>
          <span class="brand-text">盘迹<em>PanTrace</em></span>
        </a>
        <nav class="nav">
          <span class="nav-tag">PanTrace</span>
        </nav>
      </div>
    </header>

    <section class="hero" :class="{ compact: searched }">
      <div class="wrap hero-inner">
        <h1 v-if="!searched" class="title">盘迹</h1>
        <p v-if="!searched" class="subtitle">一次搜索，翻遍所有网盘</p>

        <form class="searchbar" @submit.prevent="doSearch(1)">
          <span class="search-icon">
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.6-3.6" />
            </svg>
          </span>
          <input
            v-model="keyword"
            type="search"
            placeholder="输入片名、剧名或资源关键词…"
            autocomplete="off"
            maxlength="100"
          />
          <button type="submit" :disabled="loading || !keyword.trim()">
            {{ loading ? '搜索中' : '搜索' }}
          </button>
        </form>
      </div>
    </section>

    <main class="wrap content">
      <div class="filters">
        <button class="chip" :class="{ on: activePlatform === '' }" @click="pickPlatform('')">全部平台</button>
        <button
          v-for="p in platforms"
          :key="p.key"
          class="chip"
          :class="{ on: activePlatform === p.key }"
          @click="pickPlatform(p.key)"
        >
          <i class="chip-dot" :style="{ background: p.color }"></i>
          {{ p.name }}
        </button>
      </div>

      <div v-if="loading" class="skeleton-list">
        <p class="loading-hint">正在聚合各平台结果，首次查询可能需要 10–60 秒…</p>
        <div v-for="i in 5" :key="i" class="skeleton">
          <div class="sk-line w60"></div>
          <div class="sk-line w30"></div>
        </div>
      </div>

      <div v-else-if="error" class="state error-state">
        <div class="state-icon">!</div>
        <p class="state-title">这次没搜成</p>
        <p class="state-desc">{{ error }}</p>
        <div class="state-actions">
          <button class="retry" @click="retry">重试一次</button>
          <button class="link" @click="openFeedback('bug', { error })">上报这个错误</button>
        </div>
      </div>

      <div v-else-if="showEmpty" class="state">
        <div class="state-icon empty">∅</div>
        <p class="state-title">没有找到「{{ lastKeyword }}」</p>
        <p class="state-desc">换个关键词试试，或者先取消平台筛选看看全部来源。</p>
        <button v-if="activePlatform" class="retry" @click="pickPlatform('')">搜索全部平台</button>
        <p class="state-more">
          都没有？
          <button class="link" @click="openFeedback('request')">告诉我你要找什么</button>
        </p>
      </div>

      <template v-else-if="items.length">
        <div class="result-head">
          <span>找到约 <b>{{ total }}</b> 条结果</span>
          <span class="divider">·</span>
          <span>{{ tookMs }} ms</span>
          <span v-if="totalPages > 1" class="divider">·</span>
          <span v-if="totalPages > 1">第 {{ page }} / {{ totalPages }} 页</span>
        </div>

        <div class="result-list">
          <ResultCard
            v-for="item in items"
            :key="item.hsid || item.shareUrl"
            :item="item"
            :keyword="lastKeyword"
            @feedback="openFeedback('result', { item: $event })"
          />
        </div>

        <div v-if="totalPages > 1" class="pager">
          <button class="pg" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
          <button
            v-for="p in pageWindow"
            :key="p"
            class="pg num"
            :class="{ on: p === page }"
            @click="goPage(p)"
          >
            {{ p }}
          </button>
          <button class="pg" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</button>
        </div>
      </template>
    </main>

    <footer class="footer">
      <div class="wrap">
        <span>盘迹 PanTrace</span>
        <span class="dot"></span>
        <span>资源均来自第三方分享，本站不存储任何文件</span>
        <span class="footer-links">
          <button class="link" @click="openFeedback('request')">求资源</button>
          <button class="link" @click="openFeedback('bug')">反馈问题</button>
        </span>
      </div>
    </footer>

    <FeedbackDialog
      :open="fbOpen"
      :mode="fbMode"
      :item="fbItem"
      :keyword="lastKeyword || keyword"
      :error-text="fbError"
      @close="closeFeedback"
    />
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.wrap {
  width: 100%;
  max-width: 920px;
  margin: 0 auto;
  padding: 0 24px;
}

/* 顶栏 */
.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  background: rgba(245, 247, 249, 0.82);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--line);
}

.topbar-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 60px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 9px;
}

.logo {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  background: var(--ink);
  color: #fff;
}

.brand-text {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.brand-text em {
  font-style: normal;
  font-size: 11px;
  font-weight: 500;
  color: var(--muted);
  margin-left: 7px;
  letter-spacing: 0.6px;
}

.nav-tag {
  font-size: 12px;
  color: var(--muted);
  border: 1px solid var(--line-strong);
  padding: 3px 10px;
  border-radius: 999px;
}

/* Hero */
.hero {
  position: relative;
  padding: 96px 0 56px;
  background:
    radial-gradient(55% 70% at 50% 0%, rgba(11, 122, 107, 0.07), transparent 70%),
    linear-gradient(var(--bg), var(--bg));
}

.hero.compact {
  padding: 24px 0 16px;
}

.hero-inner {
  text-align: center;
}

.title {
  margin: 0 0 14px;
  font-size: 46px;
  line-height: 1.2;
  font-weight: 700;
  letter-spacing: 6px;
  text-indent: 6px; /* 视觉居中：抵消 letter-spacing 右侧多出的空隙 */
  color: var(--ink);
}

.subtitle {
  margin: 0 auto 44px;
  max-width: 520px;
  font-size: 15px;
  font-weight: 400;
  letter-spacing: 0.4px;
  color: var(--muted);
}

.searchbar {
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 620px;
  margin: 0 auto;
  padding: 6px 6px 6px 16px;
  background: var(--surface);
  border: 1px solid var(--line-strong);
  border-radius: 14px;
  box-shadow: var(--shadow-md);
  transition: border-color 0.2s, box-shadow 0.2s;
}

.searchbar:focus-within {
  border-color: #8fc9bd;
  box-shadow: 0 12px 36px -14px rgba(11, 122, 107, 0.28);
}

.search-icon {
  display: grid;
  place-items: center;
  color: var(--muted);
  flex-shrink: 0;
}

.searchbar input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  font-size: 15px;
  color: var(--ink);
  padding: 10px 0;
}

.searchbar input::placeholder {
  color: #9aa8bb;
}

.searchbar input::-webkit-search-cancel-button {
  cursor: pointer;
}

.searchbar button {
  flex-shrink: 0;
  height: 40px;
  padding: 0 24px;
  border: none;
  border-radius: 10px;
  background: var(--ink);
  color: #fff;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: 0.3px;
  transition: background 0.16s;
}

.searchbar button:hover:not(:disabled) {
  background: #1e293b;
}

.searchbar button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* 内容区 */
.content {
  flex: 1;
  padding-bottom: 56px;
}

.filters {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 0 22px;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 13px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink-2);
  font-size: 13px;
  transition: all 0.16s;
}

.chip:hover {
  border-color: var(--line-strong);
  background: #fbfcfd;
}

.chip.on {
  background: var(--ink);
  border-color: var(--ink);
  color: #fff;
}

.chip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.result-head {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  color: var(--muted);
  padding: 0 2px 12px;
}

.result-head b {
  color: var(--ink-2);
  font-weight: 600;
}

.divider {
  color: var(--line-strong);
}

.result-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 骨架 */
.skeleton-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.loading-hint {
  margin: 0 0 4px;
  font-size: 12.5px;
  color: var(--muted);
  text-align: center;
}

.skeleton {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 20px 22px;
}

.sk-line {
  height: 12px;
  border-radius: 6px;
  background: linear-gradient(90deg, #eef2f6 25%, #f6f8fa 37%, #eef2f6 63%);
  background-size: 400% 100%;
  animation: shimmer 1.3s ease-in-out infinite;
}

.sk-line + .sk-line {
  margin-top: 12px;
}

.w60 {
  width: 60%;
}
.w30 {
  width: 26%;
}

@keyframes shimmer {
  0% {
    background-position: 100% 50%;
  }
  100% {
    background-position: 0 50%;
  }
}

/* 状态 */
.state {
  text-align: center;
  padding: 64px 20px;
}

.state-icon {
  display: inline-grid;
  place-items: center;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  background: #eef2f6;
  color: var(--muted);
  font-size: 20px;
  font-weight: 700;
  margin-bottom: 16px;
}

.state-icon.empty {
  font-weight: 400;
  font-size: 24px;
}

.error-state .state-icon {
  background: #fdecea;
  color: var(--danger);
}

.state-title {
  margin: 0 0 6px;
  font-size: 15.5px;
  font-weight: 600;
  color: var(--ink);
}

.state-desc {
  margin: 0 auto;
  max-width: 420px;
  font-size: 13.5px;
  color: var(--muted);
}

.retry {
  margin-top: 18px;
  height: 34px;
  padding: 0 18px;
  border-radius: 9px;
  border: 1px solid var(--line-strong);
  background: var(--surface);
  font-size: 13px;
  color: var(--ink-2);
}

.retry:hover {
  border-color: #b7ddd5;
  color: var(--brand-dark);
}

.state-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 18px;
}

.state-actions .retry {
  margin-top: 0;
}

.state-more {
  margin: 16px 0 0;
  font-size: 13px;
  color: var(--muted);
}

.link {
  padding: 0;
  border: none;
  background: none;
  font-size: inherit;
  color: var(--brand);
  text-decoration: underline;
  text-decoration-color: #b7ddd5;
  text-underline-offset: 3px;
}

.link:hover {
  color: var(--brand-dark);
  text-decoration-color: var(--brand);
}

.footer-links {
  display: inline-flex;
  align-items: center;
  gap: 14px;
  margin-left: auto;
}

/* 分页 */
.pager {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 6px;
  margin-top: 30px;
  flex-wrap: wrap;
}

.pg {
  min-width: 34px;
  height: 34px;
  padding: 0 12px;
  border-radius: 9px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink-2);
  font-size: 13px;
  transition: all 0.16s;
}

.pg:hover:not(:disabled):not(.on) {
  border-color: var(--line-strong);
}

.pg.on {
  background: var(--ink);
  border-color: var(--ink);
  color: #fff;
  font-weight: 600;
}

.pg:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* 页脚 */
.footer {
  border-top: 1px solid var(--line);
  padding: 22px 0;
  font-size: 12.5px;
  color: var(--muted);
}

.footer .wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.footer .dot {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--line-strong);
}

@media (max-width: 720px) {
  .title {
    font-size: 32px;
    letter-spacing: 4px;
    text-indent: 4px;
  }

  .hero {
    padding: 56px 0 36px;
  }

  .subtitle {
    margin-bottom: 32px;
    font-size: 14px;
  }

  .searchbar button {
    padding: 0 16px;
  }

  .wrap {
    padding: 0 16px;
  }

  .brand-text em {
    display: none;
  }
}
</style>
