<script setup>
import { ref, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'
import { t } from '../../i18n'

const settings = useSettingsStore()

// Copernicus Data Space 配置
// 秘密字段已配置时按真实长度显示黑点（不暴露明文），重新输入才替换原值
const username = ref('')
const password = ref('')
const clientId = ref('')
const clientSecret = ref('')
const s3Key = ref('')
const s3Secret = ref('')
const saved = ref(false)
const saving = ref(false)

/** 仍为黑点占位（未重新输入）→ 传空，后端保持原值 */
const stripMask = (v) => (v && !v.startsWith('•') ? v.trim() : '')

watch(
  () => settings.settings,
  (s) => {
    if (!s) return
    const ds = s.data_space || {}
    username.value = ds.username || ''
    clientId.value = ds.client_id || ''
    s3Key.value = ds.s3_key || ''
    password.value = ds.password_len ? '•'.repeat(ds.password_len) : ''
    clientSecret.value = ds.client_secret_len ? '•'.repeat(ds.client_secret_len) : ''
    s3Secret.value = ds.s3_secret_len ? '•'.repeat(ds.s3_secret_len) : ''
  },
  { immediate: true },
)

async function save() {
  saving.value = true
  try {
    const ok = await settings.saveSettings({
      data_space: {
        username: username.value.trim(),
        password: stripMask(password.value),
        client_id: clientId.value.trim(),
        client_secret: stripMask(clientSecret.value),
        s3_key: s3Key.value.trim(),
        s3_secret: stripMask(s3Secret.value),
      },
    })
    saved.value = ok
    if (ok) setTimeout(() => (saved.value = false), 2500)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div>
    <p class="form-hint">{{ t('ds.hintTop') }}</p>
    <p class="form-hint">{{ t('ds.hintAuto') }}</p>

    <div class="ds-srcs">
      <div class="ds-src">
        <span class="ds-src__name">{{ t('ds.sentinel') }}</span>
        <span class="ds-src__from">{{ t('ds.cdsePref') }}</span>
        <span class="ds-src__fb">{{ t('ds.fbPc') }}</span>
      </div>
      <div class="ds-src">
        <span class="ds-src__name">{{ t('ds.landsat') }}</span>
        <span class="ds-src__from">{{ t('ds.msPc') }}</span>
      </div>
      <div class="ds-src">
        <span class="ds-src__name">{{ t('ds.dem') }}</span>
        <span class="ds-src__from">{{ t('ds.cdsePref') }}</span>
        <span class="ds-src__fb">{{ t('ds.s3Need') }}</span>
      </div>
    </div>

    <div class="form-group" style="margin-top:14px">
      <label>{{ t('ds.account') }}</label>
      <input v-model="username" class="form-input" :placeholder="t('ds.accountPh')" />
    </div>
    <div class="form-group">
      <label>{{ t('ds.password') }}</label>
      <input v-model="password" type="password" class="form-input" :placeholder="t('ds.passwordPh')" />
    </div>

    <details class="advanced" style="margin-bottom:12px">
      <summary style="cursor:pointer;font-size:13px;color:var(--text-secondary)">{{ t('ds.adv') }}</summary>
      <div style="margin-top:10px">
        <div class="form-group">
          <label>OAuth2 Client ID</label>
          <input v-model="clientId" class="form-input" :placeholder="t('ds.clientIdPh')" />
        </div>
        <div class="form-group">
          <label>OAuth2 Client Secret</label>
          <input v-model="clientSecret" type="password" class="form-input" :placeholder="t('ds.clientSecretPh')" />
        </div>
        <div class="form-group">
          <label>S3 Access Key</label>
          <input v-model="s3Key" class="form-input" :placeholder="t('ds.s3KeyPh')" />
        </div>
        <div class="form-group">
          <label>S3 Secret Key</label>
          <input v-model="s3Secret" type="password" class="form-input" :placeholder="t('ds.s3SecretPh')" />
        </div>
      </div>
    </details>

    <button class="btn btn--primary btn--block" :disabled="saving" @click="save">
      <svg v-if="!saving" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
      {{ saving ? t('ds.saving') : t('ds.saveBtn') }}
    </button>
    <p v-if="saved" class="form-hint" style="margin-top:8px;color:var(--success)">{{ t('ds.saved') }}</p>
    <p class="form-hint" style="margin-top:10px">
      {{ t('ds.hintBottom') }}
    </p>
  </div>
</template>

<style scoped>
.ds-srcs { display: flex; flex-direction: column; gap: 8px; margin-top: 5px; }
.ds-src {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 10px; border: 1px solid var(--border); border-radius: var(--radius-sm);
  font-size: 13px; background: var(--bg-panel);
}
.ds-src__name { font-weight: 400; color: var(--text); }
.ds-src__from { color: var(--primary); }
.ds-src__fb { color: var(--text-muted); font-size: 12px; }
</style>
