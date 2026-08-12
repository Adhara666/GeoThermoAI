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

// 思考过程默认折叠：无论思考中还是思考结束，都不自动展开，
// 用户点击 summary 才展开查看内容
function thinkingOpen() {
  return false
}

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
          <!-- 思考过程折叠块：独立于正文，样式明显区分 -->
          <details v-if="m.thinking" class="thinking-box" :open="thinkingOpen()">
            <summary>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/></svg>
              <span>思考过程</span>
              <!-- 思考用时，格式 (用时XX秒) -->
              <span v-if="m.thinking_seconds" class="thinking-box__seconds">(用时{{ m.thinking_seconds }}秒)</span>
              <span v-if="chat.streaming && i === chat.messages.length - 1" class="thinking-box__live">思考中…</span>
            </summary>
            <div class="thinking-box__body">{{ m.thinking }}</div>
          </details>
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

/* ── 思考过程折叠块──────────────────────────────
   浅灰底、深灰字，与正文白色气泡区分但风格协调；
   思考未完成时自动展开，思考结束折叠后再输出正文 */
.thinking-box {
  margin: 0 0 10px; border-radius: 10px; overflow: hidden;
  background: #f2f4f7; border: 1px solid #e0e4ea;
  font-size: 13px;
}
.thinking-box summary {
  cursor: pointer; user-select: none; list-style: none;
  display: flex; align-items: center; gap: 7px;
  padding: 7px 12px; color: #5b6472; font-size: 12.5px; font-weight: 600;
}
.thinking-box summary::-webkit-details-marker { display: none; }
.thinking-box summary::after {
  content: ""; width: 6px; height: 6px; margin-left: auto; flex-shrink: 0;
  border-right: 1.5px solid #9aa1ad; border-bottom: 1.5px solid #9aa1ad;
  transform: rotate(-45deg); transition: transform 0.15s;
}
.thinking-box[open] summary::after { transform: rotate(45deg); }
.thinking-box summary:hover { background: #eaecef; }
.thinking-box summary svg { flex-shrink: 0; color: #8a92a0; }
.thinking-box__live {
  margin-left: auto; font-weight: 400; color: #8a92a0; font-size: 12px;
}
.thinking-box__body {
  margin: 0; padding: 8px 12px 10px;
  border-top: 1px dashed #d5d9e0; color: #4a5462;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 12px; line-height: 1.7; white-space: pre-wrap;
  word-break: break-word; max-height: 320px; overflow-y: auto;
}
</style>
