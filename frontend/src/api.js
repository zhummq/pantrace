import { trackHeaders } from './track.js'
import { ensureTicket, invalidateTicket } from './ticket.js'

const BASE = import.meta.env.VITE_API_BASE || ''

async function rawRequest(path, params, extraHeaders) {
  const qs = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.append(k, v)
  })
  const url = `${BASE}${path}${qs.toString() ? '?' + qs : ''}`
  // 带上匿名访客号：服务端拿它把「搜索」和「点击」串成一条行为链。
  // 匿名票据绑 IP，无感附带；失效时服务端回 403，由上层重领重试。
  const res = await fetch(url, { headers: { ...trackHeaders(), ...extraHeaders } })
  let data = {}
  try {
    data = await res.json()
  } catch {
    throw new Error('服务返回格式异常')
  }
  return { res, data }
}

function checked({ res, data }) {
  if (!res.ok || data.code !== 0) {
    const err = new Error(data.msg || `请求失败（${res.status}）`)
    err.code = data.code
    err.status = res.status
    throw err
  }
  return data
}

/**
 * 通用请求。带匿名票据；票据失效（code 403）时强制重领并重试一次。
 * 只重试票据错误，其他错误原样抛出，避免把限流 429 之类的错误吞掉。
 */
async function request(path, params = {}, extraHeaders = {}) {
  const ticket = await ensureTicket()
  let out = await rawRequest(path, params, { 'X-Ticket': ticket, ...extraHeaders })
  if (out.res.status === 403 && out.data.code === 403) {
    invalidateTicket()
    const fresh = await ensureTicket(true)
    out = await rawRequest(path, params, { 'X-Ticket': fresh, ...extraHeaders })
  }
  return checked(out)
}

// source 对齐后端 src：tg = 只用 TG（默认）；plugin = 只用海搜接口（计费）。
// 前端只暴露这两个明确档，不再用 auto（TG 优先自动降级）。
export function searchResources({ q, platforms, page, pageSize, searchIn = 'title', source = 'tg', refresh = false }) {
  return request('/api/search', {
    q,
    platforms,
    page,
    page_size: pageSize,
    search_in: searchIn,
    src: source,
    // 强制回源会真实消耗上游额度（后端按 3 份配额计），只有用户主动点
    // 「强制刷新」才带。翻页 / 换平台 / 检测结论补拉一律走缓存。
    // 传 undefined 时 rawRequest 会跳过，不会给普通请求拼上 refresh=0。
    refresh: refresh ? 1 : undefined,
  })
}

export function fetchPlatforms() {
  return request('/api/platforms')
}

// 群入口配置（二维码 / 群号 / 弹窗策略）。拉不到不影响搜索，前端只当没有。
export function fetchGroup() {
  return request('/api/group')
}

// 反馈 / 求资源。和搜索不同，失败要把服务端的原话给用户看——
// "提交太快了"和"缺少必要信息"是两种完全不同的处理方式。
export async function submitFeedback(payload) {
  const res = await fetch(`${BASE}/api/feedback`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...trackHeaders() },
    body: JSON.stringify(payload),
  })
  let data = {}
  try {
    data = await res.json()
  } catch {
    throw new Error('服务返回格式异常')
  }
  if (!res.ok || data.code !== 0) {
    throw new Error(data.msg || `提交失败（${res.status}）`)
  }
  return data
}
