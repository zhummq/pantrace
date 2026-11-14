<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
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
const keywordInput = ref(null)

const loading = ref(true)
const error = ref('')
const message = ref('')
const messageErr = ref(false)
const links = ref([])
const misses = ref([])
const stats = ref({})
const editingId = ref(null)
const saving = ref(false)

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
const formTitle = computed(() =>
  editingId.value
    ? [`编辑 #${editingId.value}`, '改完保存即时生效']
    : ['新增链接', '转存到自己网盘后，把分享链接录进来']
)

function flash(text, isErr) {
  message.value = text
  messageErr.value = !!isErr
  if (text) setTimeout(() => { if (message.value === text) message.value = '' }, 4000)
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
  if (panel.value) panel.value.show()
}

/** 从清单/未命中榜点「补录」：展开面板、填词、聚焦、滚到表单 */
function fillForm(q) {
  editingId.value = null
  form.keyword = q
  if (panel.value) panel.value.show()
  requestAnimationFrame(() => {
    if (keywordInput.value) {
      keywordInput.value.focus()
      keywordInput.value.scrollIntoView({ behavior: 'smooth', block: 'center' })
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
      <h2>{{ formTitle[0] }} <span>{{ formTitle[1] }}</span></h2>
      <div class="grp-grid">
        <label>主关键词 <span style="color: var(--red)">*</span>
          <input ref="keywordInput" v-model="form.keyword" maxlength="60" placeholder="例：尸兄" />
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
        <div class="grp-grid3" style="margin-bottom: 0">
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
        <span class="grp-msg" :class="{ err: messageErr }">{{ message }}</span>
      </div>
    </div>
  </Panel>
</template>
