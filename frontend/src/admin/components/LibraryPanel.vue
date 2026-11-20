<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Panel from './Panel.vue'
import * as api from '../api'
import { platformName } from '../format'

/**
 * 添加数据：腾讯文档收集表 → 资源库。
 *
 * 这个面板解决的不是"填个表单"，而是三个具体的坑：
 *
 * 1. **不同作者的表长得不一样。** 标题可能在第 3 列，中间夹着分类和备注。
 *    所以第一步不是填，是"读出来看一眼"：把每个工作表的每列连同前两条真实值
 *    摆出来，人靠样本核对，不靠数格子。
 * 2. **抓一份表要几分钟。** 38 万行大约 3 分钟，同步等会被反代在 60 秒掐断。
 *    所以起任务后立刻返回，进度靠轮询——按钮上直接显示百分比，不用盯着日志。
 * 3. **列号会过时。** 表格作者插一列，锁死的列号就静默错位，每天入库一批
 *    错位数据。改列号时后端会校验那一列里到底有没有链接，指错了当场拒。
 */

const KIND = {
  title: '标题',
  link: '链接',
  code: '提取码',
  mixed: '混合',
  other: '其他',
}

const url = ref('')
const probing = ref(false)
const doc = ref(null)
const probeError = ref('')
const adding = ref(false)
const addMsg = ref('')

const sources = ref([])
const stats = ref({})
const concurrency = ref({})
const daily = ref({})
const loadError = ref('')
const loaded = ref(false)

const editing = ref(0)          // 正在改列号的订阅 id
const editCols = ref({ title: 0, link: 0, code: 0 })
const busy = ref({})            // 订阅 id → 正在做什么，防连点
const confirmDrop = ref(0)
const rowError = ref({})   // 订阅 id → 这一行自己的错误

const running = computed(() =>
  sources.value.filter((s) => (s.job || {}).state === 'running').length
)

const pickedTabs = computed(() => (doc.value ? doc.value.tabs.filter((t) => t._pick) : []))

const totalRows = computed(() => Number(stats.value.total) || 0)
const byPlatform = computed(() =>
  Object.entries(stats.value.by_platform || {}).sort((a, b) => b[1] - a[1])
)

