<script setup>
import { ref, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'

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
    <p class="form-hint">数据源：Microsoft Planetary Computer（Landsat 8/9 L2、Sentinel-2 L2A、Copernicus DEM）＋ Copernicus Data Space（Sentinel-2 L2A、DEM，优先使用，国内访问更快），通过 STAC API 自动搜索下载</p>
    <p class="form-hint">各数据自动从数据源检索下载，无需手动上传原始影像</p>

    <div class="ds-srcs">
      <div class="ds-src">
        <span class="ds-src__name">Sentinel-2 L2A（多光谱 + SCL）</span>
        <span class="ds-src__from">Copernicus Data Space（优先）</span>
        <span class="ds-src__fb">失败回退微软行星计算机</span>
      </div>
      <div class="ds-src">
        <span class="ds-src__name">Landsat 8/9 L2（LST + QA）</span>
        <span class="ds-src__from">微软行星计算机</span>
      </div>
      <div class="ds-src">
        <span class="ds-src__name">DEM（Copernicus GLO-30）</span>
        <span class="ds-src__from">Copernicus Data Space（优先）</span>
        <span class="ds-src__fb">需配置 S3 密钥，否则用微软行星计算机</span>
      </div>
    </div>

    <div class="form-group" style="margin-top:14px">
      <label>Copernicus Data Space 账号</label>
      <input v-model="username" class="form-input" placeholder="注册邮箱（dataspace.copernicus.eu）" />
    </div>
    <div class="form-group">
      <label>密码</label>
      <input v-model="password" type="password" class="form-input" placeholder="哥白尼数据空间登录密码；已配置时显示为黑点" />
    </div>

    <details class="advanced" style="margin-bottom:12px">
      <summary style="cursor:pointer;font-size:13px;color:var(--text-secondary)">高级配置（可选，非必需）</summary>
      <div style="margin-top:10px">
        <div class="form-group">
          <label>OAuth2 Client ID</label>
          <input v-model="clientId" class="form-input" placeholder="无账号时用于搜索影像" />
        </div>
        <div class="form-group">
          <label>OAuth2 Client Secret</label>
          <input v-model="clientSecret" type="password" class="form-input" placeholder="无账号时用于搜索影像；已配置时显示为黑点" />
        </div>
        <div class="form-group">
          <label>S3 Access Key</label>
          <input v-model="s3Key" class="form-input" placeholder="DEM 走 CDSE 必需；无账号时亦用于下载" />
        </div>
        <div class="form-group">
          <label>S3 Secret Key</label>
          <input v-model="s3Secret" type="password" class="form-input" placeholder="与 S3 Access Key 配套；已配置时显示为黑点" />
        </div>
      </div>
    </details>

    <button class="btn btn--primary btn--block" :disabled="saving" @click="save">
      <svg v-if="!saving" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
      {{ saving ? '保存中…' : '保存数据源配置' }}
    </button>
    <p v-if="saved" class="form-hint" style="margin-top:8px;color:var(--success)">✅ 已保存并应用</p>
    <p class="form-hint" style="margin-top:10px">
      Sentinel-2 填写账号密码即可优先走 Copernicus Data Space（国内更快）；DEM 走 CDSE 需额外配置 S3 密钥；未配置或下载失败时自动回退微软行星计算机
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
