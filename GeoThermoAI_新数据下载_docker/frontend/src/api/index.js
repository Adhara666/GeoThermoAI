// API 封装：fetch + SSE

async function req(method, url, body) {
  const opts = { method, headers: {} }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(url, opts)
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

  async uploadStudyArea(files) {
    const fd = new FormData()
    for (const f of files) fd.append('files', f)
    const res = await fetch('/api/study-area', { method: 'POST', body: fd })
    if (!res.ok) throw new Error(`上传失败 ${res.status}`)
    return res.json()
  },

  /** 建立 SSE 连接；onEvent(type, data)；返回 AbortController */
  stream(convId, onEvent, onError) {
    const ctrl = new AbortController()
    const es = new EventSource(`/api/chat/stream?conv=${encodeURIComponent(convId)}`)
    es.onmessage = (e) => { try { onEvent('message', JSON.parse(e.data)) } catch (_) {} }
    ;['token', 'append', 'pause', 'workflow', 'log', 'done', 'error'].forEach((t) => {
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
    return ctrl
  },
}

export const downloadUrl = (projectDir, path) =>
  `/api/download?project_dir=${encodeURIComponent(projectDir || '')}&path=${encodeURIComponent(path || '')}`
