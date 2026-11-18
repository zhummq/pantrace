<script setup>
import { computed, ref } from 'vue'
import { track } from '../track.js'

const props = defineProps({
  item: { type: Object, required: true },
  keyword: { type: String, default: '' },
  // 为 true 时「打开链接」的点击交给父组件决定（群弹窗拦截一次）。
  // Ctrl/Cmd/中键不受影响，直接走浏览器默认行为。
  intercept: { type: Boolean, default: false },
})

const emit = defineEmits(['feedback', 'open'])

// 平台品牌色：这是数据，不是主题色——每个网盘有固定的识别色，
// 不参与 tokens 体系（改主题不该让夸克变成别的颜色）。
// 与后端 PLATFORMS 列表保持一致（main.py）。多一个少一个 key 都会让
// 对应平台的色条/徽章掉进默认灰。
const PLATFORM_COLORS = {
  quark: '#2B6CF6',
  baidu: '#3A6BF5',
  ali: '#7C4DFF',
  uc: '#FF7A00',
  xunlei: '#00A3FF',
  115: '#00A870',
  123: '#00B4D8',
  tianyi: '#1F7AFF',
  yidong: '#00A5A8',
  wo: '#E4002B',
  guangya: '#F5A524',
  lanzou: '#4C9AFF',
  weiyun: '#12B7F5',
  ctfile: '#8C6D31',
}

const color = computed(() => PLATFORM_COLORS[props.item.platform] || 'var(--slate-400)')

// 徽章底色/描边由平台色推导，用 color-mix 而不是拼十六进制——
// 拼字符串遇到 var(--slate-400) 这种值会直接拼出非法色号。
const badgeStyle = computed(() => ({
  color: color.value,
  background: `color-mix(in srgb, ${color.value} 8%, var(--surface))`,
  borderColor: `color-mix(in srgb, ${color.value} 24%, var(--surface))`,
}))

// 站内跳转地址（/go/xxx）与外部链接都放行；伪协议一律挡掉。
// 上游结果现在给的都是 /go/ 签名地址——真实链接不再下发到浏览器。
const safeUrl = computed(() => {
  const url = props.item.shareUrl || ''
  if (url.startsWith('/go/')) return url
  return /^https?:\/\//i.test(url) ? url : ''
})

