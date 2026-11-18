<script setup>
/**
 * 内容卡片。
 *
 * 早先这里是个 <details>：九个面板摞成一列，折叠状态就是导航——刷新回顶部、
 * 找不到东西。现在导航交给左栏，卡片只负责"一块内容"，不再承担折叠职责。
 * show() / close() 保留成空实现，避免老的调用点炸掉。
 */
defineProps({
  title: { type: String, required: true },
  tag: { type: String, default: '' },
  tagBad: { type: Boolean, default: false },
  sub: { type: String, default: '' },
})

defineExpose({ show() {}, close() {} })
</script>

<template>
  <section class="card">
    <header class="card-head">
      <div class="card-title">
        {{ title }}<span v-if="tag" class="card-tag" :class="{ bad: tagBad }">{{ tag }}</span>
      </div>
      <span v-if="sub" class="card-sub">{{ sub }}</span>
      <div class="card-extra"><slot name="extra" /></div>
    </header>
    <div class="card-body"><slot /></div>
  </section>
</template>