function fmtStamp(ts) {
  if (!ts) return ''
  const d = new Date(Number(ts) * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// 自动同步这块显示的是服务端真实状态，不是写死的文案：关掉了配置就显示"未启用"，
// 改过小时数就跟着变，跑过一轮就带上"新增几条"。面板上看到的必须是真的。
const dailyHour = computed(() =>
  daily.value.hour === null || daily.value.hour === undefined
    ? ''
    : `${String(daily.value.hour).padStart(2, '0')}:00`
)
const dailyNext = computed(() => fmtStamp(daily.value.next_at))
const dailyLast = computed(() => {
  const last = daily.value.last || {}
  if (!last.at) return ''
  if (last.error) return `${fmtStamp(last.at)} 失败：${last.error}`
  const parts = [`新增 ${Number(last.added) || 0} 条`]
  if (Number(last.failed)) parts.push(`失败 ${last.failed} 份`)
  return `${fmtStamp(last.at)} ${parts.join('，')}`
})

function kindLabel(kind) {
  return KIND[kind] || kind
}

// ---- 第一步：读表 ----

async function probe() {
  const u = url.value.trim()
  if (!u) return
  probing.value = true
  probeError.value = ''
  doc.value = null
  addMsg.value = ''
  try {
    const data = await api.libraryProbe(u)
    // 默认勾行数最多的那张：文档里通常是"最新资源1"这样的主表，
    // 7 个工作表全勾会一次灌进去几十万条，第一次用的人接不住。
    let biggest = null
    for (const tab of data.tabs) {
      tab._pick = false
      tab._titleCol = null
      tab._linkCol = null
      tab._codeCol = null
      if (!tab.error && (!biggest || tab.max_row > biggest.max_row)) biggest = tab
    }
    if (biggest) biggest._pick = true
    doc.value = data
  } catch (e) {
    probeError.value = e.message
  } finally {
    probing.value = false
  }
}

function pickAll(on) {
  for (const tab of doc.value.tabs) tab._pick = on && !tab.error
}

function guessOf(tab) {
  const g = {}
  for (const col of tab.columns) {
    if (col.kind === 'title' || col.kind === 'link' || col.kind === 'code') {
      if (!g[col.kind]) g[col.kind] = col.col
    }
  }
  return g
}

function guessText(tab) {
  const g = guessOf(tab)
  const parts = []
  if (g.title) parts.push(`标题第 ${g.title} 列`)
  if (g.link) parts.push(`链接第 ${g.link} 列`)
  if (g.code) parts.push(`码第 ${g.code} 列`)
  return parts.length ? parts.join(' · ') : '认不出列，建议手工填'
}

// ---- 第二步：导入 ----

async function addSelected() {
  const tabs = pickedTabs.value
  if (!tabs.length) return
  adding.value = true
  addMsg.value = ''
  const warnings = []
  const fresh = []
  try {
    for (const tab of tabs) {
      const got = await api.libraryAdd({
        url: url.value.trim(),
        tab_id: tab.tab_id,
        title: tab.title,
        title_col: tab._titleCol || 0,
        link_col: tab._linkCol || 0,
        code_col: tab._codeCol || 0,
      })
      if (got.warning) warnings.push(`${tab.title}：${got.warning}`)
      if (got.created) fresh.push({ id: got.id, title: tab.title })
    }
    await refresh()
    // 新登记的立刻起抓取。用户的目的就是"把数据加进来"，
    // 让他再点一次"开始抓"只是在制造一步多余的操作。
    for (const item of fresh) {
      await api.librarySync(item.id).catch(() => {})
    }
    const names = fresh.map((f) => f.title).join('、')
    addMsg.value = fresh.length
      ? `已登记 ${fresh.length} 份（${names}），正在抓取——进度看下面的列表。`
      : '这些表之前已经登记过，没有重复添加。'
    if (warnings.length) addMsg.value += ' ⚠ ' + warnings.join('；')
    doc.value = null
    url.value = ''
    startPolling()
  } catch (e) {
    addMsg.value = '导入失败：' + e.message
  } finally {
    adding.value = false
  }
}

// ---- 订阅列表 ----

async function refresh() {
  try {
    const data = await api.librarySources()
    sources.value = data.sources || []
    stats.value = data.stats || {}
    concurrency.value = data.concurrency || {}
    daily.value = data.daily || {}
    loadError.value = ''
  } catch (e) {
    loadError.value = e.message
  } finally {
    loaded.value = true
  }
}

async function runSync(src) {
  busy.value = { ...busy.value, [src.id]: 'sync' }
  try {
    await api.librarySync(src.id)
    await refresh()
    startPolling()
  } catch (e) {
    loadError.value = e.message
  } finally {
    const next = { ...busy.value }
    delete next[src.id]
    busy.value = next
  }
}

function clearRowError(id) {
  const next = { ...rowError.value }
  delete next[id]
  rowError.value = next
}

function openEdit(src) {
  clearRowError(src.id)
  editing.value = src.id
  editCols.value = {
    title: src.title_col || 0,
    link: src.link_col || 0,
    code: src.code_col || 0,
  }
}

async function saveEdit(src) {
  busy.value = { ...busy.value, [src.id]: 'cols' }
  try {
    await api.libraryPatch(src.id, {
      title_col: Number(editCols.value.title) || 0,
      link_col: Number(editCols.value.link) || 0,
      code_col: Number(editCols.value.code) || 0,
    })
    editing.value = 0
    await refresh()
  } catch (e) {
    // 列号指错了后端会 400，把原话摆在这一行上——这是给"作者插了一列"用的提示，
    // 改写反而看不出该填几。放在页面顶部会跟"读表错误"混在一起，
    // 而且离操作位置隔了好几屏。
    rowError.value = { ...rowError.value, [src.id]: e.message }
  } finally {
    const next = { ...busy.value }
    delete next[src.id]
    busy.value = next
  }
}

async function toggle(src) {
  busy.value = { ...busy.value, [src.id]: 'toggle' }
  try {
    await api.libraryPatch(src.id, { enabled: !src.enabled })
    await refresh()
  } catch (e) {
    loadError.value = e.message
  } finally {
    const next = { ...busy.value }
    delete next[src.id]
    busy.value = next
  }
}

async function drop(src) {
  busy.value = { ...busy.value, [src.id]: 'drop' }
  try {
    await api.libraryDrop(src.id)
    confirmDrop.value = 0
    await refresh()
  } catch (e) {
    loadError.value = e.message
  } finally {
    const next = { ...busy.value }
    delete next[src.id]
    busy.value = next
  }
}

// ---- 轮询：只在真的有任务在跑时开 ----

let timer = null

function stopPolling() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

function startPolling() {
  if (timer) return
  timer = setInterval(async () => {
    if (document.visibilityState !== 'visible') return
    await refresh()
    // 全部跑完了就停。空转的定时器会让"自动刷新"变成一个看不到的负担。
    if (!sources.value.some((s) => (s.job || {}).state === 'running')) stopPolling()
  }, 2500)
}

function jobOf(src) {
  const job = src.job || {}
  if (job.state === 'running') {
    const pct = job.total ? Math.min(100, Math.round((job.rows / job.total) * 100)) : 0
    return { state: 'running', pct, text: job.total ? `${pct}%` : '准备中' }
  }
  if (job.state === 'done') {
    return { state: 'done', text: `+${job.added} 条` }
  }
  if (job.state === 'error') {
    return { state: 'error', text: job.error || '失败' }
  }
  return null
}

function colsText(src) {
  const parts = []
  if (src.title_col) parts.push(`标${src.title_col}`)
  if (src.link_col) parts.push(`链${src.link_col}`)
  if (src.code_col) parts.push(`码${src.code_col}`)
  return parts.length ? parts.join(' ') : '自动'
}

function ago(ts) {
  if (!ts) return '从未'
  const diff = Math.floor(Date.now() / 1000) - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

onMounted(async () => {
  await refresh()
  if (running.value) startPolling()
})

onUnmounted(stopPolling)
</script>

<template>
  <Panel title="添加数据" tag="腾讯文档收集表 → 资源库" sub="粘链接先读表，确认列再导入">
    <!-- 已入库的量：加数据的人第一眼想知道"现在库里有多少" -->
    <div class="lib-stat">
      <div class="lib-stat-main">
        <span class="lib-stat-n">{{ totalRows.toLocaleString('zh-CN') }}</span>
        <span class="lib-stat-k">条资源在库</span>
      </div>
      <div class="lib-stat-plats">
        <span v-for="[k, n] in byPlatform" :key="k" class="lib-plat">
          {{ platformName(k) }}<b>{{ n.toLocaleString('zh-CN') }}</b>
        </span>
        <span v-if="!byPlatform.length" class="lib-muted">还没有数据</span>
      </div>
      <!-- 自动同步：每天几点、下次什么时候、上一轮抓了多少。数据来自后端，
           配置改了这里跟着变，不写死文案 -->
      <div class="lib-auto">
        <span class="lib-auto-dot" :class="{ on: dailyHour }" />
        <span v-if="dailyHour">每天 {{ dailyHour }} 自动同步</span>
        <span v-else-if="daily.enabled === false" class="lib-muted">自动同步未启用</span>
        <span v-if="dailyNext" class="lib-muted">· 下次 {{ dailyNext }}</span>
        <span v-if="dailyLast" class="lib-muted">· 上次 {{ dailyLast }}</span>
      </div>
    </div>

    <div v-if="loadError" class="err" style="margin-bottom: 12px">{{ loadError }}</div>

    <!-- 第一步 -->
    <div class="lib-step">
      <div class="lib-step-n">1</div>
      <div class="lib-step-body">
        <div class="lib-input-row">
          <input
            v-model="url"
            placeholder="https://docs.qq.com/sheet/…  （文档要开「任何人可查看」）"
            spellcheck="false"
            @keyup.enter="probe"
          />
          <button class="btn-primary" :disabled="probing || !url.trim()" @click="probe">
            {{ probing ? '读取中…' : '读取表格' }}
          </button>
        </div>
        <p class="lib-hint">
          只读文档结构，不写库。带 <code>?tab=</code> 的链接只读那一张表，否则列出全部工作表。
        </p>
        <div v-if="probeError" class="err">{{ probeError }}</div>
      </div>
    </div>

    <!-- 第二步：结构确认 -->
    <div v-if="doc" class="lib-step">
      <div class="lib-step-n">2</div>
      <div class="lib-step-body">
        <div class="lib-doc-head">
          <b>{{ doc.title }}</b>
          <span class="lib-muted">{{ doc.tabs.length }} 个工作表</span>
          <span class="lib-spacer" />
          <template v-if="doc.tabs.length > 1">
            <button class="btn-ghost lib-mini" @click="pickAll(true)">全选</button>
            <button class="btn-ghost lib-mini" @click="pickAll(false)">全不选</button>
          </template>
        </div>

        <div v-for="tab in doc.tabs" :key="tab.tab_id" class="lib-tab" :class="{ on: tab._pick }">
          <div class="lib-tab-head">
            <label class="lib-check">
              <input v-model="tab._pick" type="checkbox" :disabled="!!tab.error" />
              <span>{{ tab.title || tab.tab_id }}</span>
            </label>
            <span class="lib-muted">{{ tab.max_row.toLocaleString('zh-CN') }} 行</span>
            <span class="lib-spacer" />
            <span class="lib-guess">{{ guessText(tab) }}</span>
          </div>

          <div v-if="tab.error" class="err lib-tab-err">{{ tab.error }}</div>

          <template v-else>
            <div class="lib-cols">
              <label>
                标题列
                <input v-model.number="tab._titleCol" type="number" min="0" max="40" placeholder="自动" />
              </label>
              <label>
                链接列
                <input v-model.number="tab._linkCol" type="number" min="0" max="40" placeholder="自动" />
              </label>
              <label>
                提取码列
                <input v-model.number="tab._codeCol" type="number" min="0" max="40" placeholder="自动" />
              </label>
              <span class="lib-hint lib-inline">留空 = 自动识别；数字按表格里看到的列号填</span>
            </div>

            <div class="lib-probe">
              <div v-for="col in tab.columns" :key="col.col" class="lib-col-row">
                <span class="lib-col-n">第 {{ col.col }} 列</span>
                <span class="lib-col-kind" :class="'k-' + col.kind">{{ kindLabel(col.kind) }}</span>
                <span class="lib-col-sample">{{ col.samples.slice(0, 2).join('  /  ') }}</span>
              </div>
            </div>
          </template>
        </div>

        <div class="lib-actions">
          <button class="btn-primary" :disabled="adding || !pickedTabs.length" @click="addSelected">
            {{ adding ? '导入中…' : `导入选中的 ${pickedTabs.length} 份` }}
          </button>
          <span class="lib-muted">导入后自动开始抓取，进度看下面</span>
        </div>
        <div v-if="addMsg" class="lib-msg">{{ addMsg }}</div>
      </div>
    </div>

    <div v-else class="lib-step">
      <div class="lib-step-n lib-step-n-idle">2</div>
      <div class="lib-step-body lib-step-idle">
        读表后，这里会列出每张工作表的列结构和前两条样本值，确认无误再导入。
      </div>
    </div>

    <div v-if="addMsg" class="lib-msg lib-msg-solo">{{ addMsg }}</div>

    <!-- 第三步：已登记的订阅 -->
    <div class="lib-step lib-step-last">
      <div class="lib-step-n">3</div>
      <div class="lib-step-body">
        <div class="lib-doc-head">
          <b>已登记的文档</b>
          <span v-if="dailyHour" class="lib-muted">每天 {{ dailyHour }} 自动增量</span>
          <span v-else class="lib-muted">自动增量未启用，只能手动抓取</span>
          <span class="lib-spacer" />
          <span v-if="running" class="lib-running">
            <i class="lib-spin" />{{ running }} 份抓取中
          </span>
        </div>

        <div v-if="!loaded" class="empty">载入中…</div>
        <div v-else-if="!sources.length" class="lib-empty">
          还没有登记任何文档。把腾讯文档链接粘到上面，读一次表就能加进来。
        </div>

        <div v-for="src in sources" :key="src.id" class="lib-src" :class="{ off: !src.enabled }">
          <div class="lib-src-main">
            <div class="lib-src-title">
              {{ src.title || src.tab_id || '默认表' }}
              <span v-if="!src.enabled" class="lib-tag">已暂停</span>
            </div>
            <div class="lib-src-meta">
              已读 {{ Number(src.last_row).toLocaleString('zh-CN') }} 行
              · 列 {{ colsText(src) }}
              · {{ ago(src.last_run_at) }}
              <span v-if="src.last_error" class="lib-err-text">· {{ src.last_error }}</span>
            </div>

            <div v-if="jobOf(src)" class="lib-job" :class="jobOf(src).state">
              <template v-if="jobOf(src).state === 'running'">
                <div class="lib-bar"><i :style="{ width: jobOf(src).pct + '%' }" /></div>
                <span>{{ jobOf(src).text }} · {{ Number(src.job.rows).toLocaleString('zh-CN') }} / {{ Number(src.job.total).toLocaleString('zh-CN') }}</span>
              </template>
              <template v-else-if="jobOf(src).state === 'done'">
                <span class="lib-done">
                  抓完：新增 {{ src.job.added }}，更新 {{ src.job.touched }}<template v-if="src.job.skipped">，跳过 {{ src.job.skipped }}</template>
                </span>
              </template>
              <template v-else>
                <span class="lib-err-text">抓取失败：{{ jobOf(src).text }}</span>
              </template>
            </div>

            <div v-if="editing === src.id" class="lib-edit">
              <div class="lib-cols">
                <label>标题列 <input v-model.number="editCols.title" type="number" min="0" max="40" placeholder="自动" /></label>
                <label>链接列 <input v-model.number="editCols.link" type="number" min="0" max="40" placeholder="自动" /></label>
                <label>提取码列 <input v-model.number="editCols.code" type="number" min="0" max="40" placeholder="自动" /></label>
                <button class="btn-primary lib-mini" :disabled="busy[src.id]" @click="saveEdit(src)">
                  {{ busy[src.id] ? '校验中…' : '保存' }}
                </button>
                <button class="btn-ghost lib-mini" @click="editing = 0">取消</button>
              </div>
              <div class="lib-hint">留空 = 自动识别。保存时后端会真的去读那几列，指错了不让存。</div>
              <div v-if="rowError[src.id]" class="lib-row-err">{{ rowError[src.id] }}</div>
            </div>
          </div>

          <!-- 确认删除时把其余按钮收起来：五个按钮挤在一行必然换行，
               而且这时候人只该看两个选项。 -->
          <div v-if="confirmDrop === src.id" class="lib-src-actions lib-confirm">
            <span class="lib-muted">已入库的资源不动</span>
            <button class="btn-ghost lib-mini lib-danger" :disabled="busy[src.id]" @click="drop(src)">
              确认删除
            </button>
            <button class="btn-ghost lib-mini" @click="confirmDrop = 0">取消</button>
          </div>
          <div v-else class="lib-src-actions">
            <button
              class="btn-ghost lib-mini"
              :disabled="busy[src.id] || (src.job || {}).state === 'running'"
              @click="runSync(src)"
            >
              {{ (src.job || {}).state === 'running' ? '抓取中' : '立即抓取' }}
            </button>
            <button class="btn-ghost lib-mini" @click="openEdit(src)">改列号</button>
            <button class="btn-ghost lib-mini" :disabled="busy[src.id]" @click="toggle(src)">
              {{ src.enabled ? '暂停' : '恢复' }}
            </button>
            <button class="btn-ghost lib-mini" @click="confirmDrop = src.id">删除</button>
          </div>
        </div>

        <p class="lib-hint lib-foot">
          删除订阅只停掉每日抓取，<b>已入库的资源不动</b>。
          列号锁死后，如果表格作者插了一列，下次抓取会大面积"第 N 列没有链接"——那就是该回来改列号了。
        </p>
      </div>
    </div>
  </Panel>
</template>
