<script setup>
import { ref, watch, nextTick, onMounted, onUnmounted, computed } from 'vue'
import { useChatStore } from '../../stores/chat'
import { api } from '../../api'
import { t } from '../../i18n'

const chat = useChatStore()
const box = ref(null)
const autoscroll = ref(true)
const usage = ref(null) // { mem_gb, disk_gb }
const copied = ref(false)
const filterTask = ref('') // 日志过滤：空串=全部；否则任务号（跟随选中中枢）

// 日志条目兼容：新链路为 {text, task_id}，旧链路/服务端恢复为字符串
const textOf = (l) => (typeof l === 'string' ? l : (l && l.text) || '')
const taskOf = (l) => (l && typeof l === 'object' && l.task_id) || ''

// 按任务过滤后的渲染窗口（仅作用于当前渲染尾部，全量复制不受影响）
const viewLines = computed(() => {
  if (!filterTask.value) return chat.logLines
  return chat.logLines.filter((l) => taskOf(l) === filterTask.value)
})

// 可过滤的任务 chips（按任务编号）；跟随面板选中中枢自动切换
const taskChips = computed(() =>
  (chat.kernelTasksOrder || []).map((id, i) => {
    const tk = chat.kernelTasks[id] || {}
    return { id, num: i + 1, label: tk.label || tk.region || `任务 ${i + 1}` }
  }))

watch(() => chat.activeTaskId, (tid) => { filterTask.value = tid || '' }, { immediate: true })

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
  () => viewLines.value.length,
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
  // 清除走 store：内存视图与服务端持久记录一起清（用户显式操作）
  chat.clearLogs()
  autoscroll.value = true
}

/** 复制完整日志（logAll 是完整副本，不受渲染窗口与过滤限制）；剪贴板 API 不可用时降级 */
async function copyLog() {
  const src = chat.logAll.length ? chat.logAll : chat.logLines
  const full = src.map(textOf).join('\n')
  if (!full) return
  try {
    await navigator.clipboard.writeText(full)
  } catch (_) {
    const ta = document.createElement('textarea')
    ta.value = full
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try { document.execCommand('copy') } catch (_) {}
    ta.remove()
  }
  copied.value = true
  setTimeout(() => (copied.value = false), 1600)
}
</script>

<template>
  <div class="log-panel">
    <div class="log-panel__head">
      <span class="log-panel__title">{{ t('log.title') }}</span>
      <span class="log-panel__count">{{ t('log.count', { n: chat.logAll.length || chat.logLines.length }) }}</span>
      <button class="log-panel__clear" :disabled="!chat.logAll.length && !chat.logLines.length" @click="copyLog">{{ copied ? t('log.copied') : t('log.copy') }}</button>
      <button class="log-panel__clear" :disabled="!chat.logAll.length && !chat.logLines.length" @click="clearLog">{{ t('log.clear') }}</button>
      <span v-if="usage" class="log-panel__usage">
        {{ t('log.usage', { m: usage.mem_gb.toFixed(2), d: usage.disk_gb.toFixed(2) }) }}
        <span class="log-panel__usage-tip">{{ t('log.usageTitle') }}</span>
      </span>
    </div>
    <!-- 任务过滤 chips：在日志框上方（用户反馈：不能嵌在日志框里面） -->
    <div v-if="taskChips.length" class="log-filter">
      <button
        class="log-filter__chip"
        :class="{ 'log-filter__chip--active': !filterTask }"
        @click="filterTask = ''; chat.setActiveTask('')"
      >{{ t('log.all') }}</button>
      <button
        v-for="c in taskChips" :key="c.id"
        class="log-filter__chip"
        :class="{ 'log-filter__chip--active': filterTask === c.id }"
        @click="filterTask = c.id; chat.setActiveTask(c.id)"
      >{{ c.num }}. {{ c.label }}</button>
    </div>
    <div ref="box" class="log-panel__box" @scroll="onScroll">
      <div
        v-for="(line, i) in viewLines"
        :key="i"
        class="log-line"
        :class="{ 'log-line--warn': textOf(line).includes('[WARN]') }"
      >{{ textOf(line) }}</div>
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
  position: relative; cursor: default;
  font-size: 11px; color: var(--text-secondary); background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 1px 8px;
  white-space: nowrap; margin-left: auto;
}
/* 悬浮说明：自定义卡片提示（替代浏览器原生黑底 title，与整体 UI 风格一致） */
.log-panel__usage-tip {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 700;
  background: rgba(255, 255, 255, 0.98); color: var(--text-secondary);
  border: 1px solid var(--border); border-radius: 8px;
  box-shadow: var(--shadow); padding: 6px 10px; font-size: 12px;
  white-space: nowrap; opacity: 0; visibility: hidden;
  transform: translateY(-2px);
  transition: opacity 0.15s, transform 0.15s, visibility 0.15s;
  pointer-events: none;
}
.log-panel__usage:hover .log-panel__usage-tip {
  opacity: 1; visibility: visible; transform: translateY(0);
}
.log-panel__clear {
  border: 1px solid var(--border-strong); background: var(--bg-panel);
  color: var(--text-secondary); border-radius: var(--radius-sm);
  font-size: 11px; padding: 2px 10px; cursor: pointer;
}
.log-panel__clear:hover:not(:disabled) { color: var(--text); border-color: var(--text-muted); }
.log-panel__clear:disabled { opacity: 0.4; cursor: default; }
.log-panel__box {
  flex: 1; min-height: 0; overflow-y: auto; margin-top: 2px;
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 8px 10px;
  font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
  font-size: 11px; line-height: 1.6;
}
.log-line { color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; }
.log-line--warn { color: #d97706; }

/* ── 任务过滤 chips（在日志框上方；面板联动：与选中任务同步） ── */
.log-filter {
  display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 0 8px;
  font-family: system-ui, -apple-system, sans-serif;
}
.log-filter__chip {
  border: 1px solid var(--border-strong); background: #fff;
  color: var(--text-secondary); border-radius: 999px;
  font-size: 11px; padding: 2px 10px; cursor: pointer;
}
.log-filter__chip:hover { color: var(--text); }
.log-filter__chip--active {
  background: var(--primary, #2563eb); border-color: var(--primary, #2563eb);
  color: #fff;
}
</style>