function escapeHtml(str) {
  return String(str).replace(
    /[&<>"']/g,
    (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[m]
  )
}

const titleHtml = computed(() => {
  const safe = escapeHtml(props.item.title || '（无标题）')
  const kw = props.keyword.trim()
  if (!kw) return safe
  const pattern = escapeHtml(kw).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return safe.replace(new RegExp(`(${pattern})`, 'gi'), '<mark class="hl">$1</mark>')
})

const sizeText = computed(() => {
  const bytes = Number(props.item.sizeBytes) || 0
  if (bytes <= 0) return '大小未知'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let n = bytes
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i += 1
  }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`
})

const fileCountText = computed(() => {
  const n = Number(props.item.fileCount) || 0
  return n > 0 ? `${n} 个文件` : ''
})

const copied = ref('')

// 后端异步探活的结果，只透传结论，不下发上游原始文案。
//
// ok 必须显示。早先的判断是"每条都挂已核验等于噪声"，实际跑出来是反的：
// 全静默时用户分不清"验过且正常"和"压根没验"，整页看着像功能没生效。
// 现在改成 ok 给一个绿色的「可访问」——文案避开"有效"，因为右边的橙色徽标
// 说的是转存链接 30 分钟有效期，两件事挤一个词会互相稀释。
//
// uncertain / unsupported 一律不显示：没有结论就别下结论，也别假装验过。
const checkChip = computed(() => {
  const state = props.item.checkState
  if (state === 'ok') {
    return { tone: 'ok', text: '可访问', tip: '已实测：该分享当前可以正常打开。' }
  }
  if (state === 'bad') {
    return { tone: 'bad', text: '已失效', tip: '实测该分享已无法访问，可能已被删除或取消。' }
  }
  if (state === 'locked') {
    return { tone: 'locked', text: '需提取码', tip: '该分享需要提取码，但原始结果里没有带。' }
  }
  return null
})

// 点击走 beacon：链接是 target=_blank，但用户可能秒关标签页，
// 常规 fetch 不保证发得出去。这是转化率指标的源头，不能丢。
function onClick() {
  track(
    'click',
    {
      target: props.item.title || '',
      platform: props.item.platform || '',
      // shareUrl 是 /go/ 签名串，后端解 token 落库成真实网盘链接
      url: props.item.shareUrl || '',
      query: props.keyword,
    },
    { beacon: true }
  )
}

// 「打开链接」/标题链接：先记一次点击（beacon，跳走前发出去），
// 拦截开关开着时把导航交给父组件（第一次点击先弹群），否则维持原生 <a> 行为。
// Ctrl/Cmd/Shift 点击照常——想多开的人不该被拦。
function onOpen(e) {
  onClick()
  if (!props.intercept || e.metaKey || e.ctrlKey || e.shiftKey) return
  e.preventDefault()
  emit('open', props.item)
}

async function copyText(text, tag) {
  if (!text) return false
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const el = document.createElement('textarea')
      el.value = text
      document.body.appendChild(el)
      el.select()
      document.execCommand('copy')
      document.body.removeChild(el)
    }
    copied.value = tag
    setTimeout(() => {
      if (copied.value === tag) copied.value = ''
    }, 1600)
    return true
  } catch {
    copied.value = ''
    return false
  }
}

async function onCopyPwd() {
  track('copy_pwd', {
    target: props.item.title || '',
    platform: props.item.platform || '',
    query: props.keyword,
  })
  await copyText(props.item.sharePwd, 'pwd')
}
</script>

<template>
  <div class="card-unit">
    <article class="card">
      <span class="bar" :style="{ background: color }" aria-hidden="true"></span>

      <div class="main">
        <div class="head">
          <span class="badge" :style="badgeStyle">
            {{ item.platformName || '未知平台' }}
          </span>
          <a
            v-if="safeUrl"
            class="title"
            :href="safeUrl"
            target="_blank"
            rel="noopener noreferrer"
            @click="onOpen"
            v-html="titleHtml"
          ></a>
          <span v-else class="title" v-html="titleHtml"></span>
        </div>

        <div class="meta">
          <span v-if="fileCountText">{{ fileCountText }}</span>
          <i v-if="fileCountText" class="dot"></i>
          <span>{{ sizeText }}</span>
          <!-- 存活检测结论：ok 给绿色对勾，失效/需提取码给红/橙，
               没有任何结论（未检、unsupported）就不出现 -->
          <span
            v-if="checkChip"
            class="state-chip"
            :class="checkChip.tone"
            :title="checkChip.tip"
          >
            <svg
              v-if="checkChip.tone === 'ok'"
              width="10"
              height="10"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="3"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path d="M4 12.5l5 5L20 6.5" />
            </svg>{{ checkChip.text }}</span>
          <!-- 时效提示收敛成一个小徽标：完整说明整页只出现一次（列表顶部），
               悬停可看全文；一页十条整句提示等于免责声明刷屏 -->
          <span
            v-if="safeUrl"
            class="ttl-chip"
            title="网盘链接有效期为 30 分钟，请及时转存；失效后可重新获取。"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <path d="M12 7.5V12l3 2" />
            </svg>
            <b>30 分钟</b>内有效
          </span>
          <template v-if="item.sharePwd">
            <i class="dot"></i>
            <span class="pwd">提取码 <b>{{ item.sharePwd }}</b></span>
          </template>
          <!-- 纠错入口挂在卡片内部：用户判断"这条有问题"的时机只在这一屏。
               位置固定在元信息行末尾（不用 margin-left:auto 顶到右边），
               否则它会随徽标多少左右漂，看起来像排版错位。 -->
          <button class="flag" @click="emit('feedback', item)">这条有问题</button>
        </div>
      </div>

      <div class="actions">
        <button
          v-if="item.sharePwd"
          class="btn ghost"
          :class="{ done: copied === 'pwd' }"
          @click="onCopyPwd"
        >
          {{ copied === 'pwd' ? '已复制' : '复制提取码' }}
        </button>
        <a
          v-if="safeUrl"
          class="btn solid"
          :href="safeUrl"
          target="_blank"
          rel="noopener noreferrer"
          @click="onOpen"
        >
          打开链接
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M7 17 17 7M9 7h8v8" />
          </svg>
        </a>
      </div>
    </article>
  </div>
</template>

<style scoped>
.card-unit {
  display: block;
}

.card {
  position: relative;
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 16px 18px 16px 22px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-xs);
  transition:
    border-color var(--dur-2) var(--ease),
    box-shadow var(--dur-2) var(--ease),
    transform var(--dur-2) var(--ease);
}

.card:hover {
  border-color: var(--line-2);
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}

/* 平台色只留一条 3px 标记，不做整块染色 */
.bar {
  position: absolute;
  left: 0;
  top: 14px;
  bottom: 14px;
  width: 3px;
  border-radius: 0 3px 3px 0;
}

.main {
  flex: 1;
  min-width: 0;
}

.head {
  display: flex;
  align-items: baseline;
  gap: 9px;
  flex-wrap: wrap;
}

.badge {
  flex-shrink: 0;
  padding: 1px 7px;
  border: 1px solid transparent;
  border-radius: var(--radius-xs);
  background: transparent;
  font-size: var(--text-2xs);
  font-weight: 600;
  letter-spacing: 0.02em;
}

.title {
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  font-size: 15.5px;
  font-weight: 600;
  line-height: 1.5;
  letter-spacing: -0.005em;
  color: var(--ink);
  word-break: break-word;
  transition: color var(--dur-1) var(--ease);
}

a.title:hover {
  color: var(--brand-ink);
}

.meta {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: var(--text-sm);
  color: var(--muted);
}

.dot {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--line-2);
}

.pwd b {
  font-family: var(--mono);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--ink-2);
}

/* 存活检测徽标 */
.state-chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 7px;
  border-radius: var(--radius-pill);
  font-size: var(--text-xs);
  line-height: 1.6;
  font-weight: 600;
  cursor: help;
  user-select: none;
}

.state-chip svg {
  flex-shrink: 0;
  opacity: 0.9;
}

/* 正常比异常淡：失效得跳出来，正常只是"验过" */
.state-chip.ok {
  color: var(--success-ink);
  background: var(--success-soft);
}

.state-chip.bad {
  color: var(--danger-ink);
  background: var(--danger-soft);
}

.state-chip.locked {
  color: var(--warn);
  background: var(--warn-soft);
}

/* 时效徽标：建议性提示，不抢主操作 */
.ttl-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1.5px 8px;
  border-radius: var(--radius-pill);
  background: var(--warn-soft);
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--warn);
  cursor: help;
  user-select: none;
}

.ttl-chip svg {
  flex-shrink: 0;
  opacity: 0.85;
}

.ttl-chip b {
  font-weight: 600;
}

.flag {
  margin-left: 4px;
  padding: 0;
  border: none;
  background: none;
  font-size: var(--text-xs);
  color: var(--faint);
  text-decoration: underline;
  text-decoration-color: var(--line-2);
  text-underline-offset: 3px;
}

.flag:hover {
  color: var(--danger);
  text-decoration-color: var(--danger-line);
}

.actions {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: var(--sp-2);
}

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  height: 34px;
  padding: 0 14px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  font-weight: 500;
  white-space: nowrap;
}

.btn.ghost {
  background: var(--surface);
  border-color: var(--line-2);
  color: var(--ink-2);
  box-shadow: var(--shadow-xs);
}

.btn.ghost:hover {
  border-color: var(--brand-300);
  background: var(--brand-25);
  color: var(--brand-ink);
}

.btn.ghost.done {
  background: var(--success-soft);
  border-color: var(--success);
  color: var(--success-ink);
}

.btn.solid {
  background: var(--brand);
  color: var(--on-brand);
  box-shadow: var(--shadow-brand);
}

.btn.solid:hover {
  background: var(--brand-hover);
}

.btn.solid:active {
  background: var(--brand-active);
}

/* ---------- 平板 860 ---------- */
@media (max-width: 860px) {
  .card {
    flex-direction: column;
    align-items: stretch;
    gap: var(--sp-3);
    padding: 14px 14px 14px 18px;
  }

  /* 触屏没有 hover，浮起效果没意义，还会糊掉滚动 */
  .card:hover {
    transform: none;
    box-shadow: var(--shadow-xs);
  }

  .title {
    font-size: 15px;
  }

  .meta {
    font-size: var(--text-xs);
  }

  .ttl-chip {
    font-size: var(--text-2xs);
  }

  .flag {
    text-decoration: none;
  }

  /* 触控目标 42px，两个按钮等分；只有一个时自动占满整行 */
  .actions .btn {
    flex: 1;
    height: 42px;
    font-size: var(--text-base);
  }
}
</style>
