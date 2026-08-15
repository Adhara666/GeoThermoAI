import { defineStore } from 'pinia'
import { api } from '../api'
import { useToast } from '../composables/useToast'
import { trServer } from '../i18n'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    settings: null,
    modelParams: {},
  }),

  getters: {
    displayName: (s) => s.settings?.display_name || s.settings?.model_id || '',
    // 凭据不回传明文：以 has_api_key 判断是否已配置
    configured: (s) => !!(s.settings && (s.settings.has_api_key || s.settings.base_url)),
  },

  actions: {
    async load() {
      try {
        this.settings = await api.get('/api/settings')
      } catch (_) {
        this.settings = {}
      }
      try {
        this.modelParams = await api.get('/api/model-params')
      } catch (_) {
        this.modelParams = {}
      }
    },

    async saveSettings(payload) {
      const t = useToast()
      const r = await api.post('/api/settings', payload)
      if (!r.ok) { t.error(trServer(r.message)); return false }
      // 重新拉取掩码状态，黑点长度与最新密钥一致
      try { this.settings = await api.get('/api/settings') } catch (_) {}
      t.success(trServer(r.message))
      return true
    },

    async saveModelParams(params) {
      const t = useToast()
      const r = await api.post('/api/model-params', params)
      if (!r.ok) { t.error(trServer(r.message)); return false }
      this.modelParams = { ...params }
      t.success(trServer(r.message))
      return true
    },
  },
})
