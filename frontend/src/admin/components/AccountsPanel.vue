<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import Panel from './Panel.vue'
import StatGrid from './StatGrid.vue'
import * as api from '../api'
import { platformName } from '../format'

/**
 * 每个网盘要什么凭据，前端先说清楚，别让人填一半被后端打回。
 * cookie: required / either / none   token: required / either / none
 * temp:   required / optional
 */
const PLATFORMS = [
  {
    key: 'quark',
    name: '夸克网盘',
    cookie: 'required',
    token: 'none',
    temp: 'optional',
    tip: '浏览器登录 pan.quark.cn，F12 → Network 随便点一个请求，整条 Cookie 复制过来（要有 __puus、__uid）。登录态会在请求中自动续期，填一次能用很久。',
  },
  {
    key: 'uc',
    name: 'UC 网盘',
    cookie: 'required',
    token: 'none',
    temp: 'optional',
    tip: '和夸克同源，去 drive.uc.cn 登录后复制整条 Cookie。',
  },
  {
    key: 'baidu',
    name: '百度网盘',
    cookie: 'either',
    token: 'either',
    temp: 'optional',
    tip: '两条路选一条：① Cookie 里带 BDUSS + STOKEN（网页登录后复制）；② 填 refresh_token（开放平台授权拿到的）。有 refresh_token 时 access_token 会自动换，过期也能自愈。',
  },
  {
    key: 'xunlei',
    name: '迅雷网盘',
    cookie: 'none',
    token: 'required',
    temp: 'optional',
    tip: '只认 refresh_token。每次请求都要现换 access_token 并带 x-captcha-token，所以刷新比较频繁，属于正常现象。',
  },
  {
    key: 'guangya',
    name: '光鸭云盘',
    cookie: 'none',
    token: 'required',
    temp: 'optional',
    tip: 'refresh_token 或 access_token 都行，有 refresh_token 才能自动续期。转存接口不回文件 id，靠目录前后对比找新文件，所以别在同一个临时目录里并发转存。',
  },
  {
    key: 'c139',
    name: '移动云盘',
    cookie: 'required',
    token: 'none',
    temp: 'required',
    tip: 'Cookie 里必须有 authorization 字段（整条复制过来）。请求体要 AES 加密，已内置。临时目录必填，而且要在盘里先建好——转存不会帮你建。',
  },
]

const PLATFORM_MAP = Object.fromEntries(PLATFORMS.map((p) => [p.key, p]))

const panel = ref(null)
const loading = ref(true)
const error = ref('')
const items = ref([])
const supported = ref([])
const stats = ref({})
const busyId = ref(0)
const saving = ref(false)
const message = ref('')
const messageErr = ref(false)
const editingId = ref(null)

const formOpen = ref(false)
const toolsOpen = ref(false)

const emptyForm = () => ({
  type: 'quark',
  name: '',
  cookie: '',
  access_token: '',
  refresh_token: '',
  temp_dir: '',
  expires_at: 0,
  status: 1,
})

const form = reactive(emptyForm())

// 手工验一条链接：不等真人点击，先自己把链路跑通
const probeUrl = ref('')
const probePwd = ref('')
const probeBusy = ref(false)
const probeResult = ref(null)

// 空间回收
const coldDays = ref(30)
const cleanBusy = ref(false)
const cleanResult = ref(null)

const pf = computed(() => PLATFORM_MAP[form.type] || PLATFORMS[0])
const showCookie = computed(() => pf.value.cookie !== 'none')
const showToken = computed(() => pf.value.token !== 'none')
const editing = computed(() => !!editingId.value)
const editingItem = computed(() => items.value.find((a) => a.id === editingId.value) || null)

const tag = computed(() => `${stats.value.enabled || 0} / ${stats.value.total || 0} 个启用`)
// 后端回的是类型 key（c139、guangya 这种），面板上给人看的是中文名
const supportedNameText = computed(() =>
  supported.value.map((k) => (PLATFORM_MAP[k] ? PLATFORM_MAP[k].name : platformName(k))).join(' / ')
)
const badCount = computed(() => items.value.filter((a) => a.failCount > 0 || a.lastError).length)
const statItems = computed(() => [
  { k: '账号总数', v: stats.value.total || 0 },
  { k: '启用中', v: stats.value.enabled || 0, hi: (stats.value.enabled || 0) > 0 },
  { k: '可用网盘', v: supported.value.length, hint: supportedNameText.value || '无' },
  { k: '待修复', v: badCount.value, hi: badCount.value > 0, hint: '失败过或登录态失效 · 越低越好' },
])

const typeOptions = computed(() =>
  PLATFORMS.filter((p) => supported.value.includes(p.key))
)

