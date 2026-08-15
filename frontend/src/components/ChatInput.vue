<script setup>
import { ref, computed } from 'vue'
import { useChatStore } from '../stores/chat'
import { useProjectStore } from '../stores/project'
import { t } from '../i18n'
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
    <div v-if="folderName" class="folder-chip" :title="showFullPath ? t('chatInput.collapsePath') : t('chatInput.fullPath')" @click="showFullPath = !showFullPath">
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
          :placeholder="chat.streaming ? t('chatInput.streamingPh') : (chat.chatMode === 'chat' ? t('chatInput.chatModePh') : t('chatInput.placeholder'))"
          @keydown="onKeydown"
        ></textarea>
        <!-- 只有 Work 模式显示执行模式（由我批准/完全执行） -->
        <ExecModeSelect v-if="chat.chatMode === 'work'" />
        <button class="chat-send" :disabled="chat.streaming || !project.currentConv" @click="submit" :title="t('chatInput.send')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="22" y1="2" x2="11" y2="13" />
            <polygon points="22 2 15 22 11 13 2 9 22 2" />
          </svg>
        </button>
      </div>
    </div>
  </div>
</template>
