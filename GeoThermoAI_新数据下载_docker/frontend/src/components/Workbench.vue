<script setup>
import { ref, computed } from 'vue'
import ApiSettings from './panels/ApiSettings.vue'
import StudyArea from './panels/StudyArea.vue'
import ModelParams from './panels/ModelParams.vue'
import DataSources from './panels/DataSources.vue'
import DataSource from './panels/DataSource.vue'
import FileDownload from './panels/FileDownload.vue'
import MapView from './panels/MapView.vue'
import Accuracy from './panels/Accuracy.vue'
import Workflow from './panels/Workflow.vue'
import LogPanel from './panels/LogPanel.vue'
import { useSettingsStore } from '../stores/settings'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['close'])

const settings = useSettingsStore()
const active = ref('api')

// ── 面板宽度拖拽调整（拖动左侧边缘，限制最小/最大宽度） ──────
const MIN_PANEL_W = 320
const MAX_PANEL_W = 560
const panelWidth = ref(Number(localStorage.getItem('gtai_panel_w')) || 340)
const resizing = ref(false)

function onResizeStart(e) {
  if (!props.open) return
  resizing.value = true
  const startX = e.clientX
  const startW = panelWidth.value
  const onMove = (ev) => {
    // 面板在右侧：向左拖动（clientX 减小）→ 宽度增大
    const w = Math.max(MIN_PANEL_W, Math.min(MAX_PANEL_W, startW + (startX - ev.clientX)))
    panelWidth.value = w
  }
  const onUp = () => {
    resizing.value = false
    try { localStorage.setItem('gtai_panel_w', String(panelWidth.value)) } catch (_) {}
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
  document.body.style.cursor = 'ew-resize'
  document.body.style.userSelect = 'none'
}
// 线性 SVG 图标（Feather/Lucide 风格，stroke 2），比 emoji 更简洁专业
const tabs = [
  { id: 'api', label: 'API设置', icon: '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>' },
  { id: 'datasource', label: '数据源', icon: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>' },
  { id: 'test', label: '测试', icon: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>' },
  { id: 'studyarea', label: '研究区', icon: '<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>' },
  { id: 'params', label: '参数', icon: '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>' },
  { id: 'download', label: '下载', icon: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>' },
  { id: 'map', label: '地图', icon: '<polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"/><line x1="8" y1="2" x2="8" y2="18"/><line x1="16" y1="6" x2="16" y2="22"/>' },
  { id: 'workflow', label: '进度', icon: '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>' },
  { id: 'log', label: '日志', icon: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>' },
  { id: 'accuracy', label: '最终精度', icon: '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>' },
]

const modelTag = computed(() =>
  settings.configured ? `🟢 ${settings.displayName}` : '⚪ 未配置模型',
)
</script>

<template>
  <aside
    class="workbench"
    :class="{ 'workbench--collapsed': !open, 'workbench--resizing': resizing }"
    :style="open ? { width: panelWidth + 'px' } : undefined"
  >
    <div
      v-if="open"
      class="workbench__resizer"
      :class="{ 'workbench__resizer--active': resizing }"
      title="拖动调整面板宽度"
      @mousedown.prevent="onResizeStart"
    ></div>
    <div class="panel-tabs">
      <button
        v-for="t in tabs"
        :key="t.id"
        class="panel-tab"
        :class="{ 'panel-tab--active': active === t.id }"
        @click="active = t.id"
      >
        <svg
          v-if="t.icon"
          width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true"
          v-html="t.icon"
        ></svg>
        {{ t.label }}
      </button>
    </div>

    <div class="panel-body" :class="{ 'panel-body--map': active === 'map' }">
      <ApiSettings v-if="active === 'api'" />
      <DataSources v-else-if="active === 'datasource'" />
      <DataSource v-else-if="active === 'test'" />
      <StudyArea v-else-if="active === 'studyarea'" />
      <ModelParams v-else-if="active === 'params'" />
      <FileDownload v-else-if="active === 'download'" />
      <MapView v-else-if="active === 'map'" />
      <Accuracy v-else-if="active === 'accuracy'" />
      <Workflow v-else-if="active === 'workflow'" />
      <LogPanel v-else-if="active === 'log'" />
    </div>

    <div class="workbench__footer">
      <button class="workbench__collapse" title="收起工作面板" @click="emit('close')">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="9 6 15 12 9 18" />
        </svg>
        收起面板
      </button>
      <div class="workbench__meta">
        <div>当前模型：{{ modelTag }}</div>
        <div>GeoThermoAI · 地表温度降尺度分析</div>
      </div>
    </div>
  </aside>
</template>
