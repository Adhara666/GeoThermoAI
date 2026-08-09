<script setup>
import { ref, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useChatStore } from '../../stores/chat'
import { api } from '../../api'

const chat = useChatStore()
const box = ref(null)
const autoscroll = ref(true)
const usage = ref(null) // { mem_gb, disk_gb }

// 实时资源占用：内存每 5 秒刷新，磁盘后端 30 秒缓存
let usageTimer = null
async function refreshUsage() {
  try {
    usage.value = await api.get('/api/sysinfo')
  } catch (_) {
    /* 接口暂不可用则不显示 */
  }
}

// 切到日志页时组件重新挂载，默认直接定位到最新一行（底部）
onMounted(async () => {
  await nextTick()
  if (box.value) box.value.scrollTop = box.value.scrollHeight
  refreshUsage()
  usageTimer = setInterval(refreshUsage, 5000)
})

onUnmounted(() => {
  if (usageTimer) clearInterval(usageTimer)
})

// 实时日志追加时自动滚动到底部（用户手动上滚时暂停跟随）
watch(
  () => chat.logLines.length,
  async () => {
    if (!autoscroll.value || !box.value) return
    await nextTick()
    box.value.scrollTop = box.value.scrollHeight
  },
)

function onScroll() {
  const el = box.value
  if (!el) return
  // 距底部 40px 以内视为"跟随滚动"，否则用户可能正在回看历史日志
  autoscroll.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

function clearLog() {
  chat.logLines = []
  autoscroll.value = true
}
</script>

<template>
  <div class="log-panel">
    <div class="log-panel__head">
      <span class="log-panel__title">实时日志</span>
      <span class="log-panel__count">{{ chat.logLines.length }} 行</span>
      <button class="log-panel__clear" :disabled="!chat.logLines.length" @click="clearLog">清除</button>
      <span v-if="usage" class="log-panel__usage" title="本软件实时占用（含所有项目数据）">内存 {{ usage.mem_gb.toFixed(2) }}G · 磁盘 {{ usage.disk_gb.toFixed(2) }}G</span>
    </div>
    <div ref="box" class="log-panel__box" @scroll="onScroll">
      <div
        v-for="(line, i) in chat.logLines"
        :key="i"
        class="log-line"
        :class="{ 'log-line--warn': line.includes('[WARN]') }"
      >{{ line }}</div>
    </div>
  </div>
</template>

<style scoped>
.log-panel { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.log-panel__head {
  display: flex; align-items: center; gap: 8px;
  padding: 2px 0 8px; border-bottom: 1px solid var(--border);
}
.log-panel__title { font-size: 12px; font-weight: 600; color: var(--text); }
.log-panel__count { font-size: 11px; color: var(--text-muted); }
.log-panel__usage {
  font-size: 11px; color: var(--text-secondary); background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 1px 8px;
  white-space: nowrap; margin-left: auto;
}
.log-panel__clear {
  border: 1px solid var(--border-strong); background: var(--bg-panel);
  color: var(--text-secondary); border-radius: var(--radius-sm);
  font-size: 11px; padding: 2px 10px; cursor: pointer;
}
.log-panel__clear:hover:not(:disabled) { color: var(--text); border-color: var(--text-muted); }
.log-panel__clear:disabled { opacity: 0.4; cursor: default; }
.log-panel__box {
  flex: 1; min-height: 0; overflow-y: auto; margin-top: 8px;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 8px 10px;
  font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
  font-size: 11px; line-height: 1.6;
}
.log-line { color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; }
.log-line--warn { color: #d97706; }
</style>
