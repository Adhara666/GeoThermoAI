// API 封装：fetch + SSE，统一携带 JWT（Authorization: Bearer / ?token=）
// 401 时清除本地 token 并广播未授权事件，由 App.vue 切回登录页

const TOKEN_KEY = 'gtai_token'

// 内存兜底：iframe / 隐私模式下 localStorage 可能不可用，写入失败也不能丢 token
let _memToken = ''

function lsGet() {
  try { return localStorage.getItem(TOKEN_KEY) || '' } catch (_) { return '' }
}

function lsSet(t) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t)
    else localStorage.removeItem(TOKEN_KEY)
  } catch (_) {}
}

export function getToken() {
  if (_memToken) return _memToken
  return lsGet()
}

export function setToken(t) {
  _memToken = t || ''
  lsSet(t)
}

// 附加 token 到 URL query：ModelScope 反向代理会剥离 Authorization header，
// 但 query 参数能穿透（SSE/下载已验证），因此统一走 ?token=
function withTokenQuery(url) {
  const t = getToken()
  if (!t) return url
  return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(t)
}

async function req(method, url, body) {
  const opts = { method, headers: {} }
  const token = getToken()
  if (token) {
    opts.headers['Authorization'] = `Bearer ${token}` // 本地直连双保险
    url = withTokenQuery(url)
  }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(url, opts)
  if (res.status === 401) {
    setToken('')
    window.dispatchEvent(new CustomEvent('gtai:unauthorized'))
  }
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`${res.status} ${txt.slice(0, 200)}`)
  }
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return res.json()
  return res.text()
}

export const api = {
  get: (url) => req('GET', url),
  post: (url, body) => req('POST', url, body),
  del: (url) => req('DELETE', url),

  // ── 账号 ─────────────────────────────────────────────────
  login: (username, password) => req('POST', '/api/auth/login', { username, password }),
  register: (username, password, nickname) => req('POST', '/api/auth/register', { username, password, nickname }),
  me: () => req('GET', '/api/auth/me'),

  async uploadStudyArea(files) {
    const fd = new FormData()
    for (const f of files) fd.append('files', f)
    const res = await fetch(withTokenQuery('/api/study-area'), { method: 'POST', body: fd, headers: tokenHeader() })
    if (!res.ok) throw new Error(`上传失败 ${res.status}`)
    return res.json()
  },

  setCurrentStudyArea: (name) => req('POST', '/api/study-area/current', { name }),
  deleteStudyArea: (name) => req('DELETE', `/api/study-area?name=${encodeURIComponent(name)}`),

  /** 建立 SSE 连接；onEvent(type, data)；返回 { close }（升级点 5/6：切换对话时主动关闭旧连接） */
  stream(convId, onEvent, onError) {
    const es = new EventSource(`/api/chat/stream?conv=${encodeURIComponent(convId)}&token=${encodeURIComponent(getToken())}`)
    es.onmessage = (e) => { try { onEvent('message', JSON.parse(e.data)) } catch (_) {} }
    ;['token', 'thinking', 'append', 'pause', 'workflow', 'log', 'done', 'error'].forEach((t) => {
      es.addEventListener(t, (e) => {
        let data = {}
        try { data = JSON.parse(e.data) } catch (_) {}
        // pause 后由 resume() 建立新连接，这里必须显式关闭，否则 EventSource
        // 会自动重连并占用后端流锁，导致后续 resume 的连接被阻塞
        if (t === 'pause' || t === 'done' || t === 'error') es.close()
        onEvent(t, data)
      })
    })
    es.onerror = () => {
      // EventSource 自动重连；close 后不会再触发
      if (es.readyState === EventSource.CLOSED && onError) onError('连接已关闭')
    }
    return { close: () => { try { es.close() } catch (_) {} } }
  },
}

function tokenHeader() {
  const t = getToken()
  return t ? { Authorization: `Bearer ${t}` } : {}
}

export const downloadUrl = (projectDir, path) =>
  `/api/download?project_dir=${encodeURIComponent(projectDir || '')}&path=${encodeURIComponent(path || '')}&token=${encodeURIComponent(getToken())}`
