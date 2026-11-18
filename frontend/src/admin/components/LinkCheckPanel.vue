<script setup>
import { computed, onMounted, ref } from 'vue'
import Panel from './Panel.vue'
import * as api from '../api'
import { platformName, shortTime } from '../format'

/**
 * 链接存活检测。
 *
 * 这个页面不是"看数字"用的，是两个判断：
 * 1. uncertain 占比高 → 匿名接口被限流或改版了，检测在空转（看着有数据，其实全判不了）
 * 2. bad 突然变多 → 判定映射错了，正在误杀结果
 *
 * 所以不摆"有效占 87%"这种比例：分母是检测记录数，不是资源总数，这个百分比没有业务含义。
 * 只有 uncertain 的占比值得看，因为它直接决定"这套检测还算不算数"。
 */
const STATE_TEXT = {
  ok: '有效',
  bad: '失效',
  locked: '需提取码',
  uncertain: '判不了',
  unsupported: '不支持',
}

const data = ref(null)
const loading = ref(false)
const errorMsg = ref('')
const showOk = ref(false)

const url = ref('')
const pwd = ref('')
const testing = ref(false)
const testResult = ref(null)
const testError = ref('')

const summary = computed(() => (data.value && data.value.summary) || {})
const recent = computed(() => (data.value && data.value.recent) || [])
const live = computed(() => summary.value.live || {})
const totalLive = computed(() =>
  Object.values(live.value).reduce((s, n) => s + (Number(n) || 0), 0)
)

const spin = computed(() => {
  if (!totalLive.value) return null
  return Math.round(((Number(live.value.uncertain) || 0) / totalLive.value) * 100)
})
const spinBad = computed(() => spin.value !== null && spin.value >= 30)

// 只摆有数的状态：一排 "不支持 0" 除了让眼睛多跳一次，没有任何信息
const pills = computed(() =>
  Object.entries(STATE_TEXT).filter(
    ([key]) => (Number(live.value[key]) || 0) > 0 || key === 'ok' || key === 'bad'
  )
)

const rows = computed(() =>
  showOk.value ? recent.value : recent.value.filter((r) => r.state !== 'ok')
)
const okHidden = computed(() =>
  showOk.value ? 0 : recent.value.filter((r) => r.state === 'ok').length
)

async function reload() {
  loading.value = true
  try {
    data.value = await api.linkCheck('', 60)
    errorMsg.value = ''
  } catch (e) {
    errorMsg.value = e.message
  } finally {
    loading.value = false
  }
}

async function runTest() {
  const u = url.value.trim()
  if (!u) return
  testing.value = true
  testError.value = ''
  testResult.value = null
  try {
    testResult.value = await api.linkCheckTest(u, pwd.value.trim())
    await reload()
  } catch (e) {
    testError.value = e.message
  } finally {
    testing.value = false
  }
}

onMounted(reload)
defineExpose({ reload })
</script>

<template>
  <Panel
    title="链接存活检测"
    :tag="`${summary.rows || 0} 条记录 · 失效 ${summary.bad || 0}`"
    :tag-bad="Number(summary.bad) > 0"
  >
    <div v-if="errorMsg" class="lc-err">读取失败：{{ errorMsg }}</div>

    <div v-if="data" class="lc-states">
      <span v-for="([key, text], i) in pills" :key="i" class="lc-pill" :class="key">
        {{ text }} <b>{{ live[key] || 0 }}</b>
      </span>
      <span class="lc-meta">
        检测中 {{ summary.inflight || 0 }} · 失效{{ summary.hideBad ? '已隐藏' : '仅标记' }}
      </span>
    </div>

    <div v-if="data" class="lc-verdict" :class="{ bad: spinBad }">
      <template v-if="spin === null">
        还没有检测记录——搜索一次，返回后会自动开始探活
      </template>
      <template v-else-if="spinBad">
        检测在空转：{{ spin }}% 的链接判不了。多半是匿名接口被限流或改版，先去下面手工验一条。
      </template>
      <template v-else>
        检测正常：{{ spin }}% 判不了，判定可信。
      </template>
    </div>

    <div class="lc-test">
      <input
        v-model="url"
        class="lc-input"
        type="text"
        placeholder="粘一条分享链接，验接口还认不认我们"
        @keyup.enter="runTest"
      />
      <input v-model="pwd" class="lc-pwd" type="text" placeholder="提取码（可选）" />
      <button class="btn-ghost" :disabled="testing || !url.trim()" @click="runTest">
        {{ testing ? '检测中…' : '检测' }}
      </button>
    </div>

    <div v-if="testError" class="lc-err">{{ testError }}</div>
    <div v-if="testResult" class="lc-result" :class="testResult.state">
      <b>{{ STATE_TEXT[testResult.state] || testResult.state }}</b>
      <span>{{ testResult.summary }}</span>
      <em>{{ testResult.ms }}ms · {{ testResult.key }}</em>
    </div>

    <div class="lc-bar">
      <span class="card-sub">
        {{ showOk ? '显示全部记录' : `已隐藏 ${okHidden} 条有效记录` }}
      </span>
      <button class="btn-ghost" style="height: 26px; padding: 0 10px; font-size: 11.5px" @click="showOk = !showOk">
        {{ showOk ? '只看异常' : '显示有效' }}
      </button>
      <button class="btn-ghost" style="height: 26px; padding: 0 10px; font-size: 11.5px; margin-left: auto" :disabled="loading" @click="reload">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <div v-if="!rows.length" class="empty">
      {{ recent.length ? '没有异常记录——这段时间探活全过了' : '还没有检测记录' }}
    </div>

    <template v-else>
      <div class="lc-head">
        <span>状态</span>
        <span>平台</span>
        <span>链接</span>
        <span class="lc-t">检测于</span>
      </div>
      <div class="lc-list">
        <div v-for="row in rows" :key="row.key" class="lc-row">
          <span class="lc-badge" :class="row.state">{{ STATE_TEXT[row.state] || row.state }}</span>
          <span class="lc-plat">{{ platformName(row.platform) }}</span>
          <a
            class="lc-url"
            :href="row.url"
            target="_blank"
            rel="noopener noreferrer"
            :title="row.summary || row.url"
          >{{ row.url }}</a>
          <span class="lc-t">{{ shortTime(row.checked_at) }}</span>
        </div>
      </div>
    </template>

    <div class="card-sub" style="margin-top: 12px">
      手工验一条是误判复位入口：某条好资源被标成失效，粘进来跑一次就恢复。
    </div>
  </Panel>
