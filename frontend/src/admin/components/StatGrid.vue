<script setup>
defineProps({
  items: { type: Array, default: () => [] },
  // big = 首屏大数字，small = 次级小数字
  size: { type: String, default: 'small' },
})
</script>

<template>
  <section :class="size === 'big' ? 'overview' : 'substats'">
    <div v-for="it in items" :key="it.k" class="stat" :class="{ hi: it.hi }">
      <div class="k">{{ it.k }}</div>
      <div class="v">{{ it.v }}</div>
      <div v-if="it.hint" class="hint">{{ it.hint }}</div>
    </div>
  </section>
</template>
