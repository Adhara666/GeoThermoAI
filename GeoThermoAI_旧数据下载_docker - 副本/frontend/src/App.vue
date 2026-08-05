<script setup>
import { ref, onMounted } from 'vue'
import Sidebar from './components/Sidebar.vue'
import ChatMessages from './components/ChatMessages.vue'
import ChatInput from './components/ChatInput.vue'
import PairSelectCard from './components/PairSelectCard.vue'
import Workbench from './components/Workbench.vue'
import { toasts } from './composables/useToast'
import { useProjectStore } from './stores/project'
import { useSettingsStore } from './stores/settings'
import { useChatStore } from './stores/chat'

const project = useProjectStore()
const settings = useSettingsStore()
const chat = useChatStore()

const workbenchOpen = ref(true)
const sidebarOpen = ref(false)

onMounted(async () => {
  await Promise.all([project.bootstrap(), settings.load()])
  // 自动加载第一个对话的消息
  if (project.currentProject && project.currentConv) {
    await chat.loadMessages(project.currentProject, project.currentConv)
    await chat.refreshWorkflow(project.currentConv)
  }
})
</script>

<template>
  <div class="app-shell">
    <Sidebar :open="sidebarOpen" @close="sidebarOpen = false" />

    <main class="chat-area">
      <header class="chat-header">
        <button class="menu-fab" @click="sidebarOpen = true" aria-label="打开侧边栏">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
        </button>
        <span class="chat-header__title">{{ project.currentConvTitle || '未选择对话' }}</span>
        <div class="chat-header__model">
          <span v-if="settings.configured" class="tag">🟢 {{ settings.displayName }}</span>
          <span v-else class="tag tag--warning">⚪ 未配置模型</span>
        </div>
      </header>

      <ChatMessages />
      <PairSelectCard v-if="chat.paused && chat.pairs.length" />
      <ChatInput />
    </main>

    <button v-if="!workbenchOpen" class="workbench-toggle-fab" @click="workbenchOpen = true" title="打开工作面板">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
    </button>

    <Workbench :open="workbenchOpen" @close="workbenchOpen = false" />
  </div>

  <div class="toast-wrap">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="`toast--${t.type}`">{{ t.msg }}</div>
  </div>
</template>