</template>

<style scoped>
.lc-err {
  margin: 6px 0;
  font-size: 12px;
  color: var(--red);
}

.lc-states {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 0 10px;
}

.lc-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  background: var(--line);
  color: var(--ink-2);
}
.lc-pill b { font-weight: 600; }
.lc-pill.ok { background: var(--success-soft); color: var(--success-ink); }
.lc-pill.bad { background: var(--danger-soft); color: var(--red); }
.lc-pill.locked { background: var(--warn-soft); color: var(--warn); }
.lc-pill.uncertain { background: var(--surface-3); color: var(--muted); }

.lc-meta { margin-left: auto; font-size: 11.5px; color: var(--faint); }

.lc-verdict {
  margin-bottom: 12px;
  padding: 9px 11px;
  border-radius: 8px;
  background: var(--surface-3);
  color: var(--muted);
  font-size: 12px;
  line-height: 1.65;
}
.lc-verdict.bad { background: var(--warn-soft); color: var(--warn); }

.lc-test {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.lc-input {
  flex: 1;
  min-width: 200px;
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  font-size: 12.5px;
  color: var(--ink);
  background: var(--surface);
  outline: none;
  font-family: inherit;
}
.lc-input:focus { border-color: var(--brand-400); }

.lc-pwd {
  width: 130px;
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  font-size: 12.5px;
  outline: none;
  font-family: inherit;
}
.lc-pwd:focus { border-color: var(--brand-400); }

.lc-result {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 6px 0 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--surface-2);
  font-size: 12px;
  color: var(--ink-2);
  word-break: break-all;
}
.lc-result b { font-size: 12.5px; flex-shrink: 0; }
.lc-result em { margin-left: auto; font-style: normal; color: var(--faint); font-size: 11px; flex-shrink: 0; }
.lc-result.bad b { color: var(--red); }
.lc-result.locked b { color: var(--warn); }
.lc-result.ok b { color: var(--success-ink); }

.lc-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 12px 0 8px;
  padding-top: 10px;
  border-top: 1px solid var(--line);
}

.lc-head,
.lc-row {
  display: grid;
  grid-template-columns: 76px 90px minmax(0, 1fr) 62px;
  align-items: center;
  gap: 10px;
}

.lc-head {
  padding: 0 0 6px;
  border-bottom: 1px solid var(--line);
  font-size: 11px;
  color: var(--faint);
}

.lc-list { max-height: 420px; overflow-y: auto; padding-right: 2px; }

.lc-row {
  padding: 7px 0;
  border-bottom: 1px solid var(--line);
  font-size: 12.5px;
}

.lc-badge {
  display: inline-flex;
  justify-content: center;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  background: var(--line);
  color: var(--muted);
}
.lc-badge.ok { background: var(--success-soft); color: var(--success-ink); }
.lc-badge.bad { background: var(--danger-soft); color: var(--red); }
.lc-badge.locked { background: var(--warn-soft); color: var(--warn); }

.lc-plat { color: var(--ink-2); font-size: 11.5px; }

.lc-url {
  color: var(--muted);
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-decoration: none;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}
.lc-url:hover { color: var(--brand); }

.lc-t { text-align: right; color: var(--faint); font-size: 11.5px; font-variant-numeric: tabular-nums; }

@media (max-width: 560px) {
  .lc-head, .lc-row { grid-template-columns: 68px minmax(0, 1fr) 56px; }
  .lc-plat { display: none; }
}
</style>
