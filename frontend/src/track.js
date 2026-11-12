/**
 * 极简埋点 SDK。
 *
 * 三条规矩：
 * 1. 绝不阻塞用户。所有上报走 sendBeacon / fetch keepalive，失败就丢，不重试。
 * 2. 绝不把站点搞崩。整段逻辑包在 try 里，任何异常都吞掉。
 * 3. 只采必要信息。不发 cookie、不发指纹、不发精确地理位置；
 *    访客号是随机 UUID，存在 localStorage，用户清一下就是新访客。
 */

const BASE = import.meta.env.VITE_API_BASE || ''
const ENDPOINT = `${BASE}/api/track`

const VID_KEY = 'pt_vid'
const SID_KEY = 'pt_sid'
const SESSION_IDLE_MS = 30 * 60 * 1000 // 30 分钟无动作算新会话

function uuid() {
  try {
    if (crypto?.randomUUID) return crypto.randomUUID().replace(/-/g, '')
  } catch {
    /* 老浏览器没有 randomUUID */
  }
  return (
    Date.now().toString(36) +
    Math.random().toString(36).slice(2, 10) +
    Math.random().toString(36).slice(2, 10)
  )
}

function readLocal(key) {
  try {
    return localStorage.getItem(key) || ''
  } catch {
    return ''
  }
}

function writeLocal(key, value) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* 隐私模式下会抛，忽略 */
  }
}

// ---- 访客 / 会话 ----
let visitorId = ''
let isNewVisitor = false
let sessionId = ''
let sessionLast = 0

function initIds() {
  visitorId = readLocal(VID_KEY)
  if (!visitorId) {
    visitorId = uuid()
    writeLocal(VID_KEY, visitorId)
    isNewVisitor = true
  }
  renewSession()
}

function renewSession() {
  const now = Date.now()
  let raw = ''
  try {
    raw = sessionStorage.getItem(SID_KEY) || ''
  } catch {
    /* 隐私模式 */
  }
  if (raw) {
    try {
      const parsed = JSON.parse(raw)
      sessionId = parsed.id || ''
      sessionLast = parsed.last || 0
    } catch {
      sessionId = ''
    }
  }
  if (!sessionId || now - sessionLast > SESSION_IDLE_MS) {
    sessionId = uuid()
  }
  sessionLast = now
  try {
    sessionStorage.setItem(SID_KEY, JSON.stringify({ id: sessionId, last: now }))
  } catch {
    /* 同上 */
  }
}

export function trackHeaders() {
  try {
    renewSession()
  } catch {
    /* 忽略 */
  }
  return {
    'X-Visitor-Id': visitorId,
    'X-Session-Id': sessionId,
    'X-Visitor-New': isNewVisitor ? '1' : '0',
  }
}

// ---- 上报队列 ----
const queue = []
let timer = null

function flush(useBeacon = false) {
  if (timer) {
    clearTimeout(timer)
    timer = null
  }
  if (!queue.length) return

  const events = queue.splice(0, queue.length)
  const payload = JSON.stringify({ events })

  try {
    if (useBeacon && navigator.sendBeacon) {
      // 页面即将卸载时唯一的可靠通道：浏览器会把请求发完之后再走
      const blob = new Blob([payload], { type: 'application/json' })
      navigator.sendBeacon(ENDPOINT, blob)
      return
    }
    fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: payload,
      keepalive: true,
    }).catch(() => {})
  } catch {
    /* 上报失败不影响页面 */
  }
}

export function track(kind, data = {}, opts = {}) {
  try {
    renewSession()
    queue.push({
      kind,
      visitor: visitorId,
      session: sessionId,
      isNew: !!data.isNew,
      path: location.pathname + location.search,
      referer: document.referrer || '',
      query: data.query || '',
      platform: data.platform || '',
      target: data.target || '',
      durationMs: Number(data.durationMs) || 0,
      ok: data.ok !== false,
      resultCount: Number.isFinite(data.resultCount) ? data.resultCount : -1,
      meta: {
        screen: `${window.innerWidth}x${window.innerHeight}`,
        lang: navigator.language || '',
        ...(data.meta || {}),
      },
    })
    if (opts.beacon || queue.length >= 8) flush(!!opts.beacon)
    else if (!timer) timer = setTimeout(() => flush(false), 700)
  } catch {
    /* 埋点自身不许出问题 */
  }
}

// ---- 自动采集 ----
export function initTracking() {
  try {
    initIds()
    track('pv', { isNew: isNewVisitor, resultCount: 0 })

    // 停留时长：一次可见时段结束（页面隐藏）时上报一次 leave。
    // 累计到队列里的事件（可能还有别的 kind）一并 beacon 送出。
    let visibleSince = Date.now()
    const onHide = () => {
      const dwell = Date.now() - visibleSince
      if (dwell > 1000) {
        track('leave', { durationMs: dwell, resultCount: 0 }, { beacon: true })
      } else {
        flush(true)
      }
    }
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') {
        onHide()
      } else {
        visibleSince = Date.now()
      }
    })
    window.addEventListener('pagehide', onHide)

    // 刷新/跳转前也兜一层，避免队列里的事件丢掉
    window.addEventListener('beforeunload', () => flush(true))
  } catch {
    /* 忽略 */
  }
}
