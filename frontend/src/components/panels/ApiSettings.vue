<script setup>
import { ref, computed, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'
import { t } from '../../i18n'

const settings = useSettingsStore()
// fmt 用稳定 id（openai|anthropic），展示文案由 t() 按语言输出，
// 避免语言切换后 select 的值与选项文本失配
const fmt = ref('openai')
const baseUrl = ref('')
const apiKey = ref('')
const hasKey = ref(false)
const modelId = ref('')
const displayName = ref('')
const ctxIn = ref(128000)
const ctxOut = ref(16000)
const saved = ref(false)

watch(
  () => settings.settings,
  (s) => {
    if (!s) return
    fmt.value = s.api_format === 'anthropic' ? 'anthropic' : 'openai'
    baseUrl.value = s.base_url || ''
    // 凭据不回传明文：已配置时按真实长度显示黑点；重新输入才替换原 Key
    hasKey.value = !!s.has_api_key
    apiKey.value = s.api_key_len ? '•'.repeat(s.api_key_len) : ''
    modelId.value = s.model_id || ''
    displayName.value = s.display_name || ''
    ctxIn.value = s.context_input ?? 128000
    ctxOut.value = s.context_output ?? 16000
  },
  { immediate: true },
)

const urlHint = computed(() =>
  fmt.value === 'anthropic'
    ? t('api.urlHintAnthropic')
    : t('api.urlHintOpenai'),
)

async function save() {
  const ok = await settings.saveSettings({
    api_format: fmt.value,
    base_url: baseUrl.value,
    // 仍是黑点占位（未重新输入）→ 传空，后端保持原 Key
    api_key: apiKey.value && !apiKey.value.startsWith('•') ? apiKey.value : '',
    model_id: modelId.value,
    display_name: displayName.value,
    context_input: Number(ctxIn.value) || 0,
    context_output: Number(ctxOut.value) || 0,
  })
  saved.value = ok
  if (ok) setTimeout(() => (saved.value = false), 2500)
}
</script>

<template>
  <div>
    <div class="form-group">
      <label>{{ t('api.fmt') }}</label>
      <select v-model="fmt" class="form-select">
        <option value="openai">{{ t('api.fmtOpenai') }}</option>
        <option value="anthropic">{{ t('api.fmtAnthropic') }}</option>
      </select>
    </div>
    <div class="form-group">
      <label>{{ t('api.baseUrl') }}</label>
      <input v-model="baseUrl" class="form-input" placeholder="e.g. https://api.deepseek.com" />
      <p class="form-hint">{{ urlHint }}</p>
    </div>
    <div class="form-group">
      <label>{{ t('api.modelId') }}</label>
      <input v-model="modelId" class="form-input" placeholder="e.g. deepseek-chat" />
    </div>
    <div class="form-group">
      <label>{{ t('api.apiKey') }}</label>
      <input v-model="apiKey" type="password" class="form-input" :placeholder="t('api.apiKeyPh')" />
    </div>
    <details class="advanced" style="margin-bottom:12px">
      <summary style="cursor:pointer;font-size:13px;color:var(--text-secondary)">{{ t('api.advanced') }}</summary>
      <div style="margin-top:10px">
        <div class="form-group">
          <label>{{ t('api.displayName') }}</label>
          <input v-model="displayName" class="form-input" :placeholder="t('api.displayNamePh')" maxlength="32" />
        </div>
        <div style="display:flex;gap:8px">
          <div class="form-group" style="flex:1">
            <label>{{ t('api.ctxIn') }}</label>
            <input v-model="ctxIn" type="number" class="form-input" />
          </div>
          <div class="form-group" style="flex:1">
            <label>{{ t('api.ctxOut') }}</label>
            <input v-model="ctxOut" type="number" class="form-input" />
          </div>
        </div>
      </div>
    </details>
    <button class="btn btn--primary btn--block" @click="save">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
      {{ t('api.saveApply') }}
    </button>
    <p v-if="saved" class="form-hint" style="margin-top:8px;color:var(--success)">{{ t('api.savedHot') }}</p>
  </div>
</template>
