<script setup>
// Chat / Work 双模式切换
// Chat = 只读对话（Agent 不执行工作流、不改文件）；Work = 完整执行
// 风格与 ExecModeSelect 的"由我批准/完全执行"下拉一致：上拉面板选择
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const open = ref(false)
const root = ref(null)

const MODES = [
  { id: 'work', label: 'Work', hint: '完整能力：可执行降尺度工作流等相关任务' },
  { id: 'chat', label: 'Chat', hint: '只读对话：仅回答问题，不执行工作流、不改动文件' },
]

const current = computed(() => MODES.find((m) => m.id === chat.chatMode) || MODES[0])

function toggle() {
  open.value = !open.value
}

function pick(id) {
  chat.setChatMode(id)
  open.value = false
}

function onDocClick(e) {
  if (root.value && !root.value.contains(e.target)) open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div ref="root" class="chat-mode">
    <button
      class="chat-mode__btn"
      type="button"
      :title="current.hint"
      @click.stop="toggle"
    >
      <span class="chat-mode__label">{{ current.label }}</span>
      <svg class="chat-mode__caret" :class="{ 'chat-mode__caret--open': open }"
           width="12" height="12" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2">
        <polyline points="18 15 12 9 6 15" />
      </svg>
    </button>

    <div v-if="open" class="chat-mode__panel">
      <button
        v-for="m in MODES"
        :key="m.id"
        type="button"
        class="chat-mode__item"
        :class="{ 'chat-mode__item--active': m.id === chat.chatMode }"
        @click.stop="pick(m.id)"
      >
        <span class="chat-mode__item-title">{{ m.label }}</span>
        <span class="chat-mode__item-hint">{{ m.hint }}</span>
      </button>
    </div>
  </div>
</template>
