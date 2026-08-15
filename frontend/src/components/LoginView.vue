<script setup>
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useToast } from '../composables/useToast'
import { t } from '../i18n'
import LangSwitch from './LangSwitch.vue'

const emit = defineEmits(['authed'])
const auth = useAuthStore()
const toast = useToast()

const mode = ref('login') // 'login' | 'register'
const username = ref('')
const password = ref('')
const nickname = ref('')
const busy = ref(false)
// 密码明文/密文切换（浏览器原生密码可见控件在部分环境不显示，代码内置保证处处可用）
const showPwd = ref(false)

async function submit() {
  if (busy.value) return
  const u = username.value.trim()
  const p = password.value
  if (!u) { toast.error(t('login.errEmptyUser')); return }
  if (!p) { toast.error(t('login.errEmptyPwd')); return }
  busy.value = true
  try {
    if (mode.value === 'login') {
      await auth.login(u, p)
    } else {
      if (p.length < 6) { toast.error(t('login.errPwdLen')); return }
      await auth.register(u, p, nickname.value.trim())
    }
    emit('authed')
  } catch (e) {
    toast.error(e.message || t('login.errFailed'))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-lang"><LangSwitch /></div>
    <div class="login-card">
      <div class="login-brand">
        <img src="/logo.png?v=2" alt="GeoThermoAI" onerror="this.style.display='none'" />
        <div class="login-title">GeoThermoAI</div>
        <div class="login-sub">{{ t('login.sub') }}</div>
      </div>

      <div class="login-tabs">
        <button class="login-tab" :class="{ 'login-tab--active': mode === 'login' }" @click="mode = 'login'">{{ t('login.tabLogin') }}</button>
        <button class="login-tab" :class="{ 'login-tab--active': mode === 'register' }" @click="mode = 'register'">{{ t('login.tabRegister') }}</button>
      </div>

      <form class="login-form" @submit.prevent="submit">
        <div class="form-group">
          <label>{{ t('login.username') }}</label>
          <input v-model="username" class="form-input" :placeholder="t('login.usernamePh')" autofocus />
        </div>
        <div v-if="mode === 'register'" class="form-group">
          <label>{{ t('login.nickname') }}</label>
          <input v-model="nickname" class="form-input" :placeholder="t('login.nicknamePh')" maxlength="32" />
        </div>
        <div class="form-group">
          <label>{{ t('login.password') }}</label>
          <div class="pwd-wrap">
            <input
              v-model="password"
              :type="showPwd ? 'text' : 'password'"
              class="form-input pwd-input"
              :placeholder="mode === 'register' ? t('login.pwdRegPh') : t('login.pwdPh')"
            />
            <button
              type="button"
              class="pwd-toggle"
              :title="showPwd ? t('login.hidePwd') : t('login.showPwd')"
              @click="showPwd = !showPwd"
            >
              <svg v-if="showPwd" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
              <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            </button>
          </div>
        </div>
        <button class="btn btn--primary btn--block" :disabled="busy" type="submit">
          {{ busy ? t('login.pleaseWait') : mode === 'login' ? t('login.loginBtn') : t('login.regBtn') }}
        </button>
      </form>

      <p class="login-foot">
        {{ mode === 'login' ? t('login.noAccount') : t('login.hasAccount') }}
        <a class="login-link" @click="mode = mode === 'login' ? 'register' : 'login'">
          {{ mode === 'login' ? t('login.goRegister') : t('login.goLogin') }}
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
.login-lang { position: absolute; top: 14px; left: 16px; z-index: 10; }
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
.pwd-wrap { position: relative; }
.pwd-input { padding-right: 34px; }
.pwd-toggle {
  position: absolute; top: 50%; right: 4px; transform: translateY(-50%);
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; padding: 0; border: none; background: none;
  color: var(--text-muted); cursor: pointer; transition: color 0.15s;
}
.pwd-toggle:hover { color: var(--primary); }
</style>
