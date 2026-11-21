/* ==========================================================================
   主题切换 — 唯一入口
   --------------------------------------------------------------------------
   主题名写在 <html data-theme="xxx">，颜色本身在 tokens.css 里定义，这里只管
   切换、持久化、以及同步浏览器地址栏配色（meta theme-color）。

   加一套新主题要做两件事：
   1. tokens.css 里加一个 [data-theme='key'] 块；
   2. 下面 THEMES 里补一行，swatch 用于选择器预览。
   ========================================================================== */

const STORAGE_KEY = 'pantrace:theme'

export const DEFAULT_THEME = 'mint'

export const THEMES = [
  { key: 'mint', name: '青绿', desc: '默认 · 深海军蓝', accent: '#34d399', bg: '#060b12' },
  { key: 'azure', name: '冰蓝', desc: '冷调 · 深夜海面', accent: '#60a5fa', bg: '#06101c' },
  { key: 'violet', name: '夜紫', desc: '紫调 · 霓虹余晖', accent: '#a78bfa', bg: '#0a0716' },
  { key: 'amber', name: '琥珀', desc: '暖调 · 夜间护眼', accent: '#fb923c', bg: '#120c07' },
  { key: 'light', name: '晨光', desc: '浅色 · 白天阅读', accent: '#10b981', bg: '#f4f6fa' },
]

const KEYS = THEMES.map((t) => t.key)

export function readTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && KEYS.includes(saved)) return saved
  } catch {
    /* 隐私模式下 localStorage 会抛错，走默认主题即可 */
  }
  return DEFAULT_THEME
}

export function applyTheme(key, { persist = true } = {}) {
  const theme = KEYS.includes(key) ? key : DEFAULT_THEME
  const root = document.documentElement
  root.dataset.theme = theme
  // 地址栏 / 状态栏配色跟着走，否则 iOS 上会露一条不搭的颜色
  const meta = document.querySelector('meta[name="theme-color"]')
  const item = THEMES.find((t) => t.key === theme)
  if (meta && item) meta.setAttribute('content', item.bg)
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, theme)
    } catch {
      /* 存不住就算了，本次会话照样生效 */
    }
  }
  return theme
}
