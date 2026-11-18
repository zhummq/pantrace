<script setup>
import { computed, ref } from 'vue'
import Panel from './Panel.vue'
import { platformName, shortTime } from '../format'

/**
 * 搜不到的词。
 *
 * 后端零结果口径有两个细节，界面上必须保留：
 * - 只算首屏（page=1）：翻页越界造成的空页是分页边界，不是缺资源
 * - 同时给 n 和 total：10 次里空 1 次和 10 次全空，补资源的优先级不是一回事
 */
const props = defineProps({
  data: { type: Object, default: () => ({}) },
  limit: { type: Number, default: 0 },
})

const emit = defineEmits(['more'])

const all = computed(() => props.data.zero_keywords || [])
const rows = computed(() => (props.limit > 0 ? all.value.slice(0, props.limit) : all.value))
const totalZero = computed(() => all.value.reduce((s, r) => s + (Number(r.n) || 0), 0))
const max = computed(() => all.value.reduce((m, r) => Math.max(m, Number(r.n) || 0), 1))

const copied = ref(false)

function barW(r) {
  return Math.max(3, Math.round(((Number(r.n) || 0) / max.value) * 100)) + '%'
}

function searchUrl(q) {
  return '/?q=' + encodeURIComponent(q)
}

// platform 是 GROUP_CONCAT 出来的逗号串，会重复，先拆再去重
function plat(v) {
  const keys = String(v || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  const uniq = [...new Set(keys)]
  return uniq.length ? platformName(uniq.join(',')) : '全部平台'
}

async function copyAll() {
  const text = all.value.map((r) => r.q).join('\n')
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    setTimeout(() => (copied.value = false), 1600)
  } catch {
    /* 剪贴板不可用就当没点，不弹错 */
  }
}
</script>

<template>
  <Panel
    title="搜不到的词"
    :tag="all.length ? `${all.length} 个词 · 落空 ${totalZero} 次` : '没有零结果'"
  >
    <template v-if="limit > 0" #extra>
      <a v-if="all.length" @click="emit('more')">查看全部 {{ all.length }} 个</a>
    </template>

    <p v-if="limit === 0 && rows.length" class="zk-hint">
      首屏搜索、上游返回 0 条。点词去站内重搜验证；「零/总」是落空次数与总搜索次数。
    </p>

    <div v-if="!rows.length" class="empty">这个区间没有零结果——上游都给得出东西</div>

    <template v-else>
      <div class="zk-wrap" :class="{ slim: limit > 0 }">
        <div class="zk-head">
          <span>关键词</span>
          <span></span>
          <span class="zk-n">零/总</span>
          <span v-if="limit === 0">平台</span>
          <span class="zk-t">最近</span>
        </div>
        <div class="zk-list" :style="limit > 0 ? 'max-height:none' : ''">
          <div v-for="r in rows" :key="r.q" class="zk-row">
            <a
              class="zk-q"
              :href="searchUrl(r.q)"
              target="_blank"
              rel="noopener"
              :title="`去站内搜一次「${r.q}」`"
            >{{ r.q }}</a>
            <span class="zk-bar"><i :style="{ width: barW(r) }"></i></span>
            <span class="zk-n"><b>{{ r.n }}</b><em>/{{ r.total || r.n }}</em></span>
            <span v-if="limit === 0" class="zk-plat" :title="plat(r.platforms)">{{ plat(r.platforms) }}</span>
            <span class="zk-t">{{ r.last_ts ? shortTime(r.last_ts) : '-' }}</span>
          </div>
        </div>
      </div>

      <div v-if="limit === 0" class="zk-foot">
        <button class="ghost zk-copy" @click="copyAll">
          {{ copied ? '已复制' : '复制全部关键词' }}
        </button>
        <span class="zk-note">拿去补资源、建同义词，或者判断是不是词本身写错了</span>
      </div>
    </template>
  </Panel>
</template>

<style scoped>
.zk-hint {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.65;
  color: var(--muted);
}

.zk-head,
.zk-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 76px 86px 110px 62px;
  align-items: center;
  gap: 10px;
}
.zk-wrap.slim .zk-head,
.zk-wrap.slim .zk-row {
  grid-template-columns: minmax(0, 1fr) 64px 82px 62px;
}

.zk-head {
  font-size: 11px;
  color: var(--faint);
  padding: 0 0 6px;
  border-bottom: 1px solid var(--line);
}

.zk-list {
  max-height: 420px;
  overflow-y: auto;
  padding-right: 2px;
}

.zk-row {
  padding: 7px 0;
  border-bottom: 1px solid var(--line);
  font-size: 12.5px;
}

.zk-q {
  color: var(--ink);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-decoration: none;
  border-bottom: 1px dashed transparent;
}
.zk-q:hover {
  color: var(--brand);
  border-bottom-color: var(--brand);
}

.zk-bar {
  height: 6px;
  background: var(--surface-3);
  border-radius: 3px;
  overflow: hidden;
}
.zk-bar i {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--warn);
}

.zk-n {
  text-align: right;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.zk-n b { color: var(--ink); font-weight: 600; }
.zk-n em { font-style: normal; color: var(--faint); }

.zk-plat {
  color: var(--ink-2);
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.zk-t {
  text-align: right;
  color: var(--faint);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.zk-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 12px;
}

.zk-copy { height: 28px; padding: 0 11px; font-size: 12px; }

.zk-note { color: var(--faint); font-size: 11.5px; }

@media (max-width: 560px) {
  .zk-head, .zk-row { grid-template-columns: minmax(0, 1fr) 56px 56px 62px; }
  .zk-plat { display: none; }
}
</style>
