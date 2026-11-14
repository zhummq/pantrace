<script setup>
import { reactive, ref } from 'vue'
import Panel from './Panel.vue'
import * as api from '../api'
import { GROUP_TYPES } from '../format'

const panel = ref(null)
const loaded = ref(false)
const message = ref('')
const messageErr = ref(false)
const saving = ref(false)
const uploading = ref(-1)

const cfg = reactive({
  enabled: true,
  title: '',
  desc: '',
  tip: '',
  enter: '1',
  acquire: '1',
  once: 'session',
  delay: 800,
  groups: [],
})

function flash(text, isErr) {
  message.value = text
  messageErr.value = !!isErr
  if (text) setTimeout(() => { if (message.value === text) message.value = '' }, 4000)
}

function apply(d) {
  cfg.enabled = d.enabled !== false
  cfg.title = d.title || ''
  cfg.desc = d.desc || ''
  cfg.tip = d.tip || ''
  const p = d.popup || {}
  cfg.enter = p.enter === false ? '0' : '1'
  cfg.acquire = p.acquire === false ? '0' : '1'
  cfg.once = p.once || 'session'
  cfg.delay = Number.isFinite(Number(p.delay)) ? Number(p.delay) : 800
  cfg.groups = (d.groups || []).map((g) => ({
    type: g.type || 'qq',
    name: g.name || '',
    number: g.number || '',
    qr: g.qr || '',
    link: g.link || '',
    note: g.note || '',
    enabled: g.enabled !== false,
  }))
}

async function reload() {
  try {
    apply(await api.groupConfig())
    loaded.value = true
    message.value = ''
  } catch (e) {
    if (e.status !== 401) flash('读取失败：' + e.message, true)
  }
}

function onFirstOpen() {
  if (!loaded.value) reload()
}

function addGroup() {
  if (cfg.groups.length >= 4) return flash('最多 4 个群', true)
  cfg.groups.push({ type: 'qq', name: '', number: '', qr: '', link: '', note: '', enabled: true })
}

function removeGroup(i) {
  cfg.groups.splice(i, 1)
}

function pickFile(e) {
  const input = e.currentTarget.parentElement.querySelector('input[type="file"]')
  if (input) input.click()
}

async function upload(i, event) {
  const file = event.target.files && event.target.files[0]
  if (!file) return
  uploading.value = i
  flash('上传中…')
  try {
    const d = await api.uploadGroupQr(file)
    cfg.groups[i].qr = d.url
    flash('上传成功，记得点保存')
  } catch (e) {
    flash('上传失败：' + e.message, true)
  } finally {
    uploading.value = -1
    event.target.value = ''
  }
}

async function save() {
  saving.value = true
  flash('保存中…')
  const delay = Number(cfg.delay)
  try {
    const d = await api.saveGroupConfig({
      enabled: cfg.enabled,
      title: cfg.title.trim(),
      desc: cfg.desc.trim(),
      tip: cfg.tip.trim(),
      popup: {
        enter: cfg.enter === '1',
        acquire: cfg.acquire === '1',
        once: cfg.once,
        delay: Number.isFinite(delay) ? delay : 800,
      },
      groups: cfg.groups.map((g) => ({
        type: g.type,
        name: (g.name || '').trim(),
        number: (g.number || '').trim(),
        qr: (g.qr || '').trim(),
        link: (g.link || '').trim(),
        note: (g.note || '').trim(),
        enabled: !!g.enabled,
      })),
    })
    apply(d || {})
    flash('已保存，前端下次进站生效')
  } catch (e) {
    flash('保存失败：' + e.message, true)
  } finally {
    saving.value = false
  }
}

defineExpose({ reload })
</script>

<template>
  <Panel ref="panel" title="群入口配置" tag="改二维码 / 群号 / 弹窗" @first-open="onFirstOpen">
    <label class="grp-row">
      <input v-model="cfg.enabled" type="checkbox" /> 启用群入口（进站弹窗 + 获取链接拦截 + 页脚入口）
    </label>

    <div class="grp-grid">
      <label>弹窗标题<input v-model="cfg.title" maxlength="40" /></label>
      <label>一句说明（弹窗里的小字）<input v-model="cfg.desc" maxlength="120" /></label>
      <label>扫码提示语<input v-model="cfg.tip" maxlength="80" /></label>
      <div class="grp-grid3" style="margin-bottom: 0">
        <label>进站弹窗
          <select v-model="cfg.enter"><option value="1">开</option><option value="0">关</option></select>
        </label>
        <label>获取链接时拦截
          <select v-model="cfg.acquire"><option value="1">开</option><option value="0">关</option></select>
        </label>
        <label>进站弹窗频率
          <select v-model="cfg.once">
            <option value="session">每次打开页面一次</option>
            <option value="day">每天一次</option>
            <option value="always">每次都弹</option>
          </select>
        </label>
      </div>
    </div>
    <div class="grp-grid one">
      <label>进站延迟（毫秒，进站后多久弹）
        <input v-model.number="cfg.delay" type="number" min="0" max="10000" step="100" />
      </label>
    </div>

    <h2 style="margin: 14px 0 10px; font-size: 13.5px; font-weight: 600">
      群列表 <span style="font-size: 11.5px; color: var(--faint); font-weight: 400; margin-left: 6px">最多 4 个，可放 QQ 群 / 微信群</span>
    </h2>

    <div v-for="(g, i) in cfg.groups" :key="i" class="grp-item">
      <div class="grp-grid3">
        <label>类型
          <select v-model="g.type">
            <option v-for="t in GROUP_TYPES" :key="t[0]" :value="t[0]">{{ t[1] }}</option>
          </select>
        </label>
        <label>名称<input v-model="g.name" maxlength="40" /></label>
        <label>群号 / 微信号<input v-model="g.number" maxlength="40" /></label>
      </div>
      <div class="grp-qr-row">
        <img v-show="g.qr" class="grp-qr-prev" :src="g.qr" alt="群二维码" />
        <div class="grp-qr-fields">
          <div class="grp-line">
            <label>二维码图片地址
              <input v-model="g.qr" placeholder="/uploads/qr-xxx.png 或 https://…" />
            </label>
            <div style="flex: 0 0 auto; display: flex; align-items: flex-end">
              <input type="file" accept="image/*" hidden @change="upload(i, $event)" />
              <button type="button" class="grp-btn" :disabled="uploading === i" @click="pickFile">上传图片</button>
            </div>
          </div>
          <div class="grp-line">
            <label>手机端一键加群链接（选填）
              <input v-model="g.link" placeholder="https://qm.qq.com/…" />
            </label>
            <label>备注（替换扫码提示语）
              <input v-model="g.note" maxlength="60" />
            </label>
          </div>
          <div class="grp-line">
            <label style="flex-direction: row; align-items: center; gap: 6px">
              <input v-model="g.enabled" type="checkbox" /> 启用这个群
            </label>
            <button type="button" class="grp-btn danger" style="margin-left: auto" @click="removeGroup(i)">删除这个群</button>
          </div>
        </div>
      </div>
    </div>

    <button class="grp-btn grp-add" @click="addGroup">＋ 加一个群</button>

    <div class="grp-actions">
      <button class="grp-save" :disabled="saving" @click="save">保存配置</button>
      <span class="grp-msg" :class="{ err: messageErr }">{{ message }}</span>
    </div>
  </Panel>
</template>
