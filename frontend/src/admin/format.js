export const PLATFORM_NAMES = {
  quark: '夸克网盘',
  baidu: '百度网盘',
  ali: '阿里云盘',
  uc: 'UC网盘',
  xunlei: '迅雷网盘',
  115: '115网盘',
  123: '123云盘',
  tianyi: '天翼云盘',
  yidong: '移动云盘',
  wo: '联通云盘',
  guangya: '光鸭云盘',
  lanzou: '蓝奏云',
  weiyun: '腾讯微云',
  ctfile: '城通网盘',
}

export const KIND_NAMES = {
  pv: '打开',
  leave: '离开',
  search: '搜索',
  click: '点开',
  copy_pwd: '复制码',
  filter: '换平台',
  page: '翻页',
  retry: '重试',
  group: '进群',
}

export const GROUP_TYPES = [
  ['qq', 'QQ 群'],
  ['wechat', '微信群'],
  ['other', '其他'],
]

export function platformName(key) {
  if (!key) return '-'
  return String(key)
    .split(',')
    .map((k) => PLATFORM_NAMES[k] || k)
    .join(' / ')
}

export function pct(v) {
  return ((Number(v) || 0) * 100).toFixed(1) + '%'
}

export function ms(v) {
  v = Number(v) || 0
  if (v <= 0) return '-'
  return v >= 1000 ? (v / 1000).toFixed(1) + ' s' : v + ' ms'
}

function pad(x) {
  return (x < 10 ? '0' : '') + x
}

export function clock(ts) {
  const d = new Date(ts * 1000)
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds())
}

export function shortTime(ts) {
  const d = new Date(ts * 1000)
  return d.getMonth() + 1 + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes())
}

export function sizeText(bytes) {
  const gb = (Number(bytes) || 0) / 1073741824
  if (gb <= 0) return ''
  return gb >= 1 ? gb.toFixed(1) + ' GB' : (gb * 1024).toFixed(0) + ' MB'
}
