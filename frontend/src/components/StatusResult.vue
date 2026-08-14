<script setup>
import { computed } from 'vue'

/**
 * 状态结果组件：把带 emoji 前缀（✅/⚠️/❌）的多行文本渲染为
 * 图标 + 文本的状态行列表（ok=绿 / warn=黄 / fail=红 / 其余=灰）。
 * 与「测试」页面测试结果展示同款风格，供测试页与研究区上传验证共用。
 */
const props = defineProps({
  text: { type: String, default: '' },
})

const STATUS_ICONS = {
  ok: '<path d="M20 6L9 17l-5-5"/>',
  warn: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  fail: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
}

const resultLines = computed(() => {
  if (!props.text) return []
  // 行尾句号不显示
  const noEndPeriod = (s) => s.replace(/。$/, '')
  return String(props.text)
    .split('\n')
    .map((line) => {
      const t = line.trim()
      if (!t || t === '---') return null
      if (t.startsWith('✅')) return { type: 'ok', text: noEndPeriod(t.replace(/^✅\s*/, '')) }
      if (t.startsWith('⚠️')) return { type: 'warn', text: noEndPeriod(t.replace(/^⚠️\s*/, '')) }
      if (t.startsWith('❌')) return { type: 'fail', text: noEndPeriod(t.replace(/^❌\s*/, '')) }
      return { type: 'plain', text: noEndPeriod(t) }
    })
    .filter(Boolean)
})
</script>

<template>
  <div v-if="resultLines.length" class="status-result">
    <div
      v-for="(l, i) in resultLines"
      :key="i"
      class="status-result__row"
      :class="`status-result__row--${l.type}`"
    >
      <svg
        v-if="STATUS_ICONS[l.type]"
        class="status-result__icon"
        width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
        aria-hidden="true"
        v-html="STATUS_ICONS[l.type]"
      ></svg>
      <span>{{ l.text }}</span>
    </div>
  </div>
</template>

<style scoped>
.status-result {
  margin-top: 4px; padding: 10px 12px; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  display: flex; flex-direction: column; gap: 5px;
}
.status-result__row {
  display: flex; align-items: flex-start; gap: 7px;
  font-size: 12.5px; line-height: 1.6; color: var(--text); word-break: break-all;
}
.status-result__icon { flex-shrink: 0; margin-top: 3px; }
.status-result__row--ok { color: var(--success); }
.status-result__row--warn { color: var(--warning); }
.status-result__row--fail { color: var(--danger); }
.status-result__row--plain { color: var(--text-secondary); }
</style>
