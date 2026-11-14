// 看板统一请求层：口令、JSON、401 都在这里处理一次，组件里不再重复拼 headers。

const TOKEN_KEY = 'pt_admin_token'

let token = ''

export function readToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch (e) {
    return ''
  }
}

export function setToken(value) {
  token = value || ''
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch (e) {
    /* 隐私模式下 localStorage 可能不可写，内存里仍有值 */
  }
}

export function hasToken() {
  return !!token
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * @param {string} path
 * @param {{method?: string, body?: any, raw?: BodyInit}} options
 */
export async function api(path, options = {}) {
  const headers = {}
  if (token) headers['x-admin-token'] = token

  let body
  if (options.raw !== undefined) {
    body = options.raw
  } else if (options.body !== undefined) {
    headers['content-type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  const res = await fetch(path, { method: options.method || 'GET', headers, body })
  let json = null
  try {
    json = await res.json()
  } catch (e) {
    /* 空响应或非 JSON */
  }

  if (res.status === 401) throw new ApiError('口令无效或已过期', 401)
  if (!json || json.code !== 0) {
    throw new ApiError((json && json.msg) || `请求失败（HTTP ${res.status}）`, res.status)
  }
  return json.data
}

// 各业务接口
export const stats = (days) => api(`/api/admin/stats?days=${days}`)
export const feedback = (days) => api(`/api/admin/feedback?days=${days}&limit=100`)
export const markFeedbackDone = (id) => api(`/api/admin/feedback/${encodeURIComponent(id)}`, {
  method: 'POST',
  body: { status: 'done' },
})
export const plan = (days, page, pageSize) =>
  api(`/api/admin/plan?days=${days}&page=${page}&pageSize=${pageSize}`)
export const ownLinks = (days) => api(`/api/admin/own?days=${days}`)
export const saveOwnLink = (payload) => api('/api/admin/own', { method: 'POST', body: payload })
export const parseOwnShare = (text, keyword) =>
  api('/api/admin/own/parse', { method: 'POST', body: { text, keyword: keyword || '' } })
export const bulkSaveOwnLinks = (items) =>
  api('/api/admin/own/bulk', { method: 'POST', body: { items } })
export const deleteOwnLink = (id) => api(`/api/admin/own/${encodeURIComponent(id)}`, { method: 'DELETE' })
export const groupConfig = () => api('/api/admin/group')
export const saveGroupConfig = (payload) => api('/api/admin/group', { method: 'POST', body: payload })
export const uploadGroupQr = (file) => api('/api/admin/group/upload', { method: 'POST', raw: file })

// 网盘账号：cookie / token 只上行不下行，列表里给的是 hasCookie / hasToken 布尔值
export const accounts = () => api('/api/admin/accounts')
export const addAccount = (payload) => api('/api/admin/accounts', { method: 'POST', body: payload })
export const updateAccount = (id, payload) =>
  api(`/api/admin/accounts/${id}`, { method: 'PATCH', body: payload })
export const deleteAccount = (id) => api(`/api/admin/accounts/${id}`, { method: 'DELETE' })
export const testAccount = (id) => api(`/api/admin/accounts/${id}/test`, { method: 'POST' })
export const cleanupMirrors = (coldDays) =>
  api(`/api/admin/maintain/cleanup?cold_days=${coldDays || 0}`, { method: 'POST' })
export const mirrorTest = (url, pwd) =>
  api('/api/admin/mirror/test', { method: 'POST', body: { url, pwd: pwd || '' } })
