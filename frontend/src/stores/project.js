import { defineStore } from 'pinia'
import { api } from '../api'
import { useToast } from '../composables/useToast'
import { useChatStore } from './chat'

export const useProjectStore = defineStore('project', {
  state: () => ({
    tree: [], // [{project, project_dir, conversations: [{id,title,updated_at}]}]
    currentProject: '',
    currentConv: '',
    projectDir: '',
    studyAreas: [],
    currentStudyArea: '',
    lastValidation: [], // [{name, level: 'ok'|'warn'|'fail', message}] 最近一次上传的研究区验证结果
    loaded: false,
  }),

  getters: {
    projects: (s) => s.tree.map((t) => t.project),
    conversations: (s) => {
      const p = s.tree.find((t) => t.project === s.currentProject)
      return p ? p.conversations : []
    },
    currentProjectDir: (s) => s.projectDir,
    currentConvTitle() {
      const p = this.tree.find((t) => t.project === this.currentProject)
      const c = p?.conversations.find((x) => x.id === this.currentConv)
      return c?.title || ''
    },
  },

  actions: {
    async bootstrap() {
      const data = await api.get('/api/bootstrap')
      this.tree = data.projects || []
      this.studyAreas = data.study_areas || []
      this.currentStudyArea = data.current_study_area || ''
      this.loaded = true
      // 默认选中第一个项目/对话
      if (!this.currentProject && this.tree.length) {
        this.selectProject(this.tree[0].project)
      }
    },

    async selectProject(pid) {
      this.currentProject = pid
      const p = this.tree.find((t) => t.project === pid)
      this.projectDir = p?.project_dir || ''
      this.currentConv = ''
      const chat = useChatStore()
      const convs = p?.conversations || []
      if (convs.length) {
        // 切换项目时自动进入第一个对话，并加载其消息/进度
        await this.selectConv(convs[0].id)
      } else {
        // 项目没有对话：清空对话区，绝不显示上一个项目的内容
        chat.clear()
      }
    },

    async selectConv(cid) {
      this.currentConv = cid
      // 刷新项目目录（对话可能带独立 project_dir）
      const p = this.tree.find((t) => t.project === this.currentProject)
      if (p) this.projectDir = p.project_dir || ''
      // 切换对话时同步加载消息与进度，并恢复仍在运行的流
      const chat = useChatStore()
      await chat.loadMessages(this.currentProject, cid)
      await chat.resumeIfStreaming(cid)
      await chat.refreshWorkflow(cid)
    },

    async createProject(name) {
      const t = useToast()
      if (!name?.trim()) { t.error('请输入项目名称'); return }
      // 项目目录由后端按用户自动分配，前端不再传路径
      const r = await api.post('/api/projects', { name: name.trim() })
      if (!r.ok) { t.error(r.message); return }
      await this.bootstrap()
      await this.selectProject(name.trim())
      t.success(r.message)
    },

    async renameProject(pid, newName) {
      const t = useToast()
      if (!newName?.trim()) { t.error('请输入新的项目名称'); return }
      if (newName.trim() === pid) return
      const r = await api.post(`/api/projects/${encodeURIComponent(pid)}/rename`, { name: newName.trim() })
      if (!r.ok) { t.error(r.message); return }
      const wasCurrent = pid === this.currentProject
      await this.bootstrap()
      if (wasCurrent) {
        this.currentProject = newName.trim()
        const p = this.tree.find((x) => x.project === newName.trim())
        this.projectDir = p?.project_dir || ''
      }
      t.success(r.message)
    },

    async deleteProject(pid) {
      const t = useToast()
      const r = await api.del(`/api/projects/${encodeURIComponent(pid)}`)
      if (!r.ok) { t.error(r.message); return }
      if (pid === this.currentProject) {
        this.currentProject = ''
        this.currentConv = ''
        this.projectDir = ''
        useChatStore().clear()
      }
      await this.bootstrap()
      t.success(r.message)
    },

    async createConv(title, pid) {
      const t = useToast()
      const target = pid || this.currentProject
      if (!target) { t.error('请先选择项目'); return }
      const r = await api.post('/api/conversations', { project: target, title: (title || '').trim() || '新对话' })
      if (!r.ok) { t.error(r.message); return }
      await this.bootstrap()
      this.currentProject = target
      await this.selectConv(r.conv_id)
      t.success(r.message)
    },

    async deleteConv(cid, pid = '') {
      const t = useToast()
      const target = pid || this.currentProject
      const r = await api.del(`/api/conversations/${encodeURIComponent(cid)}?project=${encodeURIComponent(target)}`)
      if (!r.ok) { t.error(r.message); return }
      await this.bootstrap()
      if (cid === this.currentConv) {
        this.currentConv = ''
        useChatStore().clear()
      }
      t.success(r.message)
    },

    async saveProjectDir(path) {
      const t = useToast()
      if (!this.currentProject) { t.error('请先选择项目'); return }
      const r = await api.post(`/api/project/${encodeURIComponent(this.currentProject)}/dir`, { path })
      if (!r.ok) { t.error(r.message); return }
      this.projectDir = r.path
      const p = this.tree.find((x) => x.project === this.currentProject)
      if (p) p.project_dir = r.path
      t.success(r.message)
    },

    async uploadStudyArea(fileList) {
      const t = useToast()
      if (!fileList || !fileList.length) { t.error('请选择文件'); return }
      const r = await api.uploadStudyArea([...fileList])
      this.studyAreas = r.study_areas || []
      this.lastValidation = r.validations || []
      // 首次上传后自动把最新文件设为当前研究区（保持原「取最新」行为）
      if (this.studyAreas.length && !this.currentStudyArea) {
        const rr = await api.setCurrentStudyArea(this.studyAreas[0])
        if (rr.ok) this.currentStudyArea = rr.current || ''
      }
      // 验证结果只在研究区面板内展示（与「测试」页同款状态行），toast 仅给简洁摘要
      const bad = (r.validations || []).filter((v) => v.level !== 'ok')
      if (bad.length) {
        t.info(`上传完成：${bad.length} 个研究区文件未通过验证，详见研究区面板`)
      } else {
        t.success('研究区上传完成')
      }
    },

    async setCurrentStudyArea(name) {
      const t = useToast()
      if (!name) { t.error('未指定研究区'); return }
      const r = await api.setCurrentStudyArea(name)
      if (!r.ok) { t.error(r.message); return }
      this.currentStudyArea = r.current || ''
      t.success(r.message)
    },

    async deleteStudyArea(name) {
      const t = useToast()
      const r = await api.deleteStudyArea(name)
      if (!r.ok) { t.error(r.message); return }
      this.studyAreas = r.study_areas || []
      this.currentStudyArea = r.current || ''
      t.success(r.message)
    },
  },
})
