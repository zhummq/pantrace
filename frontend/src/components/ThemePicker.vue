<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { THEMES, applyTheme, readTheme } from '../theme.js'

const open = ref(false)
const current = ref(readTheme())
const root = ref(null)

const active = computed(() => THEMES.find((t) => t.key === current.value) || THEMES[0])

function pick(key) {
  current.value = applyTheme(key)
  open.value = false
}

function onDocClick(e) {
  if (open.value && root.value && !root.value.contains(e.target)) open.value = false
}

function onKey(e) {
  if (e.key === 'Escape') open.value = false
}

onMounted(() => {
  // 首屏由 index.html 里的内联脚本先落地，这里只做兜底与状态对齐
  current.value = applyTheme(readTheme(), { persist: false })
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div ref="root" class="tp">
    <button
      class="tp-btn"
      type="button"
      :aria-expanded="open"
      aria-haspopup="menu"
      :aria-label="`配色主题：${active.name}`"
      title="配色主题"
      @click="open = !open"
    >
      <!-- 图标：半明半暗的圆（明暗/主题的通用符号）。
           别用调色板——16px 下它的镂空和缺口全糊掉，读出来像张笑脸 -->
      <svg class="tp-ic" width="15" height="15" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="8.3" fill="none" stroke="currentColor" stroke-width="1.9" />
        <path d="M12 3.7a8.3 8.3 0 0 1 0 16.6Z" fill="currentColor" />
      </svg>
      <span class="tp-label">配色</span>
      <svg class="tp-caret" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="m6 9 6 6 6-6" />
      </svg>
    </button>

    <transition name="tp-fade">
      <div v-if="open" class="tp-pop" role="menu">
        <div class="tp-head mono">主题 / THEME</div>
        <button
          v-for="t in THEMES"
          :key="t.key"
          class="tp-item"
          :class="{ on: t.key === current }"
          type="button"
          role="menuitemradio"
          :aria-checked="t.key === current"
          @click="pick(t.key)"
        >
          <span class="tp-swatch" :style="{ background: t.bg }">
            <i :style="{ background: t.accent }" />
          </span>
          <span class="tp-text">
            <span class="tp-name">{{ t.name }}</span>
            <span class="tp-desc">{{ t.desc }}</span>
          </span>
          <svg v-if="t.key === current" class="tp-check" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m5 13 4 4L19 7" />
          </svg>
        </button>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.tp {
  position: relative;
  display: inline-flex;
}

.tp-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 9px 0 8px;
  border: 1px solid var(--line-2);
  border-radius: var(--radius-pill);
  background: var(--surface-2);
  font-size: var(--text-sm);
  color: var(--ink-2);
}

.tp-btn:hover {
  border-color: var(--line-strong);
  background: var(--surface-3);
  color: var(--ink);
}

.tp-btn[aria-expanded='true'] {
  border-color: var(--brand-ring);
  background: var(--brand-soft);
  color: var(--brand-ink);
}

/* 图标跟着主题走：换一套配色，这个图标立刻跟着变色，等于给自己做了个预览 */
.tp-ic {
  flex: none;
  color: var(--brand);
}

.tp-label {
  line-height: 1;
  white-space: nowrap;
}

.tp-caret {
  flex: none;
  opacity: 0.55;
}

/* 手机上省掉文字，图标本身能认出来；不省就会把顶栏右侧挤满 */
@media (max-width: 560px) {
  .tp-label {
    display: none;
  }

  .tp-btn {
    padding: 0 7px;
  }
}

/* 浮层：贴按钮右侧对齐，箭头朝上，不抢视线。
   right 不能给负值——窄屏下会被顶出视口（顶栏右侧本来就是容器的右内边距） */
.tp-pop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  z-index: 60;
  width: 208px;
  padding: 6px;
  border: 1px solid var(--line-2);
  border-radius: var(--radius);
  background: var(--surface-float);
  box-shadow: var(--shadow-md), var(--inset-top);
}

.tp-head {
  padding: 6px 8px 8px;
  font-size: var(--text-2xs);
  letter-spacing: 0.1em;
  color: var(--faint);
}

.tp-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 7px 8px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  text-align: left;
}

.tp-item:hover {
  background: var(--surface-3);
}

.tp-item.on {
  border-color: var(--brand-ring);
  background: var(--brand-soft);
}

.tp-swatch {
  position: relative;
  flex: none;
  width: 22px;
  height: 22px;
  border: 1px solid var(--line-2);
  border-radius: var(--radius-xs);
  overflow: hidden;
}

/* 每套主题的小样：底色 + 一条强调色，比纯色块更能预告效果 */
.tp-swatch i {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 6px;
}

.tp-text {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  line-height: 1.35;
}

.tp-name {
  font-size: var(--text-base);
  color: var(--ink);
}

.tp-desc {
  font-size: var(--text-2xs);
  color: var(--faint);
}

.tp-check {
  flex: none;
  margin-left: auto;
  color: var(--brand);
}

.tp-item.on .tp-name {
  color: var(--brand-ink);
}

.tp-fade-enter-active,
.tp-fade-leave-active {
  transition: opacity var(--dur-2) var(--ease), transform var(--dur-2) var(--ease-out);
}

.tp-fade-enter-from,
.tp-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
