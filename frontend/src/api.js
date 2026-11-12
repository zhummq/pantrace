import { trackHeaders } from './track.js'

const BASE = import.meta.env.VITE_API_BASE || ''

async function request(path, params = {}) {
  const qs = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.append(k, v)
  })
  const url = `${BASE}${path}${qs.toString() ? '?' + qs : ''}`
  // 带上匿名访客号：服务端拿它把「搜索」和「点击」串成一条行为链
  const res = await fetch(url, { headers: trackHeaders() })
  let data = {}
  try {
    data = await res.json()
  } catch {
    throw new Error('服务返回格式异常')
  }
  if (!res.ok || data.code !== 0) {
    throw new Error(data.msg || `请求失败（${res.status}）`)
  }
  return data
}

export function searchResources({ q, platforms, page, pageSize, searchIn = 'title' }) {
  return request('/api/search', {
    q,
    platforms,
    page,
    page_size: pageSize,
    search_in: searchIn,
  })
}

export function fetchPlatforms() {
  return request('/api/platforms')
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
