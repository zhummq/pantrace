<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import Panel from './Panel.vue'
import BarList from './BarList.vue'
import StatGrid from './StatGrid.vue'
import * as api from '../api'
import { PLATFORM_NAMES, platformName, sizeText } from '../format'

const props = defineProps({
  days: { type: Number, default: 7 },
  ownRatio: { type: Number, default: 0 },
})

const panel = ref(null)
const quickInput = ref(null)

const loading = ref(true)
const error = ref('')
const links = ref([])
const misses = ref([])
const stats = ref({})
const editingId = ref(null)
const saving = ref(false)
const manualOpen = ref(false)

// ---- 快速补录：粘贴 → 解析 → 确认 ----
const quickText = ref('')
const quickItems = ref([])
const quickBusy = ref(false)
const quickMessage = ref('')
const quickErr = ref(false)
const pendingKeyword = ref('')
let parseTimer = null

// ---- 手动表单（编辑已有链接时才需要露面）----
const form = reactive({
  keyword: '',
  aliases: '',
  title: '',
  platform: '',
  shareUrl: '',
  sharePwd: '',
  fileCount: 0,
  sizeGb: '',
  note: '',
  enabled: true,
})

const platforms = Object.keys(PLATFORM_NAMES).map((k) => ({ key: k, name: PLATFORM_NAMES[k] }))

const tag = computed(() => `${stats.value.enabled || 0} / ${stats.value.total || 0} 条启用`)
const statItems = computed(() => [
  { k: '链接总数', v: stats.value.total || 0 },
  { k: '启用中', v: stats.value.enabled || 0 },
  { k: '未接住的词', v: stats.value.misses || 0 },
  { k: '自有点击占比', v: (props.ownRatio * 100).toFixed(1) + '%' },
])
const missRows = computed(() => misses.value.map((m) => ({ label: m.q, n: m.n, q: m.q })))
const quickReady = computed(() => quickItems.value.filter((it) => it.checked && !it.dupId))
const quickHint = computed(() => {
  if (quickBusy.value) return '识别中…'
  if (!quickItems.value.length) return ''
  return `识别到 ${quickItems.value.length} 条，可保存 ${quickReady.value.length} 条`
})

function flash(text, isErr) {
  quickMessage.value = text
  quickErr.value = !!isErr
  if (text) setTimeout(() => { if (quickMessage.value === text) quickMessage.value = '' }, 5000)
}

