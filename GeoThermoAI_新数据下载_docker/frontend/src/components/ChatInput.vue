<script setup>
import { ref, computed } from 'vue'
import { useChatStore } from '../stores/chat'
import { useProjectStore } from '../stores/project'
import ExecModeSelect from './ExecModeSelect.vue'

const chat = useChatStore()
const project = useProjectStore()
const input = ref('')
const showFullPath = ref(false)

const folderName = computed(() => {
  const p = (project.projectDir || '').replace(/\/+$/, '')
  if (!p) return ''
  const seg = p.split('/').pop()
  return seg || p
})

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submit()
  }
}

async function submit() {
  if (chat.streaming) return
  if (!project.currentProject || !project.currentConv) return
  await chat.send(input.value)
  input.value = ''
}
</script>

<template>
  <div class="chat-input-area">
    <div v-if="folderName" class="folder-chip" :title="showFullPath ? '点击收起路径' : '点击查看完整路径'" @click="showFullPath = !showFullPath">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      </svg>
      <span class="folder-chip__text" :class="{ 'folder-chip__text--full': showFullPath }">
        {{ showFullPath ? project.projectDir : folderName }}
      </span>
    </div>

    <div class="chat-input-wrap">
      <div class="chat-input-box">
        <textarea
          v-model="input"
          rows="1"
          :placeholder="chat.streaming ? '回复生成中…' : (chat.chatMode === 'chat' ? 'Chat 模式：只读对话，仅回答问题' : '输入指令…（Enter 发送，Shift+Enter 换行）')"
          @keydown="onKeydown"
        ></textarea>
        <!-- 升级点 17：只有 Work 模式显示执行模式（由我批准/完全执行） -->
        <ExecModeSelect v-if="chat.chatMode === 'work'" />
        <button class="chat-send" :disabled="chat.streaming || !project.currentConv" @click="submit" title="发送">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="22" y1="2" x2="11" y2="13" />
            <polygon points="22 2 15 22 11 13 2 9 22 2" />
          </svg>
        </button>
      </div>
    </div>
  </div>
</template>
