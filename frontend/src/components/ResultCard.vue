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

// 与后端 PLATFORMS 列表保持一致（main.py）。多一个少一个 key 都会让
// 对应平台的色条/徽章掉进默认灰色。
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

const color = computed(() => PLATFORM_COLORS[props.item.platform] || '#96a1ad')

// 只放行 http/https，挡掉 javascript: 之类的伪协议
const safeUrl = computed(() => {
  const url = props.item.shareUrl || ''
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

// 点击走 beacon：链接是 target=_blank，但用户可能秒关标签页，
// 常规 fetch 不保证发得出去。这是转化率指标的源头，不能丢。
function onClick() {
  track(
    'click',
    {
      target: props.item.title || '',
      platform: props.item.platform || '',
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
  <article class="card">
    <span class="bar" :style="{ background: color }" aria-hidden="true"></span>

    <div class="main">
      <div class="head">
        <span class="badge" :style="{ color, borderColor: color + '40' }">
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
        <template v-if="item.sharePwd">
          <i class="dot"></i>
          <span class="pwd">提取码 <b>{{ item.sharePwd }}</b></span>
        </template>
        <!-- 纠错入口挂在卡片内部：用户判断"这条有问题"的时机只在这一屏，
             放到页脚或者独立页面就等于没有 -->
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
</template>

<style scoped>
.card {
  position: relative;
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 15px 16px 15px 20px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}

.card:hover {
  border-color: var(--line-2);
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}

/* 平台色只留一条 2px 标记，不做整块染色 */
.bar {
  position: absolute;
  left: 0;
  top: 12px;
  bottom: 12px;
  width: 2px;
  border-radius: 0 2px 2px 0;
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
  padding: 1px 6px;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  font-family: var(--mono);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
  transform: translateY(-1px);
}

.title {
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  font-size: 15px;
  font-weight: 550;
  line-height: 1.55;
  letter-spacing: -0.005em;
  color: var(--ink);
  word-break: break-word;
}

a.title:hover {
  color: var(--accent-deep);
}

.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: 12px;
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
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--ink-2);
}

.flag {
  margin-left: auto;
  padding: 0;
  border: none;
  background: none;
  font-size: 11.5px;
  color: var(--faint);
  text-decoration: underline;
  text-decoration-color: var(--line-2);
  text-underline-offset: 3px;
  transition: color 0.15s ease;
}

.flag:hover {
  color: var(--danger);
  text-decoration-color: #e6bdb8;
}

.actions {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 8px;
}

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  height: 32px;
  padding: 0 13px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  font-weight: 500;
  white-space: nowrap;
  transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease;
}

.btn.ghost {
  background: var(--surface);
  border-color: var(--line-2);
  color: var(--ink-2);
}

.btn.ghost:hover {
  border-color: var(--ink-2);
  color: var(--ink);
}

.btn.ghost.done {
  background: var(--accent-soft);
  border-color: rgba(0, 169, 157, 0.35);
  color: var(--accent-deep);
}

.btn.solid {
  background: var(--ink);
  color: #fff;
}

.btn.solid:hover {
  background: #1b2430;
}

@media (max-width: 760px) {
  .card {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
    padding: 14px 14px 14px 18px;
  }

  .card:hover {
    transform: none;
    box-shadow: none;
  }

  .title {
    font-size: 14.5px;
  }

  .meta {
    font-size: 11.5px;
  }

  .flag {
    text-decoration: none;
  }

  /* 触控目标 40px，两个按钮等分；只有一个时自动占满整行 */
  .actions {
    gap: 8px;
  }

  .actions .btn {
    flex: 1;
    height: 40px;
    font-size: 13px;
  }
}
</style>
