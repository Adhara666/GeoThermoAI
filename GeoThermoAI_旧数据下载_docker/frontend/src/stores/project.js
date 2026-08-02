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
      const convs = p?.conversations || []
      if (convs.length) await this.selectConv(convs[0].id)
    },

    async selectConv(cid) {
      this.currentConv = cid
      // 刷新项目目录（对话可能带独立 project_dir）
      const p = this.tree.find((t) => t.project === this.currentProject)
      if (p) this.projectDir = p.project_dir || ''
    },

    async createProject(name, dir = '') {
      const t = useToast()
      if (!name?.trim()) { t.error('请输入项目名称'); return }
      const r = await api.post('/api/projects', { name: name.trim(), path: dir || '' })
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
      t.success(r.message)
    },
  },
})
