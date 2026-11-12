<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { submitFeedback } from '../api.js'

const props = defineProps({
  open: { type: Boolean, default: false },
  // result = 打开的结果有问题；request = 求资源；bug = 页面报错
  mode: { type: String, default: 'request' },
  item: { type: Object, default: null },
  keyword: { type: String, default: '' },
  errorText: { type: String, default: '' },
})

const emit = defineEmits(['close', 'submitted'])

const RESULT_KINDS = [
  { key: 'dead_link', label: '链接打不开', hint: '已失效、被删除、跳到别处' },
  { key: 'pwd_wrong', label: '提取码不对', hint: '码错了，或者已经被改' },
  { key: 'wrong_content', label: '不是这个内容', hint: '标题党、内容对不上' },
]

const kind = ref('dead_link')
const query = ref('')
const message = ref('')
const contact = ref('')
const sending = ref(false)
const done = ref(false)
const err = ref('')

const title = computed(() => {
  if (props.mode === 'result') return '这条结果有问题'
  if (props.mode === 'bug') return '上报这个问题'
  return '你要找什么'
})

const subtitle = computed(() => {
  if (props.mode === 'result') return '选一个问题类型就行，不用打字。'
  if (props.mode === 'bug') {
    return props.errorText
      ? '错误信息会自动带上，补充一句你在做什么就够了。'
      : '说清楚你遇到的问题，越具体越好。'
  }
  return '没搜到就写在这里——比反复换关键词有用。'
})

const submitLabel = computed(() => (props.mode === 'request' ? '提交需求' : '提交反馈'))

const doneTitle = computed(() => (props.mode === 'request' ? '收到了' : '记下了'))

const doneDesc = computed(() => {
  if (props.mode === 'result') return '同一个链接被多人反馈失效后，会自动从结果里隐藏。'
  if (props.mode === 'bug') return '错误信息已经一起提交，定位会快一些。'
  return '需求会进后台清单，找到合适的源就按你留的方式联系。'
})

const msgPlaceholder = computed(() => {
  if (props.mode === 'result') return '补充一句（选填）：比如打开后提示文件已删除'
  if (props.mode === 'bug') return '当时在做什么？（选填）'
  return '年份、清晰度、语言、主演…（选填）'
})

function reset() {
  kind.value = 'dead_link'
  query.value = ''
  message.value = ''
  contact.value = ''
  sending.value = false
  done.value = false
  err.value = ''
  if (props.mode === 'request') query.value = props.keyword || ''
}

function close() {
  if (sending.value) return
  emit('close')
}

function onKeydown(e) {
  if (e.key === 'Escape') close()
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      reset()
      document.body.style.overflow = 'hidden'
      window.addEventListener('keydown', onKeydown)
    } else {
      document.body.style.overflow = ''
      window.removeEventListener('keydown', onKeydown)
    }
  }
)

onBeforeUnmount(() => {
  document.body.style.overflow = ''
  window.removeEventListener('keydown', onKeydown)
})

