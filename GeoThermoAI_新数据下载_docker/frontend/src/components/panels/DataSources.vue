<script setup>
import { ref, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'

const settings = useSettingsStore()

// Copernicus Data Space 配置
const username = ref('')
const password = ref('')
const clientId = ref('')
const clientSecret = ref('')
const s3Key = ref('')
const s3Secret = ref('')
const saved = ref(false)
const saving = ref(false)

watch(
  () => settings.settings,
  (s) => {
    if (!s) return
    const ds = s.data_space || {}
    username.value = ds.username || ''
    password.value = ds.password || ''
    clientId.value = ds.client_id || ''
    clientSecret.value = ds.client_secret || ''
    s3Key.value = ds.s3_key || ''
    s3Secret.value = ds.s3_secret || ''
  },
  { immediate: true },
)

async function save() {
  saving.value = true
  try {
    const ok = await settings.saveSettings({
      data_space: {
        username: username.value.trim(),
        password: password.value.trim(),
        client_id: clientId.value.trim(),
        client_secret: clientSecret.value.trim(),
        s3_key: s3Key.value.trim(),
        s3_secret: s3Secret.value.trim(),
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
      <input v-model="password" type="password" class="form-input" placeholder="哥白尼数据空间登录密码" />
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
          <input v-model="clientSecret" type="password" class="form-input" placeholder="无账号时用于搜索影像" />
        </div>
        <div class="form-group">
          <label>S3 Access Key</label>
          <input v-model="s3Key" class="form-input" placeholder="DEM 走 CDSE 必需；无账号时亦用于下载" />
        </div>
        <div class="form-group">
          <label>S3 Secret Key</label>
          <input v-model="s3Secret" type="password" class="form-input" placeholder="与 S3 Access Key 配套" />
        </div>
      </div>
    </details>

    <button class="btn btn--primary btn--block" :disabled="saving" @click="save">
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
.ds-src__name { font-weight: 600; }
.ds-src__from { color: var(--primary); }
.ds-src__fb { color: var(--text-muted); font-size: 12px; }
</style>
