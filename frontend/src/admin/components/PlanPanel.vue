<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import Panel from './Panel.vue'
import Pager from './Pager.vue'
import StatGrid from './StatGrid.vue'
import * as api from '../api'

const props = defineProps({
  days: { type: Number, default: 7 },
})
const emit = defineEmits(['fill'])

const panel = ref(null)
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const pages = ref(0)
const covered = ref(0)
const items = ref([])
const error = ref('')
const message = ref('')
const messageErr = ref(false)
const loading = ref(false)

const tag = computed(() =>
  total.value ? `${total.value} 个词待上架 · ${pages.value || 1} 页` : '暂时没有待办'
)
const stats = computed(() => [
  { k: '待上架词', v: total.value },
  { k: '已上架剔除', v: covered.value },
  { k: '区间', v: props.days + ' 天' },
])

function flash(text, isErr) {
  message.value = text
  messageErr.value = !!isErr
  if (text) setTimeout(() => { if (message.value === text) message.value = '' }, 4000)
}

async function reload() {
  loading.value = true
  try {
    const d = await api.plan(props.days, page.value, pageSize.value)
    items.value = d.items || []
    total.value = Number(d.total) || 0
    pages.value = Number(d.totalPages) || 0
    covered.value = Number(d.covered) || 0
    const current = Number(d.page) || page.value
    // 上架后总数变少，当前页可能越界——回退到最后一页重取
    if (!items.value.length && current > 1 && total.value > 0) {
      page.value = Math.max(1, pages.value)
      loading.value = false
      return reload()
    }
    page.value = current
    error.value = ''
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function onPage(p) {
  page.value = p
  reload()
  if (panel.value) panel.value.show()
}

function onSize(e) {
  pageSize.value = Number(e.target.value) || 20
  page.value = 1
  reload()
}

async function copy() {
  if (!items.value.length) return flash('清单是空的', true)
  const offset = (page.value - 1) * pageSize.value
  const lines = items.value.map((it, i) => {
    const u = Number(it.u) || 0
    return `${offset + i + 1}. ${it.q}  [${u > 0 ? u + ' 人 / ' : ''}${it.n} 次]`
  })
  const head = `上架清单 · 近 ${props.days} 天（按搜索人数）`
  const scope = total.value > items.value.length ? `（第 ${page.value} 页 · 共 ${total.value} 个词）` : ''
  const text = [head, scope, ...lines].filter(Boolean).join('\n')

  try {
    await navigator.clipboard.writeText(text)
    flash(`已复制 ${items.value.length} 条`)
  } catch (e) {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      flash(`已复制 ${items.value.length} 条`)
    } catch (err) {
      flash('复制失败，请手动选', true)
    }
    document.body.removeChild(ta)
  }
}

function fill(q) {
  emit('fill', q)
}

watch(() => props.days, () => { page.value = 1; reload() })
onMounted(reload)
defineExpose({ reload })
</script>

<template>
  <Panel ref="panel" title="上架清单" :tag="tag">
    <StatGrid :items="stats" />

    <div class="grp-actions" style="margin-bottom: 12px">
      <button class="fb-btn" @click="copy">复制本页清单</button>
      <label style="font-size: 11.5px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px">
        每页
        <select v-model.number="pageSize" @change="onSize" style="height: 26px; border-radius: 6px; border: 1px solid var(--line-strong); font-size: 11.5px; padding: 0 4px">
          <option :value="20">20 条</option>
          <option :value="50">50 条</option>
          <option :value="100">100 条</option>
        </select>
      </label>
      <span class="grp-msg" :class="{ err: messageErr }">{{ message }}</span>
    </div>

    <div v-if="error" class="err">清单读取失败：{{ error }}</div>

    <div class="rank">
      <h2>按搜索人数排序 <span>搜的人越多越先做 · 已上架的自动剔除</span></h2>
      <div v-if="!items.length" class="empty">清单是空的——要么都上架了，要么还没有搜索数据</div>
      <div v-for="(it, i) in items" :key="it.q" class="fb-item">
        <div class="fb-head">
          <span class="fb-title" :title="it.q">{{ (page - 1) * pageSize + i + 1 }}. {{ it.q }}</span>
          <span class="t">
            <template v-if="it.u">{{ it.u }} 人 · </template>{{ it.n }} 次搜索
          </span>
        </div>
        <div class="fb-actions">
          <a class="fb-btn" :href="'/?q=' + encodeURIComponent(it.q)" target="_blank" rel="noopener noreferrer">去搜一下</a>
          <button class="fb-btn" @click="fill(it.q)">补录</button>
        </div>
      </div>
      <Pager :page="page" :pages="pages" :total="total" :size="pageSize" @change="onPage" />
    </div>
  </Panel>
</template>
