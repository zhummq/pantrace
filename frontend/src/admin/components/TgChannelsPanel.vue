<script setup>
import { onMounted, reactive, ref } from 'vue'
import Panel from './Panel.vue'
import * as api from '../api'

/**
 * TG 频道配置。
 *
 * 频道名存在库里，保存即热更：搜索插件每次都重新取生效列表，不用重启。
 * .env 的 TG_CHANNELS 只作为"恢复默认"的基线，避免运维改完一行配置还得
 * 想起来 reload systemd。
 *
 * 空列表是有效配置（暂停 TG 采集），不是"没配"——所以保存空表要二次确认，
 * 但绝不偷偷回退到 .env，否则用户以为关掉了其实还在跑。
 */

const loaded = ref(false)
const saving = ref(false)
const message = ref('')
const messageErr = ref(false)

const cfg = reactive({
  channels: [],
  defaults: [],
  source: 'env',
  max: 20,
  enabled: true,
})

const rows = ref([])
let seq = 0

function flash(text, isErr) {
  message.value = text
  messageErr.value = !!isErr
  if (text) setTimeout(() => { if (message.value === text) message.value = '' }, 4000)
}

function addRow(value = '') {
  rows.value.push({ id: ++seq, value, result: null, probing: false })
}

function apply(d) {
  cfg.channels = Array.isArray(d.channels) ? d.channels : []
  cfg.defaults = Array.isArray(d.defaults) ? d.defaults : []
  cfg.source = d.source === 'admin' ? 'admin' : 'env'
  cfg.max = Number(d.max) || 20
  cfg.enabled = d.enabled !== false
  rows.value = []
  cfg.channels.forEach(addRow)
}

async function reload() {
  try {
    apply(await api.tgChannels())
    loaded.value = true
  } catch (e) {
    if (e.status !== 401) flash('读取失败：' + e.message, true)
  }
}

function values() {
  return rows.value.map((r) => String(r.value || '').trim()).filter(Boolean)
}

async function save() {
  const list = values()
  if (!list.length && !window.confirm('列表为空会暂停 TG 采集（不是回退到默认值），确认保存？')) return
  saving.value = true
  try {
    apply(await api.saveTgChannels({ channels: list }))
    flash('已保存，下一次搜索立即生效（不用重启）')
  } catch (e) {
    flash('保存失败：' + e.message, true)
  } finally {
    saving.value = false
  }
}

async function reset() {
  if (!window.confirm('恢复到 .env 的 TG_CHANNELS？当前的后台配置会被删掉。')) return
  try {
    apply(await api.resetTgChannels())
    flash('已恢复默认配置')
  } catch (e) {
    flash('恢复失败：' + e.message, true)
  }
}

async function probe(row) {
  const name = String(row.value || '').trim()
  if (!name) return
  row.probing = true
  row.result = null
  try {
    row.result = await api.probeTgChannel(name)
  } catch (e) {
    row.result = { ok: false, error: e.message }
  } finally {
    row.probing = false
  }
}

onMounted(reload)
</script>

<template>
  <Panel title="TG 频道" tag="免费通道 · 多频道并发" sub="改完立即生效，不用重启">
    <p class="tg-lead">
      频道里的资源由 TG 公开页抓取，<strong>不消耗上游额度</strong>。每多一个频道就多一次
      t.me 请求，并发跑、共用 8 秒窗口——不是越多越好，先放覆盖最好的两三个。
    </p>

    <div v-if="!cfg.enabled" class="tg-off">
      当前未启用 TG 源：配置可以保存，但不会生效。要启用需在 <code>.env</code> 的
      <code>ENABLED_SOURCES</code> 里加上 <code>tg_searchers</code>，然后重启服务。
    </div>

    <div class="tg-source">
      <span class="tg-source-tag" :class="cfg.source">{{ cfg.source === 'admin' ? '后台配置' : '.env 默认值' }}</span>
      <span v-if="cfg.source === 'admin'">已覆盖 .env 的 <code>TG_CHANNELS={{ cfg.defaults.join(',') || '（空）' }}</code></span>
      <span v-else>修改后即覆盖 <code>TG_CHANNELS</code>，恢复默认会回到这里。</span>
    </div>

    <div v-if="rows.length" class="tg-list">
      <div v-for="(row, i) in rows" :key="row.id" class="tg-row">
        <span class="tg-index">{{ String(i + 1).padStart(2, '0') }}</span>
        <input
          v-model="row.value"
          class="tg-input"
          :placeholder="`频道名，例如 tgsearchers${i + 1}`"
          spellcheck="false"
          @input="row.result = null"
          @keyup.enter="probe(row)"
        />
        <button class="grp-btn" :disabled="row.probing || !row.value.trim()" @click="probe(row)">
          {{ row.probing ? '探测中…' : '探测' }}
        </button>
        <button class="grp-btn danger" title="移除这个频道" @click="rows.splice(i, 1)">删</button>
        <div v-if="row.result" class="tg-probe" :class="row.result.ok ? 'ok' : 'bad'">
          <template v-if="row.result.ok">可解析 · 抓到 {{ row.result.count }} 条消息</template>
          <template v-else>探测失败：{{ row.result.error }}</template>
        </div>
      </div>
    </div>
    <div v-else class="tg-empty">当前没有频道，TG 采集已暂停。加一个频道后记得保存。</div>

    <button class="grp-btn tg-add" :disabled="rows.length >= cfg.max" @click="addRow()">
      + 添加频道{{ rows.length >= cfg.max ? `（最多 ${cfg.max} 个）` : '' }}
    </button>

    <p class="ad-hint">
      频道名只认字母、数字、下划线（3–64 位），前缀 <code>@</code> 可省略；逗号或换行分隔也行，重复会自动去掉。
      探测走的是线上同一套抓取与解析，能确认频道存在且页面结构还能读；t.me 不通时会直接报网络错误。
    </p>

    <div class="grp-actions">
      <button class="grp-save" :disabled="saving || !loaded" @click="save">
        {{ saving ? '保存中…' : '保存配置' }}
      </button>
      <button class="grp-btn" :disabled="!loaded || cfg.source === 'env'" @click="reset">
        恢复 .env 默认
      </button>
      <span class="grp-msg" :class="{ err: messageErr }">{{ message }}</span>
    </div>
  </Panel>
</template>
