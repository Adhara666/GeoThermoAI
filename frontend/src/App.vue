<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import Sidebar from './components/Sidebar.vue'
import ChatMessages from './components/ChatMessages.vue'
import ChatInput from './components/ChatInput.vue'
import ChatModeSelect from './components/ChatModeSelect.vue'
import PairSelectCard from './components/PairSelectCard.vue'
import ApprovalCard from './components/ApprovalCard.vue'
import Workbench from './components/Workbench.vue'
import LoginView from './components/LoginView.vue'
import { toasts } from './composables/useToast'
import { useProjectStore } from './stores/project'
import { useSettingsStore } from './stores/settings'
import { useChatStore } from './stores/chat'
import { useAuthStore } from './stores/auth'

const project = useProjectStore()
const settings = useSettingsStore()
const chat = useChatStore()
const auth = useAuthStore()

const workbenchOpen = ref(true)
const sidebarOpen = ref(false)
const loggedIn = ref(false)

async function boot() {
  // 初始化失败不卡在登录页：兜底进入空状态主界面（网络瞬断等场景不阻塞使用）
  try {
    await Promise.all([project.bootstrap(), settings.load()])
  } catch (e) {
    console.warn('初始化失败，进入空状态', e)
  }
  // 自动加载第一个对话的消息
  if (project.currentProject && project.currentConv) {
    try {
      await chat.loadMessages(project.currentProject, project.currentConv)
      // 刷新后若该对话仍有正在运行的流：恢复最新气泡/日志并重新订阅 SSE，保持连续性
      await chat.resumeIfStreaming(project.currentConv)
      await chat.refreshWorkflow(project.currentConv)
    } catch (e) {
      console.warn('加载对话失败', e)
    }
  }
  loggedIn.value = true
}

function onAuthed() {
  boot()
}

function onUnauthorized() {
  // token 失效：清空会话数据，回登录页
  auth.user = null
  chat.clear()
  loggedIn.value = false
}

onMounted(async () => {
  window.addEventListener('gtai:unauthorized', onUnauthorized)
  await auth.init()
  if (auth.authed) await boot()
})

onBeforeUnmount(() => {
  window.removeEventListener('gtai:unauthorized', onUnauthorized)
})
</script>

<template>
  <LoginView v-if="!loggedIn" @authed="onAuthed" />

  <template v-else>
    <div class="app-shell">
      <Sidebar :open="sidebarOpen" @close="sidebarOpen = false" />

      <main class="chat-area">
        <header class="chat-header">
          <button class="menu-fab" @click="sidebarOpen = true" aria-label="打开侧边栏">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
          </button>
          <ChatModeSelect />
          <span class="chat-header__title">{{ project.currentConvTitle || '未选择对话' }}</span>
          <div class="chat-header__model">
            <span v-if="settings.configured" class="tag">🟢 {{ settings.displayName }}</span>
            <span v-else class="tag tag--warning">⚪ 未配置模型</span>
            <!-- 工作面板打开按钮：位于模型标签右侧，蓝底白字 -->
            <button v-if="!workbenchOpen" class="workbench-open-btn" @click="workbenchOpen = true" title="打开工作面板">
              <span>工作面板</span>
            </button>
          </div>
        </header>

        <ChatMessages />
        <PairSelectCard v-if="chat.paused && chat.pairs.length" />
        <ApprovalCard v-else-if="chat.paused && chat.approval" />
        <ChatInput />
      </main>

      <Workbench :open="workbenchOpen" @close="workbenchOpen = false" />
    </div>
  </template>

  <div class="toast-wrap">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="`toast--${t.type}`">{{ t.msg }}</div>
  </div>
</template>
