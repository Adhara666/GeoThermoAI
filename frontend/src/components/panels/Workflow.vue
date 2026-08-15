<script setup>
import { computed, onMounted, watch } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useProjectStore } from '../../stores/project'
import { t, wfStepLabel } from '../../i18n'

const chat = useChatStore()
const project = useProjectStore()

const FALLBACK_STEPS = [
  { id: 'data_acquisition', label: 'data_acquisition', status: 'pending' },
  { id: 'data_pipeline', label: 'data_pipeline', status: 'pending' },
  { id: 'ttri_compute', label: 'ttri_compute', status: 'pending' },
  { id: 'rf_model', label: 'rf_model', status: 'pending' },
  { id: 'tcr_compute', label: 'tcr_compute', status: 'pending' },
  { id: 'lst_export', label: 'lst_export', status: 'pending' },
  { id: 'accuracy_eval', label: 'accuracy_eval', status: 'pending' },
  { id: 'postprocess', label: 'postprocess', status: 'pending' },
]

const steps = computed(() => {
  const list = chat.workflowSteps.length ? chat.workflowSteps : FALLBACK_STEPS
  return list.map((s) => ({
    ...s,
    label: wfStepLabel(s.id, s.label),
  }))
})

// 步骤状态图标：线性 SVG（Feather 风格）+ 状态色，与整体图标风格统一
const ICON_META = {
  completed: { path: '<circle cx="12" cy="12" r="10"/><polyline points="9 12 11 14 15 9"/>', color: '#16a34a' },
  running: { path: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>', color: 'var(--primary)' },
  failed: { path: '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>', color: '#dc2626' },
  // 上游失败导致本步骤未执行：与"失败"区分显示，明确不是"失败后仍继续完成"
  skipped_upstream: { path: '<circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/>', color: '#9ca3af' },
  // 可选步骤未执行（结果后处理）：沿用时钟图标，与"等待"视觉一致，明确"未做"
  skipped: { path: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>', color: '#9ca3af' },
  pending: { path: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>', color: '#9ca3af' },
}

const STATUS_LABELS = computed(() => ({
  completed: t('wf.status.completed'),
  running: t('wf.status.running'),
  failed: t('wf.status.failed'),
  skipped_upstream: t('wf.status.skipped_upstream'),
  skipped: t('wf.status.skipped'),
  pending: t('wf.status.pending'),
}))
const STATUS_TAG_CLASS = {
  completed: 'success', running: '', failed: 'danger',
  skipped_upstream: 'muted', skipped: 'muted', pending: 'muted',
}

onMounted(() => {
  if (project.currentConv) chat.refreshWorkflow(project.currentConv)
})

// 切换对话/项目时工作面板进度与当前对话保持一致
watch(() => project.currentConv, (cid) => {
  if (cid) chat.refreshWorkflow(cid)
})
</script>

<template>
  <div>
    <div class="wf-list">
      <div
        v-for="s in steps"
        :key="s.id"
        class="wf-item"
        :class="`wf-item--${s.status}`"
      >
        <span class="wf-item__icon">
          <svg
            width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            :style="{ color: ICON_META[s.status]?.color }"
            v-html="(ICON_META[s.status] || ICON_META.pending).path"
          ></svg>
        </span>
        <span class="wf-item__label">{{ s.label }}</span>
          <span class="tag" :class="`tag--${STATUS_TAG_CLASS[s.status] ?? 'muted'}`">
          {{ STATUS_LABELS[s.status] ?? t('wf.status.pending') }}
        </span>
      </div>
    </div>
  </div>
</template>
