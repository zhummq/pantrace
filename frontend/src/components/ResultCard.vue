<script setup>
import { computed, ref } from 'vue'
import { track } from '../track.js'

const props = defineProps({
  item: { type: Object, required: true },
  keyword: { type: String, default: '' },
})

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

const color = computed(() => PLATFORM_COLORS[props.item.platform] || '#6b7a90')

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
    <span class="bar" :style="{ background: color }"></span>

    <div class="main">
      <div class="head">
        <span class="badge" :style="{ color, background: color + '14', borderColor: color + '33' }">
          {{ item.platformName || '未知平台' }}
        </span>
        <a
          v-if="safeUrl"
          class="title"
          :href="safeUrl"
          target="_blank"
          rel="noopener noreferrer"
          @click="onClick"
          v-html="titleHtml"
        ></a>
        <span v-else class="title" v-html="titleHtml"></span>
      </div>

      <div class="meta">
        <span v-if="fileCountText">{{ fileCountText }}</span>
        <span class="dot" v-if="fileCountText"></span>
        <span>{{ sizeText }}</span>
        <template v-if="item.sharePwd">
          <span class="dot"></span>
          <span class="pwd">提取码 <b>{{ item.sharePwd }}</b></span>
        </template>
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
        @click="onClick"
      >
        打开链接
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
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
  gap: 20px;
  padding: 18px 20px 18px 24px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
  transition: border-color 0.18s, box-shadow 0.18s, transform 0.18s;
}

.card:hover {
  border-color: var(--line-strong);
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}

.bar {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  opacity: 0.85;
}

.main {
  flex: 1;
  min-width: 0;
}

.head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.badge {
  flex-shrink: 0;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.2px;
  padding: 1px 8px;
  border-radius: 6px;
  border: 1px solid transparent;
  transform: translateY(-1px);
}

.title {
  font-size: 15.5px;
  font-weight: 550;
  color: var(--ink);
  line-height: 1.5;
  word-break: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

a.title:hover {
  color: var(--brand);
}

.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 7px;
  font-size: 12.5px;
  color: var(--muted);
  flex-wrap: wrap;
}

.dot {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--line-strong);
}

.pwd b {
  color: var(--ink-2);
  font-weight: 600;
  letter-spacing: 0.5px;
}

.actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 34px;
  padding: 0 14px;
  border-radius: 9px;
  font-size: 13px;
  font-weight: 500;
  border: 1px solid transparent;
  transition: all 0.16s;
  white-space: nowrap;
}

.btn.ghost {
  background: #fff;
  border-color: var(--line-strong);
  color: var(--ink-2);
}

.btn.ghost:hover {
  background: var(--brand-soft);
  border-color: #b7ddd5;
  color: var(--brand-dark);
}

.btn.ghost.done {
  background: var(--brand-soft);
  border-color: #b7ddd5;
  color: var(--brand-dark);
}

.btn.solid {
  background: var(--brand);
  color: #fff;
}

.btn.solid:hover {
  background: var(--brand-dark);
}

@media (max-width: 720px) {
  .card {
    flex-direction: column;
    align-items: stretch;
    gap: 14px;
    padding: 16px 16px 16px 20px;
  }

  .actions {
    justify-content: flex-end;
  }

  .btn {
    height: 32px;
  }
}
</style>
