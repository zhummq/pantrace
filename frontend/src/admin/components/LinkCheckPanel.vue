<script setup>
import { computed, onMounted, ref } from 'vue'
import Panel from './Panel.vue'
import * as api from '../api'
import { platformName, shortTime } from '../format'

/**
 * 链接存活检测面板。
 *
 * 这个面板不是"看数字"用的，是两个判断用的：
 * 1. `uncertain` 占比高 → 匿名接口被限流或改版了，检测在空转（看着有数据，其实全判不了）；
 * 2. `bad` 突然变多 → 判定映射错了，正在误杀结果。先关 LINK_CHECK_HIDE_BAD 再查。
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

const url = ref('')
const pwd = ref('')
const testing = ref(false)
const testResult = ref(null)
const testError = ref('')

const summary = computed(() => data.value?.summary || {})
const recent = computed(() => data.value?.recent || [])
const live = computed(() => summary.value.live || {})
const totalLive = computed(() =>
  Object.values(live.value).reduce((s, n) => s + (Number(n) || 0), 0)
)

function share(ratio) {
  if (!totalLive.value) return '0%'
  return Math.round((ratio / totalLive.value) * 100) + '%'
}

async function reload() {
  loading.value = true
  try {
    data.value = await api.linkCheck('', 30)
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
  >
    <p class="lc-hint">
      搜索返回后异步探活（夸克/UC/阿里/123；百度需带提取码），确认失效的从结果里摘掉。
      这里同时是误判复位入口：某条好资源被标成失效，粘进来跑一次就恢复。
    </p>

    <div v-if="errorMsg" class="lc-err">读取失败：{{ errorMsg }}</div>

    <div v-if="data" class="lc-states">
      <span v-for="(text, key) in STATE_TEXT" :key="key" class="lc-pill" :class="key">
        {{ text }} <b>{{ live[key] || 0 }}</b>
        <em v-if="totalLive">{{ share(live[key] || 0) }}</em>
      </span>
      <span class="lc-meta">检测中 {{ summary.inflight || 0 }} · 失效{{ summary.hideBad ? '已隐藏' : '仅标记' }}</span>
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
      <button class="ghost" :disabled="testing || !url.trim()" @click="runTest">
        {{ testing ? '检测中…' : '检测' }}
      </button>
    </div>
    <div v-if="testError" class="lc-err">{{ testError }}</div>
    <div v-if="testResult" class="lc-result" :class="testResult.state">
      <b>{{ STATE_TEXT[testResult.state] || testResult.state }}</b>
      <span>{{ testResult.summary }}</span>
      <em>{{ testResult.ms }}ms · {{ testResult.key }}</em>
    </div>

    <div v-if="!recent.length" class="empty">
      还没有检测记录——搜索一次，返回后会自动开始探活
    </div>

    <template v-else>
      <div class="lc-head">
        <span>状态</span>
        <span>平台</span>
        <span>链接</span>
        <span class="lc-t">检测于</span>
      </div>
      <div class="lc-list">
        <div v-for="row in recent" :key="row.key" class="lc-row">
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

    <div class="lc-foot">
      <button class="ghost lc-refresh" :disabled="loading" @click="reload">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
      <span class="lc-note">
        ok / bad 都是 6 小时到期：bad 要短，任何一次误判都得有自愈的机会
      </span>
    </div>
  </Panel>
</template>

<style scoped>
.lc-hint {
  margin: 12px 0 10px;
  font-size: 12px;
  line-height: 1.65;
  color: var(--muted);
}

.lc-err {
  margin: 8px 0;
  font-size: 12px;
  color: var(--danger);
}

.lc-states {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 10px 0 12px;
}

.lc-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 11.5px;
  background: #f2f5f8;
  color: var(--ink-2);
}
.lc-pill b { font-weight: 600; }
.lc-pill em { font-style: normal; color: var(--faint); font-size: 10.5px; }
.lc-pill.ok { background: rgba(0, 169, 157, 0.1); color: #0a7c73; }
.lc-pill.bad { background: rgba(200, 60, 50, 0.1); color: var(--danger); }
.lc-pill.locked { background: rgba(201, 106, 9, 0.1); color: var(--warn); }
.lc-pill.uncertain { background: #f2f5f8; color: var(--muted); }

.lc-meta { margin-left: auto; font-size: 11.5px; color: var(--faint); }

.lc-test {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.lc-input {
  flex: 1;
  min-width: 0;
  height: 30px;
  padding: 0 10px;
  border: 1px solid var(--line-2);
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  color: var(--ink);
  background: var(--surface);
}

.lc-pwd { width: 130px; height: 30px; padding: 0 10px; border: 1px solid var(--line-2); border-radius: var(--radius-sm); font-size: 12.5px; }

.lc-result {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 6px 0 10px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: #f7f9fb;
  font-size: 12px;
  color: var(--ink-2);
}
.lc-result b { font-size: 12.5px; }
.lc-result em { margin-left: auto; font-style: normal; color: var(--faint); font-size: 11px; }
.lc-result.bad b { color: var(--danger); }
.lc-result.locked b { color: var(--warn); }
.lc-result.ok b { color: #0a7c73; }

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

.lc-list { max-height: 380px; overflow-y: auto; padding-right: 2px; }

.lc-row {
  padding: 7px 0;
  border-bottom: 1px solid #f2f5f8;
  font-size: 12.5px;
}

.lc-badge {
  display: inline-flex;
  justify-content: center;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  background: #f2f5f8;
  color: var(--muted);
}
.lc-badge.ok { background: rgba(0, 169, 157, 0.1); color: #0a7c73; }
.lc-badge.bad { background: rgba(200, 60, 50, 0.1); color: var(--danger); }
.lc-badge.locked { background: rgba(201, 106, 9, 0.1); color: var(--warn); }

.lc-plat { color: var(--ink-2); font-size: 11.5px; }

.lc-url {
  color: var(--muted);
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-decoration: none;
  font-family: var(--mono);
}
.lc-url:hover { color: var(--brand); }

.lc-t { text-align: right; color: var(--faint); font-size: 11.5px; font-variant-numeric: tabular-nums; }

.lc-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.lc-refresh { height: 28px; padding: 0 11px; font-size: 12px; }
.lc-note { color: var(--faint); font-size: 11.5px; }

@media (max-width: 720px) {
  .lc-head, .lc-row { grid-template-columns: 68px minmax(0, 1fr) 56px; }
  .lc-plat { display: none; }
}
</style>
