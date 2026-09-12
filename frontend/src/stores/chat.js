import { defineStore } from 'pinia'
import { api, getToken } from '../api'
import { useToast } from '../composables/useToast'
import { t, trServer } from '../i18n'

const EXEC_MODE_KEY = 'gtai_exec_mode'
const CHAT_MODE_KEY = 'gtai_chat_mode'

// 模块级：当前激活的 SSE 流（切换对话/项目时主动关闭旧连接，
// 避免旧对话的流式回调继续串写全局 store）
let activeStream = null

// 第六阶段：对话事件流（台账）连接，一条连接服务整个对话的多任务
let _kernelStream = null

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
    logMaxId: 0, // 已接收的日志行号上限（服务端持久行号，用于历史/实时去重）
    modelLabel: '',
    // ── 第六阶段（§12.3）：按编号存放的台账状态 ──
    // 任务卡按任务编号，问题卡按问题编号；一条对话事件流服务多任务，
    // 某个任务完成/暂停不再影响其他任务卡的展示与操作。
    kernelTasks: {}, // task_id -> 任务卡（label/状态/节点/排队原因/版本）
    kernelQuestions: {}, // question_id -> 问题卡（含固定目标列表与版本）
    kernelArtifacts: [], // 本对话正式产物（按产物编号，地图/精度/下载绑定）
    kernelQuestionsOrder: [],
    kernelTasksOrder: [],
    eventCursor: 0, // 已消费事件游标（断线重连后从此处补发）
    kernelActive: false,
    // 选中任务中枢（面板联动）：conv → task_id；地图/精度/下载/日志
    // 按选中任务的产物解析，未选中时回退旧行为（兼容旧链路）
    activeTaskByConv: {},
  }),

  getters: {
    lastAssistant() {
      for (let i = this.messages.length - 1; i >= 0; i--) {
        if (this.messages[i].role === 'assistant') return this.messages[i]
      }
      return null
    },
    /** 当前对话选中的任务 id（空串=未选中） */
    activeTaskId() {
      return this.activeTaskByConv[useProjectStore().currentConv] || ''
    },
    /** 选中任务的产物列表（按产物编号） */
    activeTaskArtifacts() {
      const tid = this.activeTaskId
      if (!tid) return []
      return (this.kernelArtifacts || []).filter((a) => a.task_id === tid)
    },
  },

  actions: {
    async setExecMode(mode) {
      const next = mode === 'auto' ? 'auto' : 'approval'
      this.execMode = next
      try { localStorage.setItem(EXEC_MODE_KEY, next) } catch (_) {}
      // 立即通知后端（不改变已编译运行的科学参数）：切到“完全执行”时，
      // 等待中的配对选择自动代选，任务无需重发消息即继续
      const project = useProjectStore()
      if (!project.currentConv) return
      try {
        const r = await api.post('/api/exec-mode', {
          project: project.currentProject,
          conv: project.currentConv,
          exec_mode: next,
        })
        if (r && r.auto_answered > 0) {
          this.refreshKernelSnapshot(project.currentConv)
        }
      } catch (_) { /* 通知失败不影响本地选择 */ }
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
        // 切换对话：丢弃未刷新的缓冲（日志已由服务端持久化，新对话从数据库回填）
        _logBuf = []
        if (_logTimer) { clearTimeout(_logTimer); _logTimer = null }
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
      // 日志改从服务端持久表加载（跨刷新/重启不丢）；
      // 旧的内存缓存与缓冲仅作清理，不再作为恢复来源
      this.logAll = []
      this.logLines = []
      this.logMaxId = 0
      _logBuf = []
      this.workflowSteps = []
      const data = await api.get(`/api/messages?project=${encodeURIComponent(pid)}&conv=${encodeURIComponent(cid)}`)
      this.messages = normalizeMessages(data.messages || [])
      // 第六阶段：拉取台账会话快照并订阅对话事件流（多任务复用一条连接）
      await this.refreshKernelSnapshot(cid)
      // 日志历史先于 SSE 订阅加载（订阅以 logMaxId 为起点补发，不重不漏）
      await this.loadLogHistory(cid)
      this._listenKernelEvents(cid)
    },

    /** 对话执行日志历史（持久化）：刷新/重启/中断后仍完整回填 */
    async loadLogHistory(cid) {
      if (!cid) return
      try {
        const tz = -new Date().getTimezoneOffset() / 60
        const r = await api.get(
          `/api/conversations/${encodeURIComponent(cid)}/logs?tz=${encodeURIComponent(tz)}`)
        if (cid !== useProjectStore().currentConv) return
        const logs = Array.isArray(r.logs) ? r.logs : []
        this.logAll = logs
        this.logLines = logs.slice(-LOG_VIEW_MAX)
        this.logMaxId = logs.reduce((m, x) => Math.max(m, Number(x.id) || 0), 0)
      } catch (_) { /* 历史加载失败不影响实时追加 */ }
    },

    /** 清除本对话日志（用户显式操作：服务端持久记录一并清） */
    async clearLogs() {
      if (_logTimer) { clearTimeout(_logTimer); _logTimer = null }
      _logBuf = []
      this.logAll = []
      this.logLines = []
      this.logMaxId = 0
      const cid = useProjectStore().currentConv
      if (!cid) return
      try {
        await api.del(`/api/conversations/${encodeURIComponent(cid)}/logs`)
      } catch (_) { /* 清除失败仅影响持久历史 */ }
    },

    /** 台账会话快照：任务卡/问题卡/产物/事件游标（§12.1）*/
    async refreshKernelSnapshot(cid) {
      if (!cid) return
      try {
        const snap = await api.get(`/api/conversations/${encodeURIComponent(cid)}/snapshot`)
        this._applyKernelSnapshot(snap)
      } catch (_) {}
    },

    _applyKernelSnapshot(snap) {
      if (!snap) return
      // 前端按事件编号去重：旧快照（游标更小且非初次）不覆盖新状态（§12.2）
      const cursor = Number(snap.event_cursor || 0)
      const tasks = Array.isArray(snap.tasks) ? snap.tasks : []
      const questions = Array.isArray(snap.questions) ? snap.questions : []
      const nextTasks = {}
      const nextOrder = []
      for (const t of tasks) {
        const id = t.task_id
        const prev = this.kernelTasks[id]
        // 按版本拒绝旧状态覆盖新状态（§12.2 协议第 3 步）
        if (prev && Number(prev.version || 0) > Number(t.version || 0)
            && prev._fromEvent) continue
        nextTasks[id] = { ...t, _fromEvent: false }
        nextOrder.push(id)
      }
      this.kernelTasks = nextTasks
      this.kernelTasksOrder = nextOrder
      const nextQ = {}
      const nextQOrder = []
      for (const q of questions) {
        const prev = this.kernelQuestions[q.id]
        if (prev && Number(prev.version || 0) > Number(q.version || 0)
            && prev._fromEvent) continue
        nextQ[q.id] = { ...q, _fromEvent: false }
        nextQOrder.push(q.id)
      }
      this.kernelQuestions = nextQ
      this.kernelQuestionsOrder = nextQOrder
      this.kernelArtifacts = Array.isArray(snap.artifacts) ? snap.artifacts : []
      if (cursor >= this.eventCursor) this.eventCursor = cursor
    },

    /** 对话事件流（§12.2）：快照 → 游标补发 → 持续推送；一条连接服务多任务。
     *  断线后浏览器自动重连，重连请求带游标，服务端从游标之后补发。 */
    _listenKernelEvents(cid) {
      if (_kernelStream) { try { _kernelStream.close() } catch (_) {} _kernelStream = null }
      if (!cid) return
      const listeningConv = cid
      // tz：用户本地时区偏移（与旧链路日志时间戳同一约定，永远等于用户电脑时间）
      const tz = -new Date().getTimezoneOffset() / 60
      const url = `/api/conversations/${encodeURIComponent(cid)}/events`
        + `?cursor=${this.eventCursor}&tz=${encodeURIComponent(tz)}`
        + `&log_cursor=${this.logMaxId || 0}`
        + (getToken() ? `&token=${encodeURIComponent(getToken())}` : '')
      const es = new EventSource(url)
      _kernelStream = es
      es.addEventListener('snapshot', (e) => {
        if (listeningConv !== useProjectStore().currentConv) return
        try { this._applyKernelSnapshot(JSON.parse(e.data || '{}')) } catch (_) {}
      })
      es.addEventListener('ledger', (e) => {
        if (listeningConv !== useProjectStore().currentConv) return
        try {
          const ev = JSON.parse(e.data || '{}')
          const seq = Number(ev.seq || 0)
          if (seq <= this.eventCursor) return // 按序号去重
          this.eventCursor = seq
          // 节点/任务汇总变化时拉一次快照（有界：事件聚合触发，非逐条）
          if ((ev.type || '').startsWith('node.') || (ev.type || '').startsWith('task.')
              || (ev.type || '').startsWith('commit.') || (ev.type || '').startsWith('artifact.')) {
            this.refreshKernelSnapshot(listeningConv)
          }
        } catch (_) {}
      })
      // 调度器执行日志 → 日志面板（持久行号去重；含失败原因/重试/完成）
      es.addEventListener('log', (e) => {
        if (listeningConv !== useProjectStore().currentConv) return
        try {
          const d = JSON.parse(e.data || '{}')
          if (d.text) this._acceptLog(d.text, d.task_id || '', d.id || 0)
        } catch (_) {}
      })
      // 服务端主动追加的消息（任务完成报告等）：直接追加到对话流
      es.addEventListener('message', (e) => {
        if (listeningConv !== useProjectStore().currentConv) return
        try {
          const d = JSON.parse(e.data || '{}')
          const list = Array.isArray(d.messages) ? d.messages : []
          const add = list.filter((m) => m && m.content &&
            !this.messages.some((x) => x.role === m.role && x.content === m.content))
          if (add.length) {
            this.messages = this.messages.concat(
              add.map((m) => ({ role: m.role, content: m.content })))
          }
        } catch (_) {}
      })
      es.onerror = () => {
        // EventSource 自带重连；重连请求需带最新游标 → 重建连接
        if (_kernelStream === es) {
          es.close()
          _kernelStream = null
          setTimeout(() => {
            if (listeningConv === useProjectStore().currentConv) {
              this._listenKernelEvents(listeningConv)
            }
          }, 2000)
        }
      }
    },

    /** 设置选中任务（面板联动中枢）：点任务卡或选择器都会调用 */
    setActiveTask(taskId) {
      const cid = useProjectStore().currentConv
      if (!cid) return
      this.activeTaskByConv = {
        ...this.activeTaskByConv,
        [cid]: taskId || '',
      }
    },

    /** 回答问题卡（第六阶段弹块内可点击选项）：与文字回答共用同一答案通道，
     *  过期卡片返回明确提示而不错误执行。成功时把服务端写入的
     *  “用户回答 / 助手确认”两条气泡追加到对话流（与文字回答体验一致）。 */
    async answerKernelQuestion(questionId, text) {
      const toast = useToast()
      try {
        const r = await api.post('/api/kernel/answer', {
          project: useProjectStore().currentProject,
          conv: useProjectStore().currentConv,
          question_id: questionId,
          answer: text,
        })
        if (!r.ok) {
          toast.error(trServer(r.message)
            || (r.expired ? '这张问题卡已失效（任务已更新或问题已被回答）' : '提交失败'))
          this.refreshKernelSnapshot(useProjectStore().currentConv)
          return r
        }
        // 成功：追加服务端持久化的两条气泡（不弹右上角通知，用户实测要求）
        if (Array.isArray(r.appended) && r.appended.length) {
          this.messages = this.messages.concat(r.appended.map((m) => ({
            role: m.role, content: m.content,
          })))
        }
        this.refreshKernelSnapshot(useProjectStore().currentConv)
        return r
      } catch (e) {
        toast.error(e.message)
        return { ok: false }
      }
    },

    /** 任务命令（§12.1）：取消 / 重试 / 优先级，带所见版本校验（409 语义） */
    async taskCommand(taskId, operation, payload = {}) {
      const toast = useToast()
      const seen = Number(this.kernelTasks[taskId]?.version || 0)
      try {
        const r = await api.post(`/api/tasks/${encodeURIComponent(taskId)}/commands`, {
          operation,
          request_id: (crypto?.randomUUID?.() || `cmd-${Date.now()}`),
          seen_version: seen,
          payload,
        })
        if (!r.ok) {
          toast.error(trServer(r.message) || (r.conflict ? '状态已变化，请刷新' : '命令失败'))
          if (r.conflict) this.refreshKernelSnapshot(useProjectStore().currentConv)
          return r
        }
        toast.success(t('wf.status.running'))
        this.refreshKernelSnapshot(useProjectStore().currentConv)
        return r
      } catch (e) {
        toast.error(e.message)
        return { ok: false }
      }
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
        // 日志恢复：持久历史由 loadLogHistory 负责（刷新/重启不丢失），
        // 此处不再依赖内存态 cur.logs 补齐
      } catch (_) {}
    },

    async send(message) {
      const t2 = useToast()
      if (this.streaming) { t2.info(t('chat.busy')); return }
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
        // 请求编号（升级第一阶段：状态内核命令去重）。同一次发送的重试/重复
        // 提交携带同一编号时，后端只记一次；界面无感知。
        if (!this._requestSeq) this._requestSeq = 0
        const requestId = (crypto?.randomUUID?.() || `req-${Date.now()}-${++this._requestSeq}`)
        const r = await api.post('/api/chat/start', {
          project: useProjectStore().currentProject,
          conv: useProjectStore().currentConv,
          message: msg,
          exec_mode: this.execMode,
          chat_mode: this.chatMode, // Chat=只读对话 / Work=完整执行
          request_id: requestId,
        })
        if (!r.ok) { t2.error(trServer(r.message) || t('chat.sendFailed')); this.streaming = false; return }
        if (r.messages) this.messages = normalizeMessages(r.messages)
        await this._listen(useProjectStore().currentConv)
      } catch (e) {
        t2.error(t('chat.sendFailedMsg', { msg: e.message }))
        this.streaming = false
      }
    },

    async _listen(conv) {
      const toast = useToast()
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
          // 旧聊天流日志：带行号对象（{text, id, task_id}）与纯字符串都兼容
          this._acceptLog(data.text, data.task_id || '', data.id || 0)
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
          toast.error(trServer(data.message) || t('chat.execError'))
          this.streaming = false
          this.paused = false
        }
      })
    },

    async resume(pairIndex) {
      const toast = useToast()
      try {
        const r = await api.post('/api/chat/resume', {
          conv: useProjectStore().currentConv,
          pair_index: pairIndex,
        })
        if (!r.ok) { toast.error(trServer(r.message)); return }
        this.paused = false
        this.pairs = []
        this.approval = null
        this.streaming = true
        await this._listen(useProjectStore().currentConv)
      } catch (e) {
        toast.error(t('chat.resumeFailed', { msg: e.message }))
      }
    },

    /** 通用审批节点恢复（新协议） */
    async resumeApproval(optionId, values) {
      const toast = useToast()
      try {
        const r = await api.post('/api/chat/resume', {
          conv: useProjectStore().currentConv,
          option_id: optionId,
          values: values || {},
        })
        if (!r.ok) { toast.error(trServer(r.message)); return }
        this.paused = false
        this.approval = null
        this.pairs = []
        this.streaming = true
        await this._listen(useProjectStore().currentConv)
      } catch (e) {
        toast.error(t('chat.resumeFailed', { msg: e.message }))
      }
    },

    /** 接收一条实时日志：完整保存到 logAll，渲染窗口 logLines 走 120ms 节流合并。
     *  带服务端持久行号（id>0）时按行号去重（历史与实时、双通道不重复）；
     *  高频下载进度上报若逐行触发响应式更新会占满主线程（页面全卡），
     *  合并成 120ms 一批只重渲染尾部 LOG_VIEW_MAX 行。 */
    _acceptLog(text, taskId = '', id = 0) {
      const lineId = Number(id) || 0
      if (lineId > 0 && lineId <= this.logMaxId) return // 已从历史接收过
      const line = String(text || '').replace(/\r?\n$/, '')
      if (!line) return
      if (lineId > 0) this.logMaxId = lineId
      // 日志条目带任务号供日志面板按任务过滤；无任务号的归入“全部”视图
      const entry = (taskId || lineId)
        ? { text: line, task_id: taskId || '', id: lineId }
        : { text: line }
      _logBuf.push(entry)
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
      if (_kernelStream) {
        try { _kernelStream.close() } catch (_) {}
        _kernelStream = null
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
      this.logMaxId = 0
      this.workflowSteps = [] // 复位进度面板为"等待"（删除项目/对话后立即变等待，不再残留旧进度）
    },
  },
})

import { useProjectStore } from './project'
