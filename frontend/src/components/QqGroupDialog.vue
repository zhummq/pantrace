<script setup>
// 群入口弹层。两种形态共用一个组件：
//   enter    —— 进站展示：二维码是主角
//   acquire  —— 用户点「打开链接」时拦截：先看群，再给「继续打开」的出口
// 群数据由后端 /api/group 下发，管理员在 /admin 看板里改，前端不写死。
import { computed, ref, watch } from 'vue'
import { track } from '../track.js'

const props = defineProps({
  open: { type: Boolean, default: false },
  mode: { type: String, default: 'enter' }, // enter | acquire
  config: { type: Object, default: null },  // /api/group 返回的 data
})

const emit = defineEmits(['close', 'continue', 'mute'])

const activeIndex = ref(0)
const copied = ref(false)
// 「今日不再弹出」：两种形态各自管自己那一摊，互不牵连——
// enter 里勾上只是今天不再自动弹进站弹窗；acquire 里勾上只是今天点链接不再拦。
// 共用一个标记会让"顺手关掉进站弹窗"的人把拦截也一起关掉，用户并没有对这个表态。
const muteToday = ref(false)

const muteLabel = computed(() =>
  props.mode === 'acquire' ? '今日点击链接不再提示' : '今日不再弹出'
)

// 每次重新打开都复位：上次勾过没关掉、或者手动打开弹窗，都不该带着上次的勾
watch(
  () => props.open,
  (v) => {
    if (v) {
      muteToday.value = false
      activeIndex.value = 0
      copied.value = false
    }
  }
)

const enabledGroups = computed(() => (props.config?.groups || []).filter((g) => g.enabled !== false))
const active = computed(() => enabledGroups.value[activeIndex.value] || null)
const showTabs = computed(() => enabledGroups.value.length > 1)

const typeLabel = computed(() => {
  const map = { qq: 'QQ 群', wechat: '微信群', other: '其他' }
  return map[active.value?.type] || '交流群'
})

const tipText = computed(() => {
  if (active.value?.note) return active.value.note
  if (active.value?.type === 'wechat') return '微信扫一扫二维码即可加入'
  return props.config?.tip || '扫一扫二维码即可加入'
})

// 勾了「今日不再弹出」就把标记交出去，父组件负责落 localStorage。
// ✕、点遮罩、「直接打开资源」三条出口都走这里——用户勾了就是勾了，不看他从哪条路关。
function commitMute() {
  if (!muteToday.value) return
  track('group', { meta: { action: 'mute_today', mode: props.mode } })
  emit('mute', props.mode)
}

function close() {
  if (props.mode === 'enter') {
    track('group', { meta: { action: 'close', mode: 'enter' } })
  }
  commitMute()
  emit('close')
}

function copyNumber() {
  const n = active.value?.number || ''
  if (!n) return
  const done = () => {
    copied.value = true
    setTimeout(() => (copied.value = false), 1600)
  }
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(n).then(done).catch(done)
  } else {
    const el = document.createElement('textarea')
    el.value = n
    document.body.appendChild(el)
    el.select()
    document.execCommand('copy')
    document.body.removeChild(el)
    done()
  }
  track('group', { meta: { action: 'copy_number', mode: props.mode } })
}

function openJoinLink() {
  const link = active.value?.link || ''
  if (!link) return
  track('group', { meta: { action: 'join_link', mode: props.mode } }, { beacon: true })
  window.open(link, '_blank', 'noopener')
}

function continueOpen() {
  track('group', { meta: { action: 'continue', mode: 'acquire' } }, { beacon: true })
  commitMute()
  emit('continue')
}

