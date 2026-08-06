import { defineStore } from 'pinia'
import { api, getToken, setToken } from '../api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null, // {uid, username, nickname}
    ready: false,
  }),

  getters: {
    authed: (s) => !!s.user,
    display: (s) => s.user?.nickname || s.user?.username || '',
  },

  actions: {
    /** 应用启动时恢复登录态 */
    async init() {
      this.ready = true
      if (!getToken()) return
      try {
        const r = await api.me()
        if (r && r.ok) this.user = r.user
      } catch (_) {
        this.user = null
      }
    },

    async login(username, password) {
      const r = await api.login(username, password)
      if (!r || !r.ok) throw new Error((r && r.message) || '登录失败')
      setToken(r.token)
      this.user = r.user
    },

    async register(username, password, nickname) {
      const r = await api.register(username, password, nickname)
      if (!r || !r.ok) throw new Error((r && r.message) || '注册失败')
      // 注册成功后自动登录
      await this.login(username, password)
    },

    logout() {
      setToken('')
      this.user = null
    },
  },
})
