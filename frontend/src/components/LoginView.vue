<script setup>
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useToast } from '../composables/useToast'

const emit = defineEmits(['authed'])
const auth = useAuthStore()
const toast = useToast()

const mode = ref('login') // 'login' | 'register'
const username = ref('')
const password = ref('')
const nickname = ref('')
const busy = ref(false)

async function submit() {
  if (busy.value) return
  const u = username.value.trim()
  const p = password.value
  if (!u) { toast.error('请输入账号名'); return }
  if (!p) { toast.error('请输入密码'); return }
  busy.value = true
  try {
    if (mode.value === 'login') {
      await auth.login(u, p)
    } else {
      if (p.length < 6) { toast.error('密码至少 6 位'); return }
      await auth.register(u, p, nickname.value.trim())
    }
    emit('authed')
  } catch (e) {
    toast.error(e.message || '操作失败')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-brand">
        <img src="/logo.png?v=2" alt="GeoThermoAI" onerror="this.style.display='none'" />
        <div class="login-title">GeoThermoAI</div>
        <div class="login-sub">高分辨率地表温度智能重建系统</div>
      </div>

      <div class="login-tabs">
        <button class="login-tab" :class="{ 'login-tab--active': mode === 'login' }" @click="mode = 'login'">登录</button>
        <button class="login-tab" :class="{ 'login-tab--active': mode === 'register' }" @click="mode = 'register'">注册</button>
      </div>

      <form class="login-form" @submit.prevent="submit">
        <div class="form-group">
          <label>账号名</label>
          <input v-model="username" class="form-input" placeholder="仅字母/数字/_/-，2-32 位" autofocus />
        </div>
        <div v-if="mode === 'register'" class="form-group">
          <label>昵称（可选，用于展示）</label>
          <input v-model="nickname" class="form-input" placeholder="留空则使用账号名" maxlength="32" />
        </div>
        <div class="form-group">
          <label>密码</label>
          <input v-model="password" type="password" class="form-input" :placeholder="mode === 'register' ? '至少 6 位，支持大小写字母、数字与符号' : '输入密码'" />
        </div>
        <button class="btn btn--primary btn--block" :disabled="busy" type="submit">
          {{ busy ? '请稍候…' : mode === 'login' ? '登 录' : '注册并登录' }}
        </button>
      </form>

      <p class="login-foot">
        {{ mode === 'login' ? '还没有账号？' : '已有账号？' }}
        <a class="login-link" @click="mode = mode === 'login' ? 'register' : 'login'">
          {{ mode === 'login' ? '立即注册' : '去登录' }}
        </a>
      </p>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  width: 100%;
  height: var(--app-height, 100%);
  max-height: var(--app-height, 100%);
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, #f6f8fb 0%, #e9eef6 100%);
  padding: 16px;
  overflow: auto;
}
.login-card {
  width: 380px;
  max-width: 100%;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow);
  padding: 28px 30px 22px;
}
.login-brand { text-align: center; margin-bottom: 18px; }
.login-brand img { width: 64px; height: 64px; object-fit: contain; margin-bottom: 8px; }
.login-title { font-size: 22px; font-weight: 700; color: var(--text); }
.login-sub { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.login-tabs { display: flex; gap: 4px; background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 3px; margin-bottom: 16px; }
.login-tab { flex: 1; border: none; background: none; padding: 8px 0; font-size: 14px; color: var(--text-secondary); border-radius: 8px; cursor: pointer; }
.login-tab--active { background: var(--primary); color: #fff; font-weight: 600; }
.login-form { display: flex; flex-direction: column; gap: 12px; }
.login-foot { text-align: center; font-size: 13px; color: var(--text-secondary); margin-top: 14px; }
.login-link { color: var(--primary); cursor: pointer; }
</style>
