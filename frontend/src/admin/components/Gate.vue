<script setup>
import { ref } from 'vue'

defineProps({
  message: { type: String, default: '' },
})

const emit = defineEmits(['submit'])
const value = ref('')

function submit() {
  const v = value.value.trim()
  if (!v) return
  emit('submit', v)
  value.value = ''
}
</script>

<template>
  <div class="gate">
    <div class="box">
      <h3>需要口令</h3>
      <p>看板数据受保护，输入 ADMIN_TOKEN 后可见。</p>
      <div v-if="message" class="err">{{ message }}</div>
      <input
        v-model="value"
        type="password"
        placeholder="管理员口令"
        autocomplete="current-password"
        @keydown.enter="submit"
      />
      <button @click="submit">进入看板</button>
    </div>
  </div>
</template>