async function reload() {
  loading.value = true
  try {
    const d = await api.ownLinks(props.days)
    links.value = d.links || []
    misses.value = d.misses || []
    stats.value = d.stats || {}
    error.value = ''
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

// ---- 快速补录 ----

function onQuickInput() {
  clearTimeout(parseTimer)
  const text = quickText.value.trim()
  if (!text) {
    quickItems.value = []
    quickMessage.value = ''
    return
  }
  parseTimer = setTimeout(runPreview, 500)
}

async function runPreview() {
  const text = quickText.value.trim()
  if (!text) return
  quickBusy.value = true
  try {
    const d = await api.parseOwnShare(text, pendingKeyword.value)
    quickItems.value = (d.items || []).map((it) => ({ ...it, checked: !it.dupId }))
    if (!quickItems.value.length) flash('没认出链接——粘贴里得有 http(s) 开头的地址', true)
    else if (quickMessage.value.startsWith('没认出')) flash('')
  } catch (e) {
    flash(e.message, true)
  } finally {
    quickBusy.value = false
  }
}

/** 每行的状态徽章：已收录 / 过期 / 缺提取码 / 缺关键词 */
function statusOf(it) {
  if (it.dupId) return { text: '已收录，跳过', tone: 'muted' }
  if (it.expiredAt && it.expiredAt * 1000 < Date.now()) return { text: '分享已过期', tone: 'bad' }
  if (it.needPwd) return { text: '需要提取码', tone: 'bad' }
  if (!it.keyword.trim()) return { text: '填个关键词', tone: 'bad' }
  if (it.error) return { text: it.error, tone: 'warn' }
  return { text: '可保存', tone: 'ok' }
}

function expiryText(it) {
  if (!it.expiredAt) return ''
  if (it.expiredAt > 4102416000) return '永久'
  const d = new Date(it.expiredAt * 1000)
  const days = Math.round((d.getTime() - Date.now()) / 86400000)
  if (days < 0) return '已过期'
  return `${d.getMonth() + 1}-${d.getDate()} 到期${days <= 7 ? `（剩 ${days} 天）` : ''}`
}

function dropRow(i) {
  quickItems.value.splice(i, 1)
}

async function saveQuick() {
  const items = quickReady.value
  if (!items.length) return flash('没有可保存的条目', true)
  const noKeyword = items.find((it) => !it.keyword.trim())
  if (noKeyword) return flash(`这条还没关键词：${noKeyword.title || noKeyword.url}`, true)
  saving.value = true
  flash(`保存 ${items.length} 条…`)
  try {
    const d = await api.bulkSaveOwnLinks(
      items.map((it) => ({
        keyword: it.keyword.trim(),
        aliases: '',
        title: it.title || '',
        platform: it.platform || '',
        shareUrl: it.url,
        sharePwd: it.pwd || '',
        fileCount: Number(it.fileCount) || 0,
        sizeBytes: Number(it.sizeBytes) || 0,
        note: '',
        enabled: true,
      }))
    )
    const ok = (d.saved || []).length
    const bad = d.errors || []
    quickText.value = ''
    quickItems.value = []
    pendingKeyword.value = ''
    await reload()
    flash(bad.length ? `已保存 ${ok} 条，${bad.length} 条失败：${bad[0].msg}` : `已保存 ${ok} 条，下次搜索即生效`, bad.length > 0)
  } catch (e) {
    flash(e.message, true)
  } finally {
    saving.value = false
  }
}

// ---- 手动表单 ----

function reset() {
  editingId.value = null
  form.keyword = ''
  form.aliases = ''
  form.title = ''
  form.platform = ''
  form.shareUrl = ''
  form.sharePwd = ''
  form.fileCount = 0
  form.sizeGb = ''
  form.note = ''
  form.enabled = true
}

function edit(link) {
  editingId.value = link.id
  form.keyword = link.keyword || ''
  form.aliases = link.aliases || ''
  form.title = link.title || ''
  form.platform = link.platform || ''
  form.shareUrl = link.share_url || ''
  form.sharePwd = link.share_pwd || ''
  form.fileCount = Number(link.file_count) || 0
  const gb = (Number(link.size_bytes) || 0) / 1073741824
  form.sizeGb = gb > 0 ? gb.toFixed(2) : ''
  form.note = link.note || ''
  form.enabled = !!link.enabled
  manualOpen.value = true
  if (panel.value) panel.value.show()
  nextTick(() => {
    const el = document.getElementById('own-manual')
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}

/** 从清单/未命中榜点「补录」：带上那个词、展开、聚焦粘贴框 */
function fillForm(q) {
  editingId.value = null
  pendingKeyword.value = q
  manualOpen.value = false
  if (panel.value) panel.value.show()
  // 已经贴过内容时，带上新词重算一次，别让人再手动戳「重新识别」
  if (quickText.value.trim()) runPreview()
  requestAnimationFrame(() => {
    if (quickInput.value) {
      quickInput.value.focus()
      quickInput.value.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  })
}

async function save() {
  if (!form.keyword.trim()) return flash('主关键词不能为空', true)
  if (!form.shareUrl.trim()) return flash('分享链接不能为空', true)
  const gb = Number(form.sizeGb)
  saving.value = true
  flash('保存中…')
  try {
    await api.saveOwnLink({
      id: editingId.value || null,
      keyword: form.keyword.trim(),
      aliases: form.aliases.trim(),
      title: form.title.trim(),
      platform: form.platform,
      shareUrl: form.shareUrl.trim(),
      sharePwd: form.sharePwd.trim(),
      fileCount: Number(form.fileCount) || 0,
      sizeBytes: Number.isFinite(gb) && gb > 0 ? Math.round(gb * 1073741824) : 0,
      note: form.note.trim(),
      enabled: form.enabled,
    })
    flash('已保存，下次搜索即生效')
    reset()
    await reload()
  } catch (e) {
    flash(e.message, true)
  } finally {
    saving.value = false
  }
}

async function remove(link) {
  if (!window.confirm('删除这条精选链接？删除后立刻从搜索结果消失。')) return
  try {
    await api.deleteOwnLink(link.id)
    flash('已删除')
    await reload()
  } catch (e) {
    flash(e.message, true)
  }
}

function linkMeta(link) {
  const meta = [platformName(link.platform)]
  if (link.share_pwd) meta.push('码 ' + link.share_pwd)
  if (Number(link.file_count) > 0) meta.push(link.file_count + ' 个文件')
  const size = sizeText(link.size_bytes)
  if (size) meta.push(size)
  return meta.join(' · ')
}

function linkWords(link) {
  return [link.keyword].concat((link.aliases || '').split(',')).filter(Boolean).join(' / ')
}

onMounted(reload)
watch(() => props.days, reload)
defineExpose({ reload, fillForm })
</script>

<template>
  <Panel ref="panel" title="精选链接" :tag="tag">
    <StatGrid :items="statItems" />

    <div v-if="error" class="err">读取失败：{{ error }}</div>

    <div class="grid2">
      <section class="rank">
        <h2>搜过但没接住的词 <span>点「补录」直接填进表单</span></h2>
        <BarList :rows="missRows" empty="最近没有接不住的词">
          <template #action="{ row }">
            <button class="fb-btn" @click="fillForm(row.q)">补录</button>
          </template>
        </BarList>
      </section>
      <section class="rank">
        <h2>已上架 <span>命中即置顶展示</span></h2>
        <div v-if="!links.length" class="empty">还没有链接——转存第一条资源，录进来</div>
        <div v-for="link in links" :key="link.id" class="fb-item" :class="{ done: !link.enabled }">
          <div class="fb-head">
            <span class="fb-kind missing">精选</span>
            <span class="fb-title" :title="link.title || link.keyword">{{ link.title || link.keyword }}</span>
            <span class="t">{{ link.enabled ? '启用中' : '已停用' }}</span>
          </div>
          <div class="fb-meta">{{ linkMeta(link) }}</div>
          <div class="fb-meta">词：{{ linkWords(link) }}</div>
          <div class="fb-actions">
            <button class="fb-btn" @click="edit(link)">编辑</button>
            <button class="fb-btn" @click="remove(link)">删除</button>
          </div>
        </div>
      </section>
    </div>

    <div class="rank" style="margin-top: 16px">
      <h2>快速补录 <span>粘贴分享链接或整段分享文字，其余字段自动识别</span></h2>

      <div v-if="pendingKeyword" class="quick-hint">
        正在补录清单里的词：<b>{{ pendingKeyword }}</b>
        <button class="fb-btn" style="margin-left: 8px" @click="pendingKeyword = ''">不用这个词</button>
      </div>

      <textarea
        ref="quickInput"
        v-model="quickText"
        class="quick-box"
        rows="4"
        spellcheck="false"
        placeholder="例：我用夸克网盘给你分享了「尸兄 1-4季」，链接：https://pan.quark.cn/s/xxxx 提取码：abcd
可一次粘多条（每条之间空一行，或直接连着贴）"
        @input="onQuickInput"
      ></textarea>

      <div class="quick-bar">
        <span class="quick-status" :class="{ bad: quickErr }">{{ quickHint || quickMessage }}</span>
        <button class="grp-save" :disabled="saving || !quickReady.length" @click="saveQuick">
          保存{{ quickReady.length ? ` ${quickReady.length} 条` : '' }}
        </button>
        <button class="fb-btn" :disabled="quickBusy || !quickText" @click="runPreview">重新识别</button>
      </div>

      <div v-if="quickItems.length" class="quick-list">
        <div v-for="(it, i) in quickItems" :key="it.url" class="quick-row" :class="{ off: it.dupId }">
          <input v-model="it.checked" type="checkbox" :disabled="!!it.dupId" />
          <div class="quick-main">
            <div class="quick-title">
              <span class="fb-kind">{{ it.platformName }}</span>
              <span class="fb-title">{{ it.title || '（没取到标题）' }}</span>
              <span class="quick-badge" :class="statusOf(it).tone">{{ statusOf(it).text }}</span>
            </div>
            <div class="quick-meta">
              <span v-if="it.pwd">提取码 {{ it.pwd }}</span>
              <span v-else>无提取码</span>
              <span v-if="it.fileCount">{{ it.fileCount }} 个文件 · {{ sizeText(it.sizeBytes) }}</span>
              <span v-if="expiryText(it)">{{ expiryText(it) }}</span>
            </div>
            <div class="quick-meta url">{{ it.url }}</div>
          </div>
          <input
            v-model="it.keyword"
            class="quick-kw"
            maxlength="60"
            placeholder="主关键词（必填）"
          />
          <button class="fb-btn" @click="dropRow(i)">移除</button>
        </div>
      </div>

      <div v-if="quickMessage && quickItems.length" class="quick-msg" :class="{ err: quickErr }">{{ quickMessage }}</div>

      <details class="quick-manual" :open="manualOpen" id="own-manual" @toggle="manualOpen = $event.target.open">
        <summary>手动录入 / 编辑已有链接</summary>
        <div class="grp-grid" style="margin-top: 12px">
          <label>主关键词 <span style="color: var(--red)">*</span>
            <input v-model="form.keyword" maxlength="60" placeholder="例：尸兄" />
          </label>
          <label>别名（逗号分隔，可空）
            <input v-model="form.aliases" maxlength="300" placeholder="例：尸兄动漫,尸兄漫画" />
          </label>
          <label>分享标题（卡片上显示，可空则用主关键词）
            <input v-model="form.title" maxlength="200" placeholder="例：尸兄 1-4季 全集合集" />
          </label>
          <label>平台
            <select v-model="form.platform">
              <option value="">（不指定平台）</option>
              <option v-for="p in platforms" :key="p.key" :value="p.key">{{ p.name }}</option>
            </select>
          </label>
          <label>分享链接 <span style="color: var(--red)">*</span>
            <input v-model="form.shareUrl" maxlength="500" placeholder="https://pan.quark.cn/s/…" />
          </label>
          <label>提取码（可空）
            <input v-model="form.sharePwd" maxlength="20" />
          </label>
          <div class="grp-grid3" style="margin-bottom: 0; grid-column: 1 / -1">
            <label>文件数
              <input v-model.number="form.fileCount" type="number" min="0" />
            </label>
            <label>大小（GB，可小数）
              <input v-model="form.sizeGb" type="number" min="0" step="0.1" />
            </label>
            <label>备注（卡片小字，可空）
              <input v-model="form.note" maxlength="200" placeholder="例：每周更新" />
            </label>
          </div>
        </div>
        <div class="grp-actions" style="margin-top: 10px">
          <label style="flex-direction: row; align-items: center; gap: 6px; font-size: 13px; color: var(--ink-2)">
            <input v-model="form.enabled" type="checkbox" /> 启用
          </label>
          <button class="grp-save" :disabled="saving" @click="save">保存</button>
          <button v-if="editingId" class="grp-btn" @click="reset">取消编辑</button>
          <span class="grp-msg" :class="{ err: quickErr }">{{ quickMessage }}</span>
        </div>
      </details>
    </div>
  </Panel>
</template>
