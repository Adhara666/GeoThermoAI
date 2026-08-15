<script setup>
// 界面语言切换按钮：造型/行为参考 ChatModeSelect（Work/Chat 选择按钮）。
// 点击展开下拉（第一个中文，第二个 English）；点击项切换全局语言并持久化。
// 登录页左上角与主界面侧栏顶栏各放一个，读取同一全局 lang，双向同步。
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { lang, setLang, t } from '../i18n'

const open = ref(false)
const root = ref(null)

const OPTIONS = [
  { id: 'zh', label: t('lang.zhOption') },
  { id: 'en', label: t('lang.enOption') },
]

function toggle() {
  open.value = !open.value
}

function pick(id) {
  setLang(id)
  open.value = false
}

function onDocClick(e) {
  if (root.value && !root.value.contains(e.target)) open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div ref="root" class="lang-switch">
    <button
      class="lang-switch__btn"
      type="button"
      :title="t('lang.buttonTitle')"
      @click.stop="toggle"
    >
      <span class="lang-switch__label">{{ t('lang.label') }}</span>
      <svg class="lang-switch__caret" :class="{ 'lang-switch__caret--open': open }"
           width="12" height="12" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2">
        <polyline points="18 15 12 9 6 15" />
      </svg>
    </button>

    <div v-if="open" class="lang-switch__panel">
      <button
        v-for="o in OPTIONS"
        :key="o.id"
        type="button"
        class="lang-switch__item"
        :class="{ 'lang-switch__item--active': lang === o.id }"
        @click.stop="pick(o.id)"
      >
        <span class="lang-switch__item-title">{{ o.label }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.lang-switch { position: relative; flex-shrink: 0; }
.lang-switch__btn {
  display: inline-flex; align-items: center; gap: 4px; height: 30px; padding: 0 10px;
  border: 1px solid var(--border-strong); border-radius: 10px; background: var(--bg-panel);
  color: var(--text); font-size: 12px; white-space: nowrap; transition: all 0.15s;
  cursor: pointer;
}
.lang-switch__btn:hover { border-color: var(--primary); color: var(--primary); }
.lang-switch__label { font-weight: 700; }
.lang-switch__caret { transition: transform 0.15s; }
.lang-switch__caret--open { transform: rotate(180deg); }
.lang-switch__panel {
  position: absolute; top: calc(100% + 6px); left: 0; z-index: 40; min-width: 140px;
  display: flex; flex-direction: column; padding: 4px;
  background: var(--bg-panel); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: var(--shadow-lg);
}
.lang-switch__item {
  display: flex; align-items: center; padding: 8px 10px; border: none; background: none;
  border-radius: var(--radius-sm); text-align: left; cursor: pointer;
}
.lang-switch__item:hover { background: var(--bg); }
.lang-switch__item--active { background: var(--primary-soft); }
.lang-switch__item-title { font-size: 13px; color: var(--text); font-weight: 500; }
</style>
