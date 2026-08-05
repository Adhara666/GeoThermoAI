<script setup>
import { ref, watch, nextTick } from 'vue'
import { useChatStore } from '../stores/chat'
import MarkdownRender from './MarkdownRender.vue'

const chat = useChatStore()
const scrollEl = ref(null)

watch(
  () => [chat.messages, chat.streaming],
  async () => {
    await nextTick()
    if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
  },
  { deep: true },
)

function pairText(p) {
  const s = p.sentinel2_date || p.sentinel_date || '?'
  const sc = p.sentinel2_coverage || p.sentinel_coverage || '?'
  const scn = p.sentinel2_count || p.sentinel_count || '?'
  return `${p.landsat_satellite || '?'} ${p.landsat_date || '?'}（${p.landsat_count || '?'} 景, 覆盖 ${p.landsat_coverage || '?'}%）+ Sentinel ${s}（${scn} 景, 覆盖 ${sc}%）`
}
</script>

<template>
  <div ref="scrollEl" class="chat-messages">
    <div v-if="!chat.messages.length" class="chat-empty">
      <img class="chat-empty__logo" src="/logo.png?v=2" alt="GeoThermoAI" />
      <h2>GeoThermoAI</h2>
      <p>
        基于跨尺度热响应一致性的高分辨率地表温度智能重建系统<br />
        描述你的任务，例如：「对武汉市做地表温度降尺度全流程处理」
      </p>
    </div>
    <div v-else class="chat-scroll">
      <div v-for="(m, i) in chat.messages" :key="i" class="msg" :class="m.role === 'user' ? 'msg--user' : 'msg--ai'">
        <div class="msg__bubble">
          <MarkdownRender :content="m.content" />
          <span v-if="chat.streaming && i === chat.messages.length - 1 && m.role === 'assistant'" class="typing-cursor"></span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.typing-cursor {
  display: inline-block; width: 2px; height: 14px; background: currentColor;
  margin-left: 2px; vertical-align: -2px; animation: blink 0.8s step-end infinite;
}
@keyframes blink { 50% { opacity: 0; } }
</style>
