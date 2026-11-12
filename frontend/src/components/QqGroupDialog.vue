<script setup>
// 群入口弹层。两种形态共用一个组件：
//   enter    —— 进站展示：二维码是主角
//   acquire  —— 用户点「打开链接」时拦截：先看群，再给「继续打开」的出口
// 群数据由后端 /api/group 下发，管理员在 /admin 看板里改，前端不写死。
import { computed, ref } from 'vue'
import { track } from '../track.js'

const props = defineProps({
  open: { type: Boolean, default: false },
  mode: { type: String, default: 'enter' }, // enter | acquire
  config: { type: Object, default: null },  // /api/group 返回的 data
})

const emit = defineEmits(['close', 'continue'])

const activeIndex = ref(0)
const copied = ref(false)

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

function close() {
  if (props.mode === 'enter') {
    track('group', { meta: { action: 'close', mode: 'enter' } })
  }
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
  padding: 20px;
  background: rgba(8, 12, 18, 0.46);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
}

.pt-panel {
  position: relative;
  width: 344px;
  max-width: 100%;
  max-height: calc(100vh - 40px);
  max-height: calc(100dvh - 40px);
  padding: 24px 24px 20px;
  overflow-y: auto;
  background: var(--surface, #fff);
  border: 1px solid var(--line, #e4e8ee);
  border-radius: var(--radius, 9px);
  box-shadow: var(--shadow-lg, 0 30px 70px -30px rgba(11, 16, 23, 0.45));
  text-align: center;
  animation: pt-in 0.18s ease-out;
}

@keyframes pt-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.pt-close {
  position: absolute;
  top: 10px;
  right: 10px;
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border: none;
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--faint, #96a1ad);
  font-size: 13px;
  cursor: pointer;
}

.pt-close:hover {
  background: var(--bg, #f5f7f9);
  color: var(--ink, #0b1017);
}

.pt-head {
  margin-bottom: 14px;
}

.pt-kicker {
  margin: 0 0 8px;
  font-size: 9px;
  letter-spacing: 0.18em;
  color: var(--faint, #96a1ad);
}

.pt-title {
  margin: 0 0 6px;
  font-size: 16.5px;
  font-weight: 650;
  letter-spacing: 0.01em;
  color: var(--ink, #0b1017);
}

.pt-desc {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--muted, #69747f);
}

.pt-tabs {
  display: flex;
  justify-content: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.pt-tab {
  height: 27px;
  padding: 0 11px;
  border: 1px solid var(--line, #e4e8ee);
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--ink-2, #2c3644);
  font-size: 12px;
  cursor: pointer;
}

.pt-tab:hover {
  border-color: var(--line-2, #ccd4de);
}

.pt-tab.on {
  background: var(--ink, #0b1017);
  border-color: var(--ink, #0b1017);
  color: #fff;
}

.pt-body {
  display: flex;
  flex-direction: column;
  align-items: center;
}

/* 二维码用取景框：四个角各一小段线，比投影更干净 */
.pt-qr {
  position: relative;
  width: 208px;
  height: 208px;
  padding: 8px;
  border: 1px solid var(--line, #e4e8ee);
  border-radius: var(--radius-sm, 6px);
  background: #fff;
}

.pt-qr::before,
.pt-qr::after {
  content: '';
  position: absolute;
  width: 12px;
  height: 12px;
  border: 1px solid var(--accent, #00a99d);
}

.pt-qr::before {
  top: -1px;
  left: -1px;
  border-right: 0;
  border-bottom: 0;
}

.pt-qr::after {
  right: -1px;
  bottom: -1px;
  border-left: 0;
  border-top: 0;
}

.pt-qr img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.pt-qr-empty {
  display: grid;
  place-items: center;
  height: 100%;
  font-size: 12px;
  color: var(--muted, #69747f);
}

.pt-number {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 13px;
  font-size: 12.5px;
}

.pt-num-label {
  font-size: 10px;
  letter-spacing: 0.1em;
  color: var(--faint, #96a1ad);
}

.pt-num {
  font-size: 14px;
  letter-spacing: 0.08em;
  color: var(--ink, #0b1017);
}

.pt-copy {
  height: 25px;
  padding: 0 10px;
  border: 1px solid var(--line-2, #ccd4de);
  border-radius: 5px;
  background: #fff;
  font-size: 11.5px;
  color: var(--ink-2, #2c3644);
  cursor: pointer;
}

.pt-copy:hover {
  border-color: var(--ink-2, #2c3644);
}

.pt-copy.done {
  border-color: rgba(0, 169, 157, 0.4);
  background: var(--accent-soft, #e7f6f4);
  color: var(--accent-deep, #07726b);
}

.pt-tip {
  margin: 10px 0 0;
  font-size: 11.5px;
  color: var(--muted, #69747f);
}

.pt-join {
  height: 38px;
  margin-top: 12px;
  padding: 0 18px;
  border: none;
  border-radius: var(--radius-sm, 6px);
  background: var(--ink, #0b1017);
  color: #fff;
  font-size: 12.5px;
  letter-spacing: 0.04em;
  cursor: pointer;
}

.pt-join:hover {
  background: #1b2430;
}

.pt-continue {
  width: 100%;
  height: 40px;
  margin-top: 14px;
  border: 1px solid var(--line-2, #ccd4de);
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  font-size: 12.5px;
  color: var(--ink-2, #2c3644);
  cursor: pointer;
}

.pt-continue:hover {
  border-color: var(--ink-2, #2c3644);
  color: var(--ink, #0b1017);
}

@media (max-width: 560px) {
  .pt-veil {
    align-items: end;
    padding: 0;
  }

  .pt-panel {
    width: 100%;
    max-height: 90dvh;
    padding: 22px 20px calc(18px + env(safe-area-inset-bottom));
    border: none;
    border-top: 1px solid var(--line, #e4e8ee);
    border-radius: 14px 14px 0 0;
  }

  .pt-join,
  .pt-continue {
    height: 44px;
  }
}
</style>
