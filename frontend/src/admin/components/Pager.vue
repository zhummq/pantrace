<script setup>
import { computed } from 'vue'

const props = defineProps({
  page: { type: Number, default: 1 },
  pages: { type: Number, default: 0 },
  total: { type: Number, default: 0 },
  size: { type: Number, default: 20 },
})

const emit = defineEmits(['change'])

const from = computed(() => (props.total ? (props.page - 1) * props.size + 1 : 0))
const to = computed(() => Math.min(props.page * props.size, props.total))

// 页码窗口最多 7 个
const windowPages = computed(() => {
  const pages = props.pages
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1)
  let start = Math.max(1, props.page - 3)
  const end = Math.min(pages, start + 6)
  start = Math.max(1, end - 6)
  return Array.from({ length: end - start + 1 }, (_, i) => start + i)
})

function go(p) {
  if (p === props.page || p < 1 || p > props.pages) return
  emit('change', p)
}
</script>

<template>
  <div class="pager">
    <template v-if="total">
      <template v-if="pages > 1">
        <button class="fb-btn" :disabled="page <= 1" @click="go(page - 1)">‹ 上一页</button>
        <button
          v-for="p in windowPages"
          :key="p"
          class="fb-btn"
          :class="{ cur: p === page }"
          @click="go(p)"
        >{{ p }}</button>
        <button class="fb-btn" :disabled="page >= pages" @click="go(page + 1)">下一页 ›</button>
      </template>
      <span class="spacer"></span>
      <span class="range">{{ from }}–{{ to }} / 共 {{ total }} 个词 · 第 {{ page }}/{{ pages || 1 }} 页</span>
    </template>
  </div>
</template>
