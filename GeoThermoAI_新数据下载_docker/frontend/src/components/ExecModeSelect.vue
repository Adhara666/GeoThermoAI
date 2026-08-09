<script setup>
// 执行模式上拉框（技术方案 9.1）：位于发送键左边，向上弹出选项面板。
// 选择结果写入 chat store 的 execMode 并持久化到 localStorage，每次发送随请求带上。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const open = ref(false)
const root = ref(null)

const MODES = [
  { id: 'approval', label: '由我批准', hint: '关键节点会停下来问你' },
  { id: 'auto', label: '完全执行', hint: '一次跑完，不打断' },
]

const current = computed(() => MODES.find((m) => m.id === chat.execMode) || MODES[0])

function toggle() {
  if (chat.streaming) return
  open.value = !open.value
}

function pick(id) {
  chat.setExecMode(id)
  open.value = false
}

function onDocClick(e) {
  if (root.value && !root.value.contains(e.target)) open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div ref="root" class="exec-mode">
    <button
      class="exec-mode__btn"
      type="button"
      :disabled="chat.streaming"
      :title="current.hint"
      @click.stop="toggle"
    >
      <span class="exec-mode__label">{{ current.label }}</span>
      <svg class="exec-mode__caret" :class="{ 'exec-mode__caret--open': open }"
           width="12" height="12" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2">
        <!-- 默认朝下（收起），点击展开面板后朝上（--open 旋转 180°） -->
        <polyline points="18 9 12 15 6 9" />
      </svg>
    </button>

    <div v-if="open" class="exec-mode__panel">
      <button
        v-for="m in MODES"
        :key="m.id"
        type="button"
        class="exec-mode__item"
        :class="{ 'exec-mode__item--active': m.id === chat.execMode }"
        @click.stop="pick(m.id)"
      >
        <span class="exec-mode__item-title">{{ m.label }}</span>
        <span class="exec-mode__item-hint">{{ m.hint }}</span>
      </button>
    </div>
  </div>
</template>
