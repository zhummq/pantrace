<script setup>
import { onMounted, reactive, ref } from 'vue'
import Panel from './Panel.vue'
import * as api from '../api'

/**
 * 广告过滤配置。
 *
 * 两个开关对应两级处理，别合并——它们的代价完全不同：
 *   一级（preskip）在转存**之前**从待转清单里摘掉，零请求零等待；
 *   二级（filter）在转存**之后**由后台线程往目录里翻两层删文件。
 *
 * 下半部分是"试一个文件名"：走的是后端线上同一套判断函数，不是前端复刻的
 * 规则。运营改完词先在这儿试，别等线上删了正片才发现配歪了。
 */

const loaded = ref(false)
const saving = ref(false)
const message = ref('')
const messageErr = ref(false)

const cfg = reactive({
  preskip: true,
  filter: false,
  keywords: '',
  depth: 2,
  max_mb: 2,
})

const suffixes = ref([])

// 探针
const probe = reactive({ name: '', size: 2048 })
const probeResult = ref(null)
const probing = ref(false)

function flash(text, isErr) {
  message.value = text
  messageErr.value = !!isErr
  if (text) setTimeout(() => { if (message.value === text) message.value = '' }, 4000)
}

function apply(d) {
  cfg.preskip = d.preskip !== false
  cfg.filter = d.filter === true
  cfg.keywords = d.keywords || ''
  cfg.depth = Number(d.depth) || 2
  cfg.max_mb = Number(d.max_mb ?? 2)
  if (Array.isArray(d.suffixes)) suffixes.value = d.suffixes
}

// 词表实时预览：后端会做去空/去重/小写，这里只负责让人看见"我到底填了几个"
function words() {
  const seen = new Set()
  const out = []
  for (const raw of String(cfg.keywords || '').replace(/，/g, ',').split(/[,\n]/)) {
    const w = raw.trim()
    if (!w || seen.has(w.toLowerCase())) continue
    seen.add(w.toLowerCase())
    out.push(w)
  }
  return out
}

async function reload() {
  try {
    apply(await api.adFilter())
    loaded.value = true
  } catch (e) {
    if (e.status !== 401) flash('读取失败：' + e.message, true)
  }
}

async function save() {
  saving.value = true
  try {
    apply(await api.saveAdFilter({
      preskip: cfg.preskip,
      filter: cfg.filter,
      keywords: cfg.keywords,
      depth: cfg.depth,
      max_mb: cfg.max_mb,
    }))
    flash('已保存，下一次转存立即生效（不用重启）')
    probeResult.value = null
  } catch (e) {
    flash('保存失败：' + e.message, true)
  } finally {
    saving.value = false
  }
}

async function runProbe() {
  if (!probe.name.trim()) return
  probing.value = true
  try {
    probeResult.value = await api.probeAd(probe.name.trim(), Number(probe.size) || 0)
  } catch (e) {
    probeResult.value = null
    flash('试算失败：' + e.message, true)
  } finally {
    probing.value = false
  }
}

function sizeText(bytes) {
  const n = Number(bytes) || 0
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + ' GB'
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + ' MB'
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB'
  return n + ' B'
}

onMounted(reload)
</script>

<template>
  <Panel title="广告过滤" tag="转存前拦截 / 转存后清理" sub="改完即生效，不用重启">
    <p class="ad-lead">
      判定是<strong>三条全中</strong>，缺一条就放过——宁可漏删，不可误杀。误杀的代价是用户拿到
      残缺资源，而他不会回来投诉，只会觉得资源坏了。
    </p>

    <div class="ad-row">
      <label>转存前拦截
        <select v-model="cfg.preskip">
          <option :value="true">开</option>
          <option :value="false">关</option>
        </select>
      </label>
      <label>转存后清理
        <select v-model="cfg.filter">
          <option :value="false">关</option>
          <option :value="true">开</option>
        </select>
      </label>
      <label>附件体积上限（MB）
        <input v-model.number="cfg.max_mb" type="number" min="0" max="10240" step="0.5" />
      </label>
    </div>

    <p class="ad-hint">
      一级在 <code>share_info</code> 之后、<code>transfer</code> 之前把顶层引流附件从待转清单里摘掉：
      零请求、零等待，文件根本不进网盘。二级是转存之后用后台线程往目录里翻
      <strong>{{ cfg.depth }}</strong> 层补一刀，只删文件不删目录。二级默认关，先看日志再开。
    </p>

    <label class="ad-block">
      拦截词（逗号或换行分隔，不区分大小写，只匹配文件名不匹配路径）
      <textarea
        v-model="cfg.keywords"
        class="ad-words"
        rows="6"
        placeholder="微信,公众号,加群,进群,福利,防失联,更多资源,QQ群"
      />
    </label>

    <div class="ad-chips">
      <span class="ad-chip-count">{{ words().length }} 个词</span>
      <span v-for="w in words()" :key="w" class="ad-chip">{{ w }}</span>
    </div>

    <p class="ad-hint">
      不建议放「广告」「推广」「资源」「合集」这类宽词——「广告设计素材.jpg」会被它们误伤。
      真正的引流附件几乎都带「公众号 / 加群」。
    </p>

    <div class="ad-row">
      <label>二级清理深度（1–5）
        <input v-model.number="cfg.depth" type="number" min="1" max="5" step="1" />
      </label>
      <label class="ad-suffix-box">
        会判定的后缀（其余一律不动）
        <span class="ad-suffix-list">{{ suffixes.join('  ') }}</span>
        <span class="ad-suffix-tip">
          文本类命中词就判；图片类还要求文件名主干<strong>正好等于</strong>某个拦截词——
          「微信图片_20240101.jpg」这类默认名不会被误伤。
        </span>
      </label>
    </div>

    <h2 class="ad-h2">试一个文件名</h2>
    <p class="ad-hint">
      跟线上走同一个判断函数。填个真实文件名和体积，看它会不会被拦、卡在哪一步。
    </p>
    <div class="ad-probe">
      <input v-model="probe.name" placeholder="加群领福利.txt" @keyup.enter="runProbe" />
      <input v-model.number="probe.size" type="number" min="0" placeholder="字节" />
      <button class="grp-btn" :disabled="probing || !probe.name.trim()" @click="runProbe">
        {{ probing ? '试算中…' : '试算' }}
      </button>
    </div>
    <div v-if="probeResult" class="ad-result" :class="{ hit: probeResult.hit }">
      <span class="ad-verdict">{{ probeResult.hit ? '会被拦下' : '不会被拦' }}</span>
      <span class="ad-reason">{{ probeResult.reason }}</span>
      <span class="ad-meta">
        后缀 {{ probeResult.suffix || '无' }} · 附件型 {{ probeResult.attachment ? '是' : '否' }} ·
        命中词 {{ probeResult.keyword_hit ? '是' : '否' }} · 体积 {{ sizeText(probe.size) }}
      </span>
    </div>

    <div class="grp-actions">
      <button class="grp-save" :disabled="saving || !loaded" @click="save">保存配置</button>
      <span class="grp-msg" :class="{ err: messageErr }">{{ message }}</span>
    </div>
  </Panel>
</template>
