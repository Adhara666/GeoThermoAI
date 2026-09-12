<script setup>
/**
 * 任务结果选择器（面板联动中枢）：地图 / 精度 / 下载面板共用。
 *
 * 设计（用户反馈修订）：不再提供“自动（最新）”选项——进入对话后
 * 默认选中第一个任务，用户在下拉里显式切换；下拉为自绘样式，
 * 与发送区执行模式选择框保持同一视觉语言（圆角/边框/浮层）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useChatStore } from '../../stores/chat'
import { t } from '../../i18n'

const chat = useChatStore()
const open = ref(false)
const root = ref(null)

const options = computed(() =>
  (chat.kernelTasksOrder || []).map((id, i) => {
    const tk = chat.kernelTasks[id] || {}
    return { id, num: i + 1, label: tk.label || tk.region || t('taskSelect.num', { n: i + 1 }) }
  }))

const current = computed(() =>
  options.value.find((o) => o.id === chat.activeTaskId) || null)

// 默认选中第一个任务（替代原“自动（最新）”的默认行为）
watch(options, (list) => {
  if (!chat.activeTaskId && list.length) chat.setActiveTask(list[0].id)
}, { immediate: true })

function pick(id) {
  chat.setActiveTask(id)
  open.value = false
}

function toggle() {
  open.value = !open.value
}

function onDocClick(e) {
  if (root.value && !root.value.contains(e.target)) open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div v-if="options.length" ref="root" class="task-select">
    <button type="button" class="task-select__btn" @click.stop="toggle">
      <span class="task-select__label">
        <template v-if="current">{{ current.num }}. {{ current.label }}</template>
        <template v-else>{{ t('taskSelect.placeholder') }}</template>
      </span>
      <svg
        class="task-select__caret" :class="{ 'task-select__caret--open': open }"
        width="12" height="12" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="2" stroke-linecap="round"
        stroke-linejoin="round" aria-hidden="true"
      ><polyline points="6 9 12 15 18 9" /></svg>
    </button>
    <div v-if="open" class="task-select__menu">
      <button
        v-for="o in options" :key="o.id"
        type="button" class="task-select__item"
        :class="{ 'task-select__item--active': o.id === chat.activeTaskId }"
        @click.stop="pick(o.id)"
      >{{ o.num }}. {{ o.label }}</button>
    </div>
  </div>
</template>

<style scoped>
.task-select { position: relative; margin-bottom: 10px; }
.task-select__btn {
  width: 100%; display: flex; align-items: center; gap: 6px;
  border: 1px solid var(--border-strong); background: #fff;
  color: var(--text); border-radius: var(--radius-sm);
  font-size: 12px; padding: 5px 8px; cursor: pointer; text-align: left;
}
.task-select__btn:hover { border-color: var(--text-muted); }
.task-select__label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-select__caret { flex-shrink: 0; color: var(--text-muted); transition: transform .15s; }
.task-select__caret--open { transform: rotate(180deg); }
.task-select__menu {
  position: absolute; z-index: 1200; top: calc(100% + 4px); left: 0; right: 0;
  background: #fff; border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm); box-shadow: 0 6px 18px rgba(15, 23, 42, .12);
  max-height: 240px; overflow-y: auto;
}
.task-select__item {
  display: block; width: 100%; text-align: left; border: 0;
  background: transparent; color: var(--text);
  font-size: 12px; padding: 7px 10px; cursor: pointer;
}
.task-select__item:hover { background: var(--bg-panel); }
.task-select__item--active { color: var(--primary); font-weight: 600; }
</style>
