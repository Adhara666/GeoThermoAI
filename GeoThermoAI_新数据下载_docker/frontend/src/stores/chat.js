import { defineStore } from 'pinia'
import { api } from '../api'
import { useToast } from '../composables/useToast'

const EXEC_MODE_KEY = 'gtai_exec_mode'
const CHAT_MODE_KEY = 'gtai_chat_mode'

// 模块级：当前激活的 SSE 流（切换对话/项目时主动关闭旧连接，
// 避免旧对话的流式回调继续串写全局 store）
let activeStream = null

// 实时日志双层结构（根治「全流程后页面卡顿」）：
//   logAll    完整日志（供复制按钮导出，不被渲染）
//   logLines  渲染窗口，只保留尾部 LOG_VIEW_MAX 行（日志面板 v-for 只渲染这段）
// 下载阶段日志事件高频（并发分块按 2MB 粒度上报），每行都做响应式替换会占满
// 主线程 → 页面所有交互（含气泡滚动条）卡死；因此日志批量合并到 120ms 节流窗口
// 再更新一次 logLines。
const LOG_ALL_MAX = 20000
const LOG_VIEW_MAX = 1000

// 按对话暂存日志：切换对话不丢（切回原对话时恢复），LRU 上限 5 个防内存膨胀；
// 刷新页面后内存清空（实时日志本来就是会话内存态，与后端会话文件无关）
let _convLogCache = {}
let _convLogOrder = []
const LOG_CACHE_MAX = 5

let _logBuf = []
let _logTimer = null
let _activeConvId = ''

function loadExecMode() {
  try {
    const v = localStorage.getItem(EXEC_MODE_KEY)
    return v === 'auto' || v === 'approval' ? v : 'approval'
  } catch (_) {
    return 'approval'
  }
}

// Chat / Work 双模式：Chat=只读对话，Work=完整执行
function loadChatMode() {
  try {
    const v = localStorage.getItem(CHAT_MODE_KEY)
    return v === 'chat' || v === 'work' ? v : 'work'
  } catch (_) {
    return 'work'
  }
}

