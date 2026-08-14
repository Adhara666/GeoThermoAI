<script setup>
import { ref, computed, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'

const settings = useSettingsStore()
const fmt = ref('OpenAI Chat Completions 格式')
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
    fmt.value = s.api_format === 'anthropic' ? 'Anthropic Messages 格式' : 'OpenAI Chat Completions 格式'
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
  fmt.value.includes('Anthropic')
    ? '请填写 Claude API 地址，/v1/messages 会自动补到末尾'
    : '请填写兼容 OpenAI 的地址，/chat/completions 会自动补到末尾',
)

async function save() {
  const ok = await settings.saveSettings({
    api_format: fmt.value.includes('Anthropic') ? 'anthropic' : 'openai',
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
      <label>API 格式</label>
      <select v-model="fmt" class="form-select">
        <option>OpenAI Chat Completions 格式</option>
        <option>Anthropic Messages 格式</option>
      </select>
    </div>
    <div class="form-group">
      <label>请求地址</label>
      <input v-model="baseUrl" class="form-input" placeholder="e.g. https://api.deepseek.com" />
      <p class="form-hint">{{ urlHint }}</p>
    </div>
    <div class="form-group">
      <label>模型 ID</label>
      <input v-model="modelId" class="form-input" placeholder="e.g. deepseek-chat" />
    </div>
    <div class="form-group">
      <label>API 密钥</label>
      <input v-model="apiKey" type="password" class="form-input" placeholder="输入 API 密钥（保存后生效）；已配置时显示为黑点" />
    </div>
    <details class="advanced" style="margin-bottom:12px">
      <summary style="cursor:pointer;font-size:13px;color:var(--text-secondary)">高级配置</summary>
      <div style="margin-top:10px">
        <div class="form-group">
          <label>模型展示名称</label>
          <input v-model="displayName" class="form-input" placeholder="留空则用模型 ID" maxlength="32" />
        </div>
        <div style="display:flex;gap:8px">
          <div class="form-group" style="flex:1">
            <label>上下文-输入</label>
            <input v-model="ctxIn" type="number" class="form-input" />
          </div>
          <div class="form-group" style="flex:1">
            <label>上下文-输出</label>
            <input v-model="ctxOut" type="number" class="form-input" />
          </div>
        </div>
      </div>
    </details>
    <button class="btn btn--primary btn--block" @click="save">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
      保存并应用
    </button>
    <p v-if="saved" class="form-hint" style="margin-top:8px;color:var(--success)">已保存并热更新模型</p>
  </div>
</template>
