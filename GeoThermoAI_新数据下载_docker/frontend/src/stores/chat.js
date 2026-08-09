import { defineStore } from 'pinia'
import { api } from '../api'
import { useToast } from '../composables/useToast'

const EXEC_MODE_KEY = 'gtai_exec_mode'
const CHAT_MODE_KEY = 'gtai_chat_mode'

// 模块级：当前激活的 SSE 流（升级点 5/6：切换对话/项目时主动关闭旧连接，
// 避免旧对话的流式回调继续串写全局 store）
let activeStream = null

function loadExecMode() {
  try {
    const v = localStorage.getItem(EXEC_MODE_KEY)
    return v === 'auto' || v === 'approval' ? v : 'approval'
  } catch (_) {
    return 'approval'
  }
}

// Chat / Work 双模式（升级点 17）：Chat=只读对话，Work=完整执行
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
    // 旧格式 summary「已深度思考（12.3s）」→ 迁移为 thinking_seconds（升级点 16）
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
    chatMode: loadChatMode(), // 'work'（完整执行）| 'chat'（只读对话，升级点 17）
    workflowSteps: [], // [{id,label,status}]
    logLines: [], // 实时过程日志（日志面板）
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
      // 升级点 6：切换对话/项目时重置上一对话的暂停态、配对卡、审批、日志与进度，
      // 避免上一对话的弹窗/日志串扰当前对话（正在运行的流由 resumeIfStreaming 恢复）
      this.streaming = false
      this.paused = false
      this.pairs = []
      this.approval = null
      this.logLines = []
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
            if (cur.thinking_seconds) last.thinking_seconds = cur.thinking_seconds // 升级点 16
            this.messages = [...this.messages]
          }
        }
        if (cur && cur.active) {
          this.streaming = true
          this.paused = false
          await this._listen(cid)
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
      this.logLines = [] // 新流程开始时清空日志面板
      try {
        const r = await api.post('/api/chat/start', {
          project: useProjectStore().currentProject,
          conv: useProjectStore().currentConv,
          message: msg,
          exec_mode: this.execMode,
          chat_mode: this.chatMode, // 升级点 17：Chat=只读对话 / Work=完整执行
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
      // 升级点 5/6：先关闭旧对话的连接，新事件只属于当前监听会话
      if (activeStream) {
        try { activeStream.close() } catch (_) {}
        activeStream = null
      }
      const listeningConv = conv
      activeStream = api.stream(conv, (type, data) => {
        // 双保险：事件只属于当前激活对话（切换对话后即使旧连接未被 close 也忽略）
        if (listeningConv !== useProjectStore().currentConv) return
        if (type === 'thinking') {
          // 思考过程实时更新（升级点 15）：独立字段渲染在折叠块内
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
          }
          this.messages = [...this.messages]
        } else if (type === 'pause') {
          this.paused = true
          this.pairs = data.pairs || []
          this.approval = data.approval || null
          // 升级点 16：由我批准模式在 plan_confirm/选影像处暂停，
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
          const text = (data.text || '').replace(/\r?\n$/, '')
          if (text) {
            this.logLines.push(text)
            // 防止日志无限增长拖慢渲染（下载进度按 2MB 粒度上报，大文件可能上千行）
            if (this.logLines.length > 3000) this.logLines = this.logLines.slice(-3000)
            this.logLines = [...this.logLines]
          }
        } else if (type === 'done') {
          const last = this.messages[this.messages.length - 1]
          if (last && data.content) last.content = data.content
          if (last && data.thinking) {
            last.thinking = data.thinking
            last.thinkingDone = true
          }
          if (last && data.thinking_seconds) last.thinking_seconds = data.thinking_seconds // 升级点 16
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

    /** 通用审批节点恢复（技术方案 3.3 新协议） */
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

    clear() {
      this.messages = []
      this.streaming = false
      this.paused = false
      this.pairs = []
      this.approval = null
      this.logLines = []
    },
  },
})

import { useProjectStore } from './project'