function switchTab(i) {
  if (i === activeIndex.value) return
  activeIndex.value = i
  copied.value = false
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open && config" class="pt-veil" @click.self="close">
      <div class="pt-panel" role="dialog" aria-modal="true">
        <button class="pt-close" aria-label="关闭" @click="close">✕</button>

        <div class="pt-head">
          <p class="pt-kicker mono">{{ mode === 'acquire' ? 'GROUP / BEFORE OPEN' : 'GROUP / INVITE' }}</p>
          <p class="pt-title">{{ config.title || '资源交流群' }}</p>
          <p v-if="config.desc" class="pt-desc">{{ config.desc }}</p>
        </div>

        <div v-if="showTabs" class="pt-tabs">
          <button
            v-for="(g, i) in enabledGroups"
            :key="i"
            class="pt-tab"
            :class="{ on: i === activeIndex }"
            @click="switchTab(i)"
          >
            {{ g.name }}
          </button>
        </div>

        <div v-if="active" class="pt-body">
          <div class="pt-qr">
            <img v-if="active.qr" :src="active.qr" alt="群二维码" />
            <div v-else class="pt-qr-empty">暂无二维码</div>
          </div>

          <div v-if="active.number" class="pt-number">
            <span class="pt-num-label mono">{{ typeLabel }}</span>
            <b class="pt-num mono">{{ active.number }}</b>
            <button class="pt-copy" :class="{ done: copied }" @click="copyNumber">
              {{ copied ? '已复制' : '复制' }}
            </button>
          </div>

          <p class="pt-tip">{{ tipText }}</p>

          <button v-if="active.link" class="pt-join" @click="openJoinLink">
            手机上点这里直接加群 →
          </button>
        </div>

        <button v-if="mode === 'acquire'" class="pt-continue" @click="continueOpen">
          不进群了，直接打开资源
        </button>

        <label class="pt-mute">
          <input v-model="muteToday" type="checkbox" />
          <span>{{ muteLabel }}</span>
        </label>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.pt-veil {
  position: fixed;
  inset: 0;
  z-index: 90;
  display: grid;
  place-items: center;
  padding: var(--sp-5);
  background: var(--veil);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
}

.pt-panel {
  position: relative;
  width: 352px;
  max-width: 100%;
  max-height: calc(100vh - 40px);
  max-height: calc(100dvh - 40px);
  padding: var(--sp-6);
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
  text-align: center;
  animation: pt-in var(--dur-3) var(--ease-out);
}

@keyframes pt-in {
  from {
    opacity: 0;
    transform: translateY(10px) scale(0.985);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.pt-close {
  position: absolute;
  top: 12px;
  right: 12px;
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--faint);
  font-size: var(--text-base);
}

.pt-close:hover {
  background: var(--surface-3);
  color: var(--ink);
}

.pt-head {
  margin-bottom: var(--sp-4);
}

.pt-kicker {
  display: inline-block;
  margin: 0 0 10px;
  padding: 2px 8px;
  border-radius: var(--radius-xs);
  background: var(--brand-soft);
  font-family: var(--sans);
  font-size: var(--text-2xs);
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--brand-ink);
}

.pt-title {
  margin: 0 0 6px;
  font-size: var(--text-lg);
  font-weight: 650;
  letter-spacing: -0.01em;
  color: var(--ink);
}

.pt-desc {
  margin: 0;
  font-size: var(--text-sm);
  line-height: 1.6;
  color: var(--muted);
  /* 两行时别把最后一个字甩到第二行 */
  text-wrap: pretty;
}

.pt-tabs {
  display: flex;
  justify-content: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: var(--sp-3);
}

.pt-tab {
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-pill);
  background: var(--surface);
  color: var(--ink-2);
  font-size: var(--text-sm);
}

.pt-tab:hover {
  border-color: var(--brand-ring);
  background: var(--brand-soft);
  color: var(--brand-ink);
}

.pt-tab.on {
  background: var(--brand-soft);
  border-color: var(--brand-ring);
  color: var(--brand-ink);
  font-weight: 600;
}

.pt-body {
  display: flex;
  flex-direction: column;
  align-items: center;
}

/* 二维码：干净的白底方框，靠圆角与描边收边，不再用取景框四角 */
.pt-qr {
  position: relative;
  width: 208px;
  height: 208px;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow-xs);
}

.pt-qr img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: var(--radius-xs);
}

.pt-qr-empty {
  display: grid;
  place-items: center;
  height: 100%;
  font-size: var(--text-sm);
  color: var(--muted);
}

.pt-number {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
  font-size: var(--text-sm);
}

.pt-num-label {
  font-size: var(--text-2xs);
  letter-spacing: 0.06em;
  color: var(--faint);
}

.pt-num {
  font-size: var(--text-base);
  letter-spacing: 0.06em;
  color: var(--ink);
}

.pt-copy {
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--line-2);
  border-radius: var(--radius-xs);
  background: var(--surface);
  font-size: var(--text-sm);
  color: var(--ink-2);
}

.pt-copy:hover {
  border-color: var(--brand-ring);
  background: var(--brand-soft);
  color: var(--brand-ink);
}

.pt-copy.done {
  border-color: var(--success);
  background: var(--success-soft);
  color: var(--success-ink);
}

.pt-tip {
  margin: 10px 0 0;
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--muted);
}

.pt-join {
  height: 40px;
  margin-top: var(--sp-3);
  padding: 0 var(--sp-5);
  border: none;
  border-radius: var(--radius);
  background: var(--brand);
  color: var(--on-brand);
  font-size: var(--text-sm);
  font-weight: 600;
  box-shadow: var(--shadow-brand);
}

.pt-join:hover {
  background: var(--brand-hover);
}

.pt-join:active {
  background: var(--brand-active);
}

.pt-continue {
  width: 100%;
  height: 42px;
  margin-top: var(--sp-4);
  border: 1px solid var(--line-2);
  border-radius: var(--radius);
  background: var(--surface);
  font-size: var(--text-sm);
  color: var(--ink-2);
}

.pt-continue:hover {
  border-color: var(--brand-ring);
  background: var(--brand-soft);
  color: var(--brand-ink);
}

/* 今日不再弹出：放最底下，字号压小，不抢二维码的视线 */
.pt-mute {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: var(--sp-4);
  font-size: var(--text-xs);
  color: var(--muted);
  cursor: pointer;
  user-select: none;
}

.pt-mute input {
  width: 14px;
  height: 14px;
  margin: 0;
  accent-color: var(--brand);
  cursor: pointer;
}

.pt-mute:hover {
  color: var(--muted);
}

/* ---------- 手机 560 ---------- */
@media (max-width: 560px) {
  .pt-veil {
    align-items: end;
    padding: 0;
  }

  .pt-panel {
    width: 100%;
    max-height: 90dvh;
    padding: var(--sp-5) var(--sp-5) calc(var(--sp-4) + env(safe-area-inset-bottom));
    border: none;
    border-top: 1px solid var(--line);
    border-radius: var(--radius-xl) var(--radius-xl) 0 0;
  }

  .pt-join,
  .pt-continue {
    height: 46px;
  }
}
</style>
