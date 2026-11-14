<script setup>
import { ref } from 'vue'

const props = defineProps({
  title: { type: String, required: true },
  tag: { type: String, default: '' },
  open: { type: Boolean, default: false },
})

const emit = defineEmits(['first-open'])
const root = ref(null)
let opened = false

function onToggle(e) {
  if (e.target.open && !opened) {
    opened = true
    emit('first-open')
  }
}

// 供父组件主动展开并滚到这里（比如"补录"按钮）
function show() {
  if (!root.value) return
  root.value.open = true
  onToggle({ target: root.value })
  root.value.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function close() {
  if (root.value) root.value.open = false
}

defineExpose({ show, close })
</script>

<template>
  <details ref="root" class="panel" :open="props.open" @toggle="onToggle">
    <summary>
      {{ props.title }}
      <span v-if="props.tag" class="tag">{{ props.tag }}</span>
      <span class="arrow">▾</span>
    </summary>
    <div class="body"><slot /></div>
  </details>
</template>
