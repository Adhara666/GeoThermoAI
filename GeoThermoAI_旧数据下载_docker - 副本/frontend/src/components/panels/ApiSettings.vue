<script setup>
import { ref, computed, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'

const settings = useSettingsStore()
const fmt = ref('OpenAI Chat Completions 格式')
const baseUrl = ref('')
const apiKey = ref('')
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
    apiKey.value = s.api_key || ''
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
    api_key: apiKey.value,
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
      <input v-model="apiKey" type="password" class="form-input" placeholder="输入 API 密钥（保存后生效）" />
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
    <button class="btn btn--primary btn--block" @click="save">保存并应用</button>
    <p v-if="saved" class="form-hint" style="margin-top:8px;color:var(--success)">✅ 已保存并热更新模型</p>
  </div>
</template>
