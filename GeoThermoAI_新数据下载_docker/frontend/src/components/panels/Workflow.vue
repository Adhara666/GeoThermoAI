<script setup>
import { computed, onMounted, watch } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useProjectStore } from '../../stores/project'

const chat = useChatStore()
const project = useProjectStore()

const steps = computed(() => {
  if (chat.workflowSteps.length) return chat.workflowSteps
  return [
    { id: 'data_acquisition', label: '数据获取', status: 'pending' },
    { id: 'data_pipeline', label: '数据预处理', status: 'pending' },
    { id: 'ttri_compute', label: 'TTRI 计算', status: 'pending' },
    { id: 'rf_model', label: '模型训练', status: 'pending' },
    { id: 'tcr_compute', label: 'TCR 计算', status: 'pending' },
    { id: 'lst_export', label: 'LST 导出', status: 'pending' },
    { id: 'accuracy_eval', label: '精度评估', status: 'pending' },
    { id: 'postprocess', label: '结果后处理（可选）', status: 'pending' },
  ]
})

// 步骤状态图标：线性 SVG（Feather 风格）+ 状态色，与整体图标风格统一
const ICON_META = {
  completed: { path: '<circle cx="12" cy="12" r="10"/><polyline points="9 12 11 14 15 9"/>', color: '#16a34a' },
  running: { path: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>', color: 'var(--primary)' },
  failed: { path: '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>', color: '#dc2626' },
  // 上游失败导致本步骤未执行（A-08）：与"失败"区分显示，明确不是"失败后仍继续完成"
  skipped_upstream: { path: '<circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/>', color: '#9ca3af' },
  // 可选步骤未执行（结果后处理）：灰色空心圆，明确是"用户未选择执行"
  skipped: { path: '<circle cx="12" cy="12" r="10"/>', color: '#9ca3af' },
  pending: { path: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>', color: '#9ca3af' },
}

const STATUS_LABELS = {
  completed: '完成', running: '进行中', failed: '失败',
  skipped_upstream: '未执行（上游失败）', skipped: '未执行（可选）', pending: '等待',
}
const STATUS_TAG_CLASS = {
  completed: 'success', running: '', failed: 'danger',
  skipped_upstream: 'muted', skipped: 'muted', pending: 'muted',
}

onMounted(() => {
  if (project.currentConv) chat.refreshWorkflow(project.currentConv)
})

// 升级点 7：切换对话/项目时工作面板进度与当前对话保持一致
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
          {{ STATUS_LABELS[s.status] ?? '等待' }}
        </span>
      </div>
    </div>
  </div>
</template>
