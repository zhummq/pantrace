<script setup>
import { onMounted, reactive, ref } from 'vue'
import Panel from './Panel.vue'
import * as api from '../api'

/**
 * 转存引流水印配置。
 *
 * 全局一个文件夹名，不分平台：转存时在落点目录建一个空文件夹，它随资源
 * 一起进分享、一起在 30 分钟后被回收。名字存库覆盖 .env，保存即热更。
 * 留空是有效值——关闭水印，不产生任何额外请求。
 */

const DEFAULT_NAME = '资源搜索网址panseek.xyz'

const loaded = ref(false)
const saving = ref(false)
const message = ref('')
const messageErr = ref(false)
const cfg = reactive({ name: '' })

function flash(text, isErr) {
  message.value = text
  messageErr.value = !!isErr
  if (text) setTimeout(() => { if (message.value === text) message.value = '' }, 4000)
}

function apply(d) {
  cfg.name = d.name || ''
}

async function reload() {
  try {
    apply(await api.watermark())
    loaded.value = true
  } catch (e) {
    if (e.status !== 401) flash('读取失败：' + e.message, true)
  }
}

async function save() {
  saving.value = true
  try {
    apply(await api.saveWatermark(cfg.name.trim()))
    flash('已保存，下一次转存立即生效（不用重启）')
  } catch (e) {
    flash('保存失败：' + e.message, true)
  } finally {
    saving.value = false
  }
}

onMounted(reload)
</script>

<template>
  <Panel title="转存水印" tag="引流文件夹" sub="改完即生效，不用重启">
    <p class="ad-lead">
      每次转存时，在落点目录里建一个<strong>空文件夹</strong>，名字就是这里填的内容。
      它会随资源一起进分享链接——用户打开就能看到；30 分钟回收时，跟资源一起删掉。
    </p>

    <label class="ad-block">
      文件夹名（留空 = 关闭水印，不产生任何额外请求）
      <input
        v-model="cfg.name"
        class="wm-name"
        type="text"
        maxlength="60"
        placeholder="资源搜索网址panseek.xyz"
        @keyup.enter="save"
      />
    </label>

    <div class="wm-preview">
      <span>分享里会显示：</span>
      <span class="ad-chip">{{ cfg.name.trim() ? cfg.name.trim() + '/' : '（关闭水印）' }}</span>
    </div>

    <p class="ad-hint">
      只有<strong>夸克 / UC / 迅雷 / 百度</strong>会建这个文件夹，其余网盘自动跳过、转存照常。
      名字不能含 <code>/</code> 或 <code>\</code>，最多 60 字。保存后写入数据库，重启仍然生效。
    </p>

    <div class="grp-actions">
      <button class="grp-save" :disabled="saving || !loaded" @click="save">保存配置</button>
      <button class="grp-btn wm-preset" :disabled="saving || !loaded" @click="cfg.name = DEFAULT_NAME">
        填入默认名
      </button>
      <span class="grp-msg" :class="{ err: messageErr }">{{ message }}</span>
    </div>
  </Panel>
</template>
