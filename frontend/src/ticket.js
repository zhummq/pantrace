/**
 * 匿名票据管理。
 *
 * 用户无感：进站第一次请求时静默领一张，缓存在内存里复用，过期前自动续。
 * 票据绑的是来源 IP，不携带任何身份信息，也不写 localStorage / cookie——
 * 刷新页面重新领即可，没必要持久化。
 *
 * 三条规矩（跟埋点 SDK 一致）：
 * 1. 领票失败不阻塞业务：返回空票据，让请求照发，由服务端决定是否放行。
 * 2. 并发去重：同时发多个请求只领一次票。
 * 3. 绝不把站点搞崩：所有异常吞掉。
 */

const BASE = import.meta.env.VITE_API_BASE || ''
const ENDPOINT = `${BASE}/api/ticket`
const REFRESH_AHEAD_MS = 30 * 1000 // 提前 30 秒续，避免边界上踩过期

let cached = ''
let expiresAt = 0
let inflight = null

async function fetchTicket() {
  const res = await fetch(ENDPOINT)
  let data = {}
  try {
    data = await res.json()
  } catch {
    return { ticket: '', ttl: 0 }
  }
  if (!res.ok || data.code !== 0 || !data.ticket) return { ticket: '', ttl: 0 }
  return { ticket: data.ticket, ttl: Number(data.expiresIn) || 300 }
}

/**
 * 拿当前有效票据。force=true 用于服务端判定票据失效后的强制重领。
 * 永远返回字符串（失败为 ''），不抛异常。
 */
export async function ensureTicket(force = false) {
  if (force) {
    cached = ''
    expiresAt = 0
  } else if (cached && Date.now() < expiresAt - REFRESH_AHEAD_MS) {
    return cached
  }
  if (inflight) return inflight

  inflight = (async () => {
    try {
      const { ticket, ttl } = await fetchTicket()
      cached = ticket
      // 领票失败时 5 秒后再试，免得接口抖动时每次请求都打一次领票接口。
      const life = ticket ? Math.max(1, ttl) * 1000 : 5000
      expiresAt = Date.now() + life
      return ticket
    } catch {
      expiresAt = Date.now() + 5000
      return ''
    } finally {
      inflight = null
    }
  })()
  return inflight
}

/** 服务端明确说票据无效时调用，丢弃当前缓存，下次请求重领。 */
export function invalidateTicket() {
  cached = ''
  expiresAt = 0
}
