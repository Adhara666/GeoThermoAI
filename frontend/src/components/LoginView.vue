<script setup>
import { onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useToast } from '../composables/useToast'
import { api } from '../api'
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

// ── 魔搭（ModelScope）OAuth 登录（新增入口，原有登录/注册不变） ──
const msBusy = ref(false)

/** 计算 OAuth 回调地址：
 *  - 优先构建期覆盖（创空间部署可设 VITE_PUBLIC_ORIGIN）
 *  - 魔搭 Studio 是 iframe 嵌入，location.origin 为 modelscope.cn 不可用于回调，
 *    从 /studios/{owner}/{name} 自动推导 host 地址 https://{owner}-{name}.ms.show/
 *  - 其余场景用当前 origin
 */
function computeRedirectUri() {
  const env = String(import.meta.env.VITE_PUBLIC_ORIGIN || '').trim()
  if (env) return env.replace(/\/+$/, '') + '/'
  const host = window.location.hostname
  if (host === 'modelscope.cn' || host.endsWith('.modelscope.cn')) {
    const m = window.location.pathname.match(/^\/studios\/([^/]+)\/([^/]+)/)
    if (m) return `https://${(m[1] + '-' + m[2]).toLowerCase()}.ms.show/`
  }
  return window.location.origin + '/'
}

function inIframe() {
  try { return window.self !== window.top } catch (_) { return true }
}

async function loginWithModelScope() {
  if (msBusy.value) return
  msBusy.value = true
  try {
    // 回调地址用前端真实可达地址（本地为 http://localhost:7860/，
    // 需与魔搭「互联应用」登记的回调地址一致）
    const redirect = computeRedirectUri()
    const r = await api.modelscopeAuthUrl(redirect)
    if (!r || !r.ok) {
      toast.error(r?.message || t('login.msUnavailable'))
      return
    }
    try { sessionStorage.setItem('ms_oauth_redirect', redirect) } catch (_) {}
    // Studio iframe 内：授权页可能禁止被嵌入，改开新窗口
    if (inIframe()) {
      window.open(r.url, '_blank')
    } else {
      window.location.href = r.url
    }
  } catch (e) {
    toast.error(e.message || t('login.msFailed'))
  } finally {
    msBusy.value = false
  }
}

// 从魔搭授权页返回：URL 携带 ?code=&state= 时自动完成登录
onMounted(async () => {
  const q = new URLSearchParams(window.location.search)
  const code = q.get('code')
  if (!code) return
  const state = q.get('state') || ''
  let redirect = computeRedirectUri()
  try { redirect = sessionStorage.getItem('ms_oauth_redirect') || redirect } catch (_) {}
  msBusy.value = true
  try {
    const r = await api.modelscopeCallback({ code, state, redirect_uri: redirect })
    if (r && r.ok) {
      auth.adoptSession(r.token, r.user)
      toast.success(t('login.msSuccess', { name: r.user?.nickname || r.user?.username || '' }))
      emit('authed')
    } else {
      toast.error(r?.message || t('login.msFailed'))
    }
  } catch (e) {
    toast.error(e.message || t('login.msFailed'))
  } finally {
    msBusy.value = false
    window.history.replaceState({}, '', window.location.pathname)
    try { sessionStorage.removeItem('ms_oauth_redirect') } catch (_) {}
  }
})
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

      <div class="login-divider"><span>{{ t('login.msOr') }}</span></div>
      <button
        class="btn btn--block ms-login-btn"
        type="button"
        :disabled="msBusy"
        @click="loginWithModelScope"
      >
        <img class="ms-login-btn__logo" src="/modelscope-logo.ico" width="16" height="16" alt="" />
        {{ msBusy ? t('login.msLoading') : t('login.msBtn') }}
      </button>

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
.login-divider {
  display: flex; align-items: center; gap: 10px;
  margin: 14px 0 12px; color: var(--text-muted); font-size: 12px;
}
.login-divider::before, .login-divider::after {
  content: ''; flex: 1; height: 1px; background: var(--border);
}
.ms-login-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  background: #fff; color: var(--text); border: 1px solid var(--border-strong);
}
.ms-login-btn__logo { border-radius: 3px; flex-shrink: 0; }
.ms-login-btn:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
</style>