// 历史兼容：旧格式把思考链以 <details>...</details> 内嵌在 content 里，
// 新版改用独立 thinking 字段渲染。加载历史消息时把旧格式迁移到 thinking 字段。
function normalizeMessages(msgs) {
  if (!Array.isArray(msgs)) return []
  return msgs.map((m) => {
    if (m.role !== 'assistant' || m.thinking) {
      // 已完成的思考链（历史消息）默认折叠，等用户点击再展开
      if (m.thinking) m.thinkingDone = true
      return m
    }
    const c = m.content || ''
    const m2 = /<details[^>]*>([\s\S]*?)<\/details>/.exec(c)
    if (!m2) return m
    const thinking = (m2[1] || '').replace(/^[\s\S]*?<\/summary>\s*/i, '').trim()
    // 旧格式 summary「已深度思考（12.3s）」→ 迁移为 thinking_seconds
    const secMatch = (m2[1] || '').match(/已深度思考（([\d.]+)s）/)
    const rest = (c.slice(0, m2.index) + c.slice(m2.index + m2[0].length)).trim()
    return {
      ...m,
      thinking: thinking || undefined,
      thinking_seconds: secMatch ? Number(secMatch[1]) : undefined,
      thinkingDone: true, // 历史消息：思考已完成，折叠展示
      content: rest || ' ',
    }
  })
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    messages: [],
    streaming: false,
    paused: false,
    pairs: [],
    approval: null, // 通用审批载荷 {type,node,title,summary,options,default_option}
    execMode: loadExecMode(), // 'approval'（由我批准）| 'auto'（完全执行）
    chatMode: loadChatMode(), // 'work'（完整执行）| 'chat'（只读对话）
    workflowSteps: [], // [{id,label,status}]
    logLines: [], // 实时过程日志（日志面板渲染窗口，尾部 LOG_VIEW_MAX 行）
    logAll: [], // 完整实时日志（供复制导出，不参与渲染）
    modelLabel: '',
  }),

  getters: {
    lastAssistant() {
      for (let i = this.messages.length - 1; i >= 0; i--) {
        if (this.messages[i].role === 'assistant') return this.messages[i]
      }
      return null
    },
  },

  actions: {
    setExecMode(mode) {
      const next = mode === 'auto' ? 'auto' : 'approval'
      this.execMode = next
      try { localStorage.setItem(EXEC_MODE_KEY, next) } catch (_) {}
    },

    setChatMode(mode) {
      const next = mode === 'chat' ? 'chat' : 'work'
      this.chatMode = next
      try { localStorage.setItem(CHAT_MODE_KEY, next) } catch (_) {}
    },

    async loadMessages(pid, cid) {
      // 切换对话/项目时重置上一对话的暂停态、配对卡、审批与进度，
      // 避免上一对话的弹窗/日志串扰当前对话（正在运行的流由 resumeIfStreaming 恢复）
      // 日志例外：按对话暂存，切回原对话时恢复，不再被清空
      if (_activeConvId && _activeConvId !== cid) {
        // 先把未刷新的缓冲日志并入当前对话完整日志再整体缓存：否则切换后
        // 120ms 节流器会把旧对话残留的缓冲写进新对话（串台显示运行中项目的最后一句日志）
        if (_logBuf.length) {
          this.logAll = this.logAll.concat(_logBuf)
          _logBuf = []
        }
        if (_logTimer) { clearTimeout(_logTimer); _logTimer = null }
        _convLogCache[_activeConvId] = { all: this.logAll, view: this.logLines }
        _convLogOrder = _convLogOrder.filter((x) => x !== _activeConvId)
        _convLogOrder.push(_activeConvId)
        if (_convLogOrder.length > LOG_CACHE_MAX) {
          delete _convLogCache[_convLogOrder.shift()]
        }
      }
      _activeConvId = cid
      this.streaming = false
      this.paused = false
      this.pairs = []
      this.approval = null
      const saved = _convLogCache[cid]
      this.logAll = saved ? saved.all : []
      this.logLines = saved ? saved.view : []
      this.workflowSteps = []
      const data = await api.get(`/api/messages?project=${encodeURIComponent(pid)}&conv=${encodeURIComponent(cid)}`)
      this.messages = normalizeMessages(data.messages || [])
    },

    async refreshWorkflow(conv) {
      if (!conv) return
      try {
        const r = await api.get(`/api/workflow?conv=${encodeURIComponent(conv)}`)
        this.workflowSteps = r.steps || []
      } catch (_) {}
    },

    /** 重新进入对话时：立即拉取后端当前流内容替换气泡（显示最新），
     *  若该对话仍有正在运行的流则恢复 SSE 订阅继续增量更新 */
    async resumeIfStreaming(cid) {
      if (!cid || this.streaming) return
      try {
        const cur = await api.get(`/api/chat/current?conv=${encodeURIComponent(cid)}`)
        // 直接显示最新累积内容，避免先显示会话文件旧快照、等 SSE 慢慢同步
        if (cur && cur.content) {
          const last = this.messages[this.messages.length - 1]
          if (last && last.role === 'assistant') {
            last.content = cur.content
            if (cur.thinking) {
              last.thinking = cur.thinking
              last.thinkingDone = true // 已有正文 → 思考已结束
            }
            if (cur.thinking_seconds) last.thinking_seconds = cur.thinking_seconds
            this.messages = [...this.messages]
          }
        }
        if (cur && cur.active) {
          this.streaming = true
          this.paused = false
          await this._listen(cid)
        }
        // 恢复实时日志（服务端权威全量）：刷新后本地已清空、或切换对话本地滞后，
        // 用服务端累积日志补齐，保证日志面板连续性（_listen 订阅后继续追加新行）
        if (cur && Array.isArray(cur.logs) && cur.logs.length >= this.logAll.length) {
          this.logAll = cur.logs
          this.logLines = cur.logs.slice(-LOG_VIEW_MAX)
        }
      } catch (_) {}
    },

    async send(message) {
      const t = useToast()
      if (this.streaming) { t.info('上一条回复还在生成中，请稍候'); return }
      const msg = (message || '').trim()
      if (!msg) return
      this.streaming = true
      this.paused = false
      this.pairs = []
      this.approval = null
      // 同一对话多轮执行的日志持续追加：不在此清空 logAll/缓存，避免后一轮
      //（如结果后处理）覆盖前一轮（如全流程）的日志；仅把未刷新的缓冲并入
      this.logAll = this.logAll.concat(_logBuf)
      _logBuf = []
      if (_logTimer) { clearTimeout(_logTimer); _logTimer = null }
      try {
        const r = await api.post('/api/chat/start', {
          project: useProjectStore().currentProject,
          conv: useProjectStore().currentConv,
          message: msg,
          exec_mode: this.execMode,
          chat_mode: this.chatMode, // Chat=只读对话 / Work=完整执行
        })
        if (!r.ok) { t.error(r.message || '发送失败'); this.streaming = false; return }
        if (r.messages) this.messages = normalizeMessages(r.messages)
        if (r.messages && r.messages.length && !this.messages[this.messages.length - 1].content?.trim()) {
          // 无需处理
        }
        await this._listen(useProjectStore().currentConv)
      } catch (e) {
        t.error(`发送失败：${e.message}`)
        this.streaming = false
      }
    },

    async _listen(conv) {
      const t = useToast()
      // 先关闭旧对话的连接，新事件只属于当前监听会话
      if (activeStream) {
        try { activeStream.close() } catch (_) {}
        activeStream = null
      }
      const listeningConv = conv
      activeStream = api.stream(conv, (type, data) => {
        // 双保险：事件只属于当前激活对话（切换对话后即使旧连接未被 close 也忽略）
        if (listeningConv !== useProjectStore().currentConv) return
        if (type === 'thinking') {
          // 思考过程实时更新：独立字段渲染在折叠块内
          const last = this.messages[this.messages.length - 1]
          if (last) {
            last.thinking = data.thinking || last.thinking
            last.thinkingDone = false // 思考进行中：折叠块展开、正文尚未开始
            this.messages = [...this.messages]
          }
        } else if (type === 'token') {
          // 第一个正文 token 到达 → 思考已结束，折叠块收起后再输出正文
          const last = this.messages[this.messages.length - 1]
          if (last) {
            if (last.thinking) last.thinkingDone = true
            last.content = data.content || last.content
            // 正文开始即带上思考用时（服务端 token 事件随附），
            // 避免长流程/中断流程（无 done 事件）时思考时间一直不显示
            if (data.thinking_seconds) last.thinking_seconds = data.thinking_seconds
          }
          this.messages = [...this.messages]
        } else if (type === 'pause') {
          this.paused = true
          this.pairs = data.pairs || []
          this.approval = data.approval || null
          // 由我批准模式在 plan_confirm/选影像处暂停，
          // done 事件不会走到，思考用时随 pause 事件送达
          const last = this.messages[this.messages.length - 1]
          if (last && data.thinking_seconds) {
            last.thinking_seconds = data.thinking_seconds
            if (last.thinking) last.thinkingDone = true // 已有正文 → 思考已结束
          }
          this.streaming = false
        } else if (type === 'workflow') {
          this.workflowSteps = data.steps || []
        } else if (type === 'log') {
          this._acceptLog(data.text)
        } else if (type === 'done') {
          const last = this.messages[this.messages.length - 1]
          if (last && data.content) last.content = data.content
          if (last && data.thinking) {
            last.thinking = data.thinking
            last.thinkingDone = true
          }
          if (last && data.thinking_seconds) last.thinking_seconds = data.thinking_seconds
          if (last && last.thinking) last.thinkingDone = true
          this.messages = [...this.messages]
          this.streaming = false
          this.paused = false
          this.pairs = []
          this.approval = null
        } else if (type === 'error') {
          t.error(data.message || '执行出错')
          this.streaming = false
          this.paused = false
        }
      }, (err) => {
        if (err && !this.streaming) { /* 连接结束 */ }
      })
    },

    async resume(pairIndex) {
      const t = useToast()
      try {
        const r = await api.post('/api/chat/resume', {
          conv: useProjectStore().currentConv,
          pair_index: pairIndex,
        })
        if (!r.ok) { t.error(r.message); return }
        this.paused = false
        this.pairs = []
        this.approval = null
        this.streaming = true
        await this._listen(useProjectStore().currentConv)
      } catch (e) {
        t.error(`恢复失败：${e.message}`)
      }
    },

    /** 通用审批节点恢复（新协议） */
    async resumeApproval(optionId, values) {
      const t = useToast()
      try {
        const r = await api.post('/api/chat/resume', {
          conv: useProjectStore().currentConv,
          option_id: optionId,
          values: values || {},
        })
        if (!r.ok) { t.error(r.message); return }
        this.paused = false
        this.approval = null
        this.pairs = []
        this.streaming = true
        await this._listen(useProjectStore().currentConv)
      } catch (e) {
        t.error(`恢复失败：${e.message}`)
      }
    },

    /** 接收一条实时日志：完整保存到 logAll，渲染窗口 logLines 走 120ms 节流合并。
     *  高频下载进度上报若逐行触发响应式更新会占满主线程（页面全卡），
     *  合并成 120ms 一批只重渲染尾部 LOG_VIEW_MAX 行。 */
    _acceptLog(text) {
      const line = String(text || '').replace(/\r?\n$/, '')
      if (!line) return
      _logBuf.push(line)
      if (_logTimer) return
      _logTimer = setTimeout(() => {
        _logTimer = null
        if (!_logBuf.length) return
        const batch = _logBuf
        _logBuf = []
        this.logAll = this.logAll.concat(batch)
        if (this.logAll.length > LOG_ALL_MAX) this.logAll = this.logAll.slice(-LOG_ALL_MAX)
        this.logLines = this.logAll.slice(-LOG_VIEW_MAX)
      }, 120)
    },

    clear() {
      // 关闭并释放当前 SSE 流（删除项目/对话时若仍在运行，避免旧流继续回调串写）
      if (activeStream) {
        try { activeStream.close() } catch (_) {}
        activeStream = null
      }
      if (_logTimer) { clearTimeout(_logTimer); _logTimer = null }
      _logBuf = []
      if (_activeConvId) delete _convLogCache[_activeConvId]
      _activeConvId = ''
      this.messages = []
      this.streaming = false
      this.paused = false
      this.pairs = []
      this.approval = null
      this.logAll = []
      this.logLines = []
      this.workflowSteps = [] // 复位进度面板为"等待"（删除项目/对话后立即变等待，不再残留旧进度）
    },
  },
})

import { useProjectStore } from './project'