async function submit() {
  err.value = ''
  if (props.mode === 'request' && !query.value.trim()) {
    err.value = '写一下你要找的资源名'
    return
  }
  if (props.mode === 'bug' && !message.value.trim()) {
    err.value = '写一句具体情况吧，不然没法查'
    return
  }

  const base = {
    message: message.value.trim(),
    contact: contact.value.trim(),
    page: location.pathname + location.search,
  }

  let payload
  if (props.mode === 'result') {
    payload = {
      ...base,
      kind: kind.value,
      query: props.keyword || '',
      platform: props.item?.platform || '',
      target: props.item?.title || '',
      url: props.item?.shareUrl || '',
    }
  } else if (props.mode === 'request') {
    payload = { ...base, kind: 'missing', query: query.value.trim() }
  } else {
    // 错误原文拼在说明前面：看板上第一眼要能看到是签名过期还是超时
    const raw = (props.errorText || '').slice(0, 200)
    payload = {
      ...base,
      kind: 'bug',
      query: props.keyword || '',
      message: `[${raw}] ${message.value.trim()}`.trim(),
    }
  }

  sending.value = true
  try {
    await submitFeedback(payload)
    done.value = true
    emit('submitted', { kind: payload.kind })
  } catch (e) {
    err.value = e.message || '提交失败，稍后再试'
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="fb">
      <div v-if="open" class="mask" @click.self="close">
        <div class="sheet" role="dialog" aria-modal="true" :aria-label="title">
          <button v-show="!done" class="x" aria-label="关闭" @click="close">×</button>

          <template v-if="!done">
            <h2>{{ title }}</h2>
            <p class="sub">{{ subtitle }}</p>

            <div v-if="mode === 'result' && item" class="target" :title="item.title">
              {{ item.title || '（无标题）' }}
            </div>

            <div v-if="mode === 'result'" class="opts">
              <button
                v-for="o in RESULT_KINDS"
                :key="o.key"
                class="opt"
                :class="{ on: kind === o.key }"
                @click="kind = o.key"
              >
                <b>{{ o.label }}</b>
                <i>{{ o.hint }}</i>
              </button>
            </div>

            <div v-if="mode === 'request'" class="field">
              <label for="fb-query">资源名</label>
              <input
                id="fb-query"
                v-model="query"
                maxlength="100"
                placeholder="比如：繁花 4K 国语"
                @keydown.enter.prevent="submit"
              />
            </div>

            <div v-if="mode === 'bug' && errorText" class="errbox">
              {{ errorText }}
            </div>

            <div class="field">
              <label for="fb-msg">{{ mode === 'request' ? '补充说明（选填）' : '说明（选填）' }}</label>
              <textarea id="fb-msg" v-model="message" maxlength="500" rows="3" :placeholder="msgPlaceholder"></textarea>
            </div>

            <div v-if="mode === 'request'" class="field">
              <label for="fb-contact">联系方式（选填）</label>
              <input id="fb-contact" v-model="contact" maxlength="120" placeholder="邮箱或微信，找到后通知你" />
            </div>

            <p v-if="err" class="err">{{ err }}</p>

            <button class="submit" :disabled="sending" @click="submit">
              {{ sending ? '提交中…' : submitLabel }}
            </button>
            <p class="tip">只用于改进搜索，不会公开展示。</p>
          </template>

          <div v-else class="ok">
            <div class="okicon">✓</div>
            <h2>{{ doneTitle }}</h2>
            <p class="sub">{{ doneDesc }}</p>
            <button class="submit" @click="close">知道了</button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(15, 23, 42, 0.42);
  backdrop-filter: blur(3px);
}

.sheet {
  position: relative;
  width: 100%;
  max-width: 460px;
  max-height: 88vh;
  overflow-y: auto;
  padding: 26px 26px 22px;
  background: var(--surface);
  border-radius: 16px;
  box-shadow: 0 24px 70px -24px rgba(15, 23, 42, 0.4);
}

.x {
  position: absolute;
  top: 14px;
  right: 14px;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  font-size: 19px;
  line-height: 1;
}

.x:hover {
  background: #f1f5f9;
  color: var(--ink);
}

h2 {
  margin: 0 0 6px;
  font-size: 18px;
  font-weight: 650;
  letter-spacing: 0.2px;
  color: var(--ink);
}

.sub {
  margin: 0 0 18px;
  font-size: 13px;
  line-height: 1.55;
  color: var(--muted);
}

.target {
  margin: -6px 0 14px;
  padding: 9px 11px;
  border-radius: 9px;
  background: #f6f8fa;
  border: 1px solid var(--line);
  font-size: 12.5px;
  color: var(--ink-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.opts {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 16px;
}

.opt {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  padding: 10px 13px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: #fff;
  text-align: left;
  transition: all 0.15s;
}

.opt b {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--ink);
}

.opt i {
  font-style: normal;
  font-size: 11.5px;
  color: var(--muted);
}

.opt:hover {
  border-color: #b7ddd5;
}

.opt.on {
  border-color: var(--brand);
  background: var(--brand-soft);
}

.opt.on b {
  color: var(--brand-dark);
}

.field {
  margin-bottom: 12px;
}

.field label {
  display: block;
  margin-bottom: 5px;
  font-size: 12px;
  color: var(--muted);
}

.field input,
.field textarea {
  width: 100%;
  padding: 9px 11px;
  border: 1px solid var(--line-strong);
  border-radius: 9px;
  background: #fff;
  font-size: 13.5px;
  color: var(--ink);
  outline: none;
  resize: vertical;
  transition: border-color 0.15s;
}

.field input:focus,
.field textarea:focus {
  border-color: #8fc9bd;
  box-shadow: 0 0 0 3px rgba(11, 122, 107, 0.08);
}

.field input::placeholder,
.field textarea::placeholder {
  color: #9aa8bb;
}

.errbox {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 9px;
  background: #fdf2f1;
  border: 1px solid #f5d3d0;
  color: var(--danger);
  font-size: 12.5px;
  line-height: 1.5;
  word-break: break-word;
  max-height: 96px;
  overflow-y: auto;
}

.err {
  margin: 0 0 10px;
  font-size: 12.5px;
  color: var(--danger);
}

.submit {
  width: 100%;
  height: 40px;
  border: none;
  border-radius: 10px;
  background: var(--ink);
  color: #fff;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: 0.3px;
  transition: background 0.16s;
}

.submit:hover:not(:disabled) {
  background: #1e293b;
}

.submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.tip {
  margin: 10px 0 0;
  font-size: 11.5px;
  color: var(--muted);
  text-align: center;
}

.ok {
  padding: 8px 0 2px;
  text-align: center;
}

.okicon {
  display: grid;
  place-items: center;
  width: 46px;
  height: 46px;
  margin: 0 auto 14px;
  border-radius: 50%;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 22px;
  font-weight: 600;
}

.ok .sub {
  margin-bottom: 20px;
}

.fb-enter-active,
.fb-leave-active {
  transition: opacity 0.18s ease;
}

.fb-enter-active .sheet,
.fb-leave-active .sheet {
  transition: transform 0.18s ease, opacity 0.18s ease;
}

.fb-enter-from,
.fb-leave-to {
  opacity: 0;
}

.fb-enter-from .sheet,
.fb-leave-to .sheet {
  transform: translateY(10px) scale(0.985);
  opacity: 0;
}

@media (max-width: 560px) {
  .mask {
    align-items: flex-end;
    padding: 0;
  }

  .sheet {
    max-width: none;
    max-height: 92vh;
    padding: 22px 18px 18px;
    border-radius: 18px 18px 0 0;
  }
}
</style>
