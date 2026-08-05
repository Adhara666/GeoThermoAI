import { defineStore } from 'pinia'
import { api } from '../api'
import { useToast } from '../composables/useToast'

export const useChatStore = defineStore('chat', {
  state: () => ({
    messages: [],
    streaming: false,
    paused: false,
    pairs: [],
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
    async loadMessages(pid, cid) {
      const data = await api.get(`/api/messages?project=${encodeURIComponent(pid)}&conv=${encodeURIComponent(cid)}`)
      this.messages = data.messages || []
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
      this.logLines = [] // 新流程开始时清空日志面板
      try {
        const r = await api.post('/api/chat/start', {
          project: useProjectStore().currentProject,
          conv: useProjectStore().currentConv,
          message: msg,
        })
        if (!r.ok) { t.error(r.message || '发送失败'); this.streaming = false; return }
        if (r.messages) this.messages = r.messages
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
      api.stream(conv, (type, data) => {
        if (type === 'token') {
          const last = this.messages[this.messages.length - 1]
          if (last) last.content = data.content || last.content
          this.messages = [...this.messages]
        } else if (type === 'pause') {
          this.paused = true
          this.pairs = data.pairs || []
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
          this.messages = [...this.messages]
          this.streaming = false
          this.paused = false
          this.pairs = []
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
      this.logLines = []
    },
  },
})

import { useProjectStore } from './project'