function flash(text, isErr) {
  message.value = text
  messageErr.value = !!isErr
  if (text) {
    const keep = text
    setTimeout(() => {
      if (message.value === keep) message.value = ''
    }, 6000)
  }
}

async function reload() {
  loading.value = true
  try {
    const d = await api.accounts()
    items.value = d.items || []
    stats.value = d
    supported.value = d.supported || []
    error.value = ''
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

// ---- 表单 ----

function resetForm() {
  editingId.value = null
  Object.assign(form, emptyForm())
  if (typeOptions.value.length && !supported.value.includes(form.type)) {
    form.type = typeOptions.value[0].key
  }
}

function openAdd() {
  resetForm()
  if (typeOptions.value.length) form.type = typeOptions.value[0].key
  formOpen.value = true
  if (panel.value) panel.value.show()
}

function edit(item) {
  editingId.value = item.id
  Object.assign(form, {
    type: item.type,
    name: item.name || '',
    cookie: '',
    access_token: '',
    refresh_token: '',
    temp_dir: item.tempDir || '',
    expires_at: item.expiresAt || 0,
    status: item.status,
  })
  formOpen.value = true
  if (panel.value) panel.value.show()
}

/** 换平台时把上一个平台的凭据清掉，否则会把夸克的 cookie 提交成迅雷的。 */
function onTypeChange() {
  form.cookie = ''
  form.access_token = ''
  form.refresh_token = ''
}

/** 提交前的拦截。比后端报错更早一步，也说得更具体。 */
function validate() {
  const p = pf.value
  const hasCookie = form.cookie.trim() !== ''
  const hasToken = form.access_token.trim() !== '' || form.refresh_token.trim() !== ''
  // 编辑时凭据留空表示"不改"，这里不能当缺失处理
  const cookieFilled = hasCookie || (editing.value && editingItem.value && editingItem.value.hasCookie)
  const tokenFilled = hasToken || (editing.value && editingItem.value && editingItem.value.hasToken)

  if (p.cookie === 'required' && !cookieFilled) return `${p.name}必须填 Cookie`
  if (p.token === 'required' && !tokenFilled) {
    return p.key === 'xunlei' ? '迅雷必须填 refresh_token（只填 access_token 活不过一小时）' : `${p.name}必须填 refresh_token 或 access_token`
  }
  if (p.key === 'baidu' && !cookieFilled && !tokenFilled) return '百度网盘 Cookie 和 refresh_token 至少填一个'
  if (p.temp === 'required' && !form.temp_dir.trim()) return `${p.name}必须填临时目录，而且要在盘里先建好`
  return ''
}

async function save() {
  const bad = validate()
  if (bad) return flash(bad, true)

  saving.value = true
  flash(editing.value ? '保存中…' : '添加中…')
  try {
    const payload = {
      type: form.type,
      name: form.name.trim(),
      cookie: form.cookie.trim(),
      access_token: form.access_token.trim(),
      refresh_token: form.refresh_token.trim(),
      temp_dir: form.temp_dir.trim(),
      expires_at: Number(form.expires_at) || 0,
      status: Number(form.status) || 0,
    }
    if (editing.value) {
      await api.updateAccount(editingId.value, payload)
      flash('已保存')
    } else {
      await api.addAccount(payload)
      flash('已添加，点「测试」验一下再上线')
    }
    resetForm()
    formOpen.value = false
    await reload()
  } catch (e) {
    flash(e.message, true)
  } finally {
    saving.value = false
  }
}

// ---- 行操作 ----

async function test(item) {
  busyId.value = item.id
  try {
    const d = await api.testAccount(item.id)
    await reload()
    if (d.ok) flash(`${label(item)} 连接正常`)
    else flash(`${label(item)} 不可用：${d.reason || '未知原因'}`, true)
  } catch (e) {
    flash(e.message, true)
  } finally {
    busyId.value = 0
  }
}

async function toggle(item) {
  busyId.value = item.id
  try {
    await api.updateAccount(item.id, { status: item.status ? 0 : 1 })
    await reload()
    flash(`${label(item)} 已${item.status ? '停用' : '启用'}`)
  } catch (e) {
    flash(e.message, true)
  } finally {
    busyId.value = 0
  }
}

async function remove(item) {
  if (!window.confirm(`删除账号「${label(item)}」？\n\n已转存的文件不会跟着删，但之后点击会换别的账号。`)) return
  busyId.value = item.id
  try {
    await api.deleteAccount(item.id)
    if (editingId.value === item.id) resetForm()
    await reload()
    flash('已删除')
  } catch (e) {
    flash(e.message, true)
  } finally {
    busyId.value = 0
  }
}

function label(item) {
  return `${platformName(item.type)}${item.name ? ' · ' + item.name : ' #' + item.id}`
}

function credText(item) {
  const parts = []
  if (item.hasCookie) parts.push('Cookie')
  if (item.hasToken) parts.push('Token')
  return parts.length ? parts.join(' + ') : '无凭据'
}

// ---- 手工验链 ----

async function runMirrorTest() {
  const url = probeUrl.value.trim()
  if (!url.startsWith('http')) return flash('先贴一条完整分享链接', true)
  probeBusy.value = true
  probeResult.value = null
  try {
    probeResult.value = await api.mirrorTest(url, probePwd.value.trim())
  } catch (e) {
    probeResult.value = { ok: false, reason: e.message, url: '' }
  } finally {
    probeBusy.value = false
    await reload()
  }
}

// ---- 空间回收 ----

async function runCleanup() {
  cleanBusy.value = true
  cleanResult.value = null
  try {
    cleanResult.value = await api.cleanupMirrors(Number(coldDays.value) || 0)
    const d = cleanResult.value
    flash(`回收完成：清 ${d.rows || 0} 行 / ${d.files || 0} 个文件`)
  } catch (e) {
    flash(e.message, true)
  } finally {
    cleanBusy.value = false
  }
}

onMounted(reload)
defineExpose({ reload, openAdd })
</script>

<template>
  <Panel ref="panel" title="网盘账号" :tag="tag">
    <StatGrid :items="statItems" />

    <div v-if="error" class="err">读取失败：{{ error }}</div>
    <div v-else-if="loading" class="empty">载入中…</div>

    <template v-else>
      <div v-if="!items.length" class="acct-blank">
        还没有任何账号。没有账号时，每次点击都会退回源站原始链接——转存、防失效、统计全都用不上。
        <div style="margin-top: 10px">
          <button class="grp-save" @click="openAdd">加第一个账号</button>
        </div>
      </div>

      <div v-for="item in items" :key="item.id" class="fb-item" :class="{ done: !item.status }">
        <div class="fb-head">
          <span class="fb-kind">{{ platformName(item.type) }}</span>
          <span class="fb-title">{{ item.name || '（未命名 #' + item.id + '）' }}</span>
          <span class="acct-cred">{{ credText(item) }}</span>
          <span v-if="!item.status" class="quick-badge">已停用</span>
          <span v-else-if="item.failCount > 0 || item.lastError" class="quick-badge bad">
            失败 {{ item.failCount }} 次
          </span>
          <span v-else class="quick-badge ok">正常</span>
          <span class="t">#{{ item.id }}</span>
        </div>

        <div class="fb-meta">
          临时目录：{{ item.tempDir || '（未设，用盘根目录）' }}
          <template v-if="item.expiresAt > 0">
            · 令牌到期 {{ new Date(item.expiresAt * 1000).toLocaleString('zh-CN', { hour12: false }) }}
          </template>
        </div>
        <div v-if="item.lastError" class="acct-err">{{ item.lastError }}</div>

        <div class="fb-actions">
          <button class="fb-btn" :disabled="busyId === item.id" @click="test(item)">
            {{ busyId === item.id ? '测试中…' : '测试' }}
          </button>
          <button class="fb-btn" @click="edit(item)">改配置</button>
          <button class="fb-btn" :disabled="busyId === item.id" @click="toggle(item)">
            {{ item.status ? '停用' : '启用' }}
          </button>
          <button class="fb-btn" :disabled="busyId === item.id" @click="remove(item)">删除</button>
        </div>
      </div>

      <div class="quick-bar" style="margin-top: 12px">
        <button class="grp-btn grp-add" @click="openAdd">添加账号</button>
        <span class="quick-status" :class="{ bad: messageErr }">{{ message }}</span>
      </div>

      <!-- 添加 / 编辑 -->
      <details class="quick-manual" :open="formOpen" @toggle="formOpen = $event.target.open">
        <summary>{{ editing ? '修改账号配置' : '添加账号' }}</summary>

        <div class="acct-tip">{{ pf.tip }}</div>

        <div class="grp-grid" style="margin-top: 12px">
          <label>网盘类型
            <select
              v-model="form.type"
              :disabled="editing"
              @change="onTypeChange"
            >
              <option v-for="p in typeOptions" :key="p.key" :value="p.key">{{ p.name }}</option>
            </select>
          </label>
          <label>备注名（随便起，方便区分多个号）
            <input v-model="form.name" maxlength="60" placeholder="例：夸克主号 / 备用" />
          </label>
        </div>

        <div v-if="showCookie" class="acct-field">
          <label>
            Cookie
            <span class="acct-req" :class="{ must: pf.cookie === 'required' }">{{ pf.cookie === 'required' ? '必填' : '二选一' }}</span>
            <span v-if="editing && editingItem && editingItem.hasCookie" class="acct-keep">已存着，留空表示不改</span>
          </label>
          <textarea
            v-model="form.cookie"
            class="quick-box acct-ta"
            rows="3"
            spellcheck="false"
            :placeholder="editing ? '留空 = 保留原来的 Cookie' : '粘贴整条 Cookie，不要只贴一半'"
          ></textarea>
        </div>

        <div v-if="showToken" class="grp-grid">
          <label>refresh_token
            <span class="acct-req" :class="{ must: pf.token === 'required' }">
              {{ pf.token === 'required' ? '必填' : '二选一' }}
            </span>
            <input
              v-model="form.refresh_token"
              maxlength="2000"
              :placeholder="editing ? '留空 = 不改' : '长期凭据，能自动换 access_token'"
            />
          </label>
          <label>access_token（可空，会自动刷新）
            <input
              v-model="form.access_token"
              maxlength="2000"
              :placeholder="editing ? '留空 = 不改' : '短期凭据'"
            />
          </label>
        </div>

        <div class="grp-grid">
          <label>
            临时目录
            <span class="acct-req" :class="{ must: pf.temp === 'required' }">{{ pf.temp === 'required' ? '必填' : '建议填' }}</span>
            <input v-model="form.temp_dir" maxlength="200" placeholder="例：pantrace_tmp" />
          </label>
          <label style="flex-direction: row; align-items: center; gap: 6px; padding-top: 18px">
            <input v-model="form.status" type="checkbox" :true-value="1" :false-value="0" />
            <span style="font-size: 13px; color: var(--ink-2)">启用这个账号</span>
          </label>
        </div>

        <div class="grp-actions">
          <button class="grp-save" :disabled="saving" @click="save">
            {{ editing ? '保存' : '添加' }}
          </button>
          <button v-if="editing" class="grp-btn" @click="resetForm">取消修改</button>
          <span class="grp-msg" :class="{ err: messageErr }">{{ message }}</span>
        </div>
      </details>

      <!-- 工具：手工验链 + 空间回收 -->
      <details class="quick-manual" :open="toolsOpen" @toggle="toolsOpen = $event.target.open">
        <summary>测试与维护</summary>

        <div class="acct-tool">
          <h3>手工验一条链接 <span>不经过前端，直接跑完整转存链路</span></h3>
          <p class="acct-hint">
            新配好账号先用它验一遍。会真的转存并建分享，产生的文件在临时目录里，可以用下面的回收清掉。
          </p>
          <div class="grp-grid">
            <label>分享链接
              <input v-model="probeUrl" maxlength="500" placeholder="https://pan.quark.cn/s/…" />
            </label>
            <label>提取码（可空）
              <input v-model="probePwd" maxlength="20" />
            </label>
          </div>
          <div class="grp-actions">
            <button class="grp-save" :disabled="probeBusy" @click="runMirrorTest">
              {{ probeBusy ? '转存中…' : '跑一遍' }}
            </button>
          </div>
          <div v-if="probeResult" class="acct-result" :class="{ bad: !probeResult.ok }">
            <template v-if="probeResult.ok">
              成功{{ probeResult.cached ? '（命中缓存，没真的转存）' : '' }}：
              <a :href="probeResult.url" target="_blank" rel="noopener" class="fb-link">{{ probeResult.url }}</a>
            </template>
            <template v-else>失败：{{ probeResult.reason || '未知原因' }}</template>
          </div>
        </div>

        <div class="acct-tool">
          <h3>空间回收 <span>删冷掉的映射，把盘里的文件拿回来</span></h3>
          <p class="acct-hint">
            只删两类：失败超过 7 天的行、零点击且超过下面天数的映射（连文件一起删）。
            有热度的映射一律不动——删了就是把用户手上的链接打死。
          </p>
          <div class="grp-grid" style="max-width: 340px">
            <label>冷映射天数
              <input v-model.number="coldDays" type="number" min="1" max="365" />
            </label>
          </div>
          <div class="grp-actions">
            <button class="grp-btn" :disabled="cleanBusy" @click="runCleanup">
              {{ cleanBusy ? '回收中…' : '立即回收' }}
            </button>
            <span class="acct-hint" style="margin: 0">日常每 6 小时自动跑一次，这里只是手动催一把</span>
          </div>
          <div v-if="cleanResult" class="acct-result">
            清掉 {{ cleanResult.rows || 0 }} 行、{{ cleanResult.files || 0 }} 个文件
            <template v-if="(cleanResult.errors || []).length">
              <div class="acct-err">
                {{ cleanResult.errors.length }} 条失败：{{ cleanResult.errors[0] }}
              </div>
            </template>
          </div>
        </div>
      </details>
    </template>
  </Panel>
</template>
