<script setup>
import { computed, onMounted, watch } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useProjectStore } from '../../stores/project'
import { useToast } from '../../composables/useToast'
import { t, wfStepLabel } from '../../i18n'

const chat = useChatStore()
const project = useProjectStore()
const toast = useToast()

// 第六阶段（§12.3）：任务卡按任务编号渲染——多城市/多任务各自推进，
// 互不阻塞；问题卡按问题编号提示等待原因；产物按产物编号下载。
const taskCards = computed(() =>
  (chat.kernelTasksOrder || []).map((id) => chat.kernelTasks[id]).filter(Boolean))

function taskQuestion(taskId) {
  const id = (chat.kernelQuestionsOrder || []).find((qid) => {
    const q = chat.kernelQuestions[qid]
    return (q.targets || []).some((x) => x.task_id === taskId)
  })
  return id ? chat.kernelQuestions[id] : null
}

const STATUS_TEXT = {
  draft: '草稿', ready: '就绪', queued: '排队中', running: '执行中',
  awaiting_info: '等待回答', completed: '已完成', failed: '失败',
  cancelled: '已取消',
}
function taskStatusText(s) {
  // 任务卡状态标签随界面语言（英文模式下不再残留中文）
  const key = `task.status.${s}`
  const v = t(key)
  if (v !== key) return v
  return STATUS_TEXT[s] || s || '—'
}

function currentNodeLabel(task) {
  // 快照节点字段名为 type（node_key 同义）；节点名随界面语言。
  // 注意：参数不能叫 t——会遮蔽 i18n 的 t() 导致渲染报错（面板白屏）
  const ntype = (n) => {
    const key = n.node_type || n.type || n.node_key || ""
    const tr = t(`node.${key}`)
    return tr !== `node.${key}` ? tr : (NODE_LABELS[key] || key)
  }
  const running = (task.nodes || []).find((n) => n.status === "running"
    || n.status === "committing")
  if (running) return `${ntype(running)}${t('wf.status.running') === 'Running' ? ' (Running)' : '（执行中）'}`
  const failed = (task.nodes || []).find((n) => n.status === "failed")
  if (failed) return `${ntype(failed)}${t('wf.status.failed') === 'Failed' ? ' (Failed)' : '（失败）'}`
  const wait = (task.nodes || []).find((n) => n.wait_reason)
  return wait ? `${ntype(wait)}：${wait.wait_reason}` : ""
}

async function onCancel(taskId, version) {
  const r = await chat.taskCommand(taskId, 'cancel', {}, version)
  if (r.ok) toast.info(t('wf.stopRequested'))
}

async function onRetry(taskId, version) {
  // 失败任务重试：后端把失败节点按现行预算重新排队（不新建任务）
  const r = await chat.taskCommand(taskId, 'retry', {}, version)
  if (r.ok) toast.info(t('wf.retrySubmitted'))
}

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

// 调度节点中文标签（与后端 NODE_TYPES 一致；新链路真实节点清单）
const NODE_LABELS = {
  search_scene: '检索场景候选', select_scene: '配对选择或场景确定',
  acquire_asset: '网络资产获取', prepare_local: '本地定标对齐准备',
  data_check: '原始包数据检查', preprocess_split: '预处理与空间划分',
  prep_check: '预处理数据检查', ttri: 'TTRI 拟合与应用',
  ttri_check: 'TTRI 数据检查', rf_round: 'RF 轮训练与测试预测',
  train_decision: '训练决定', promote_best: '登记最佳模型引用',
  tcr: 'TCR 与最终温度', export: '导出 GeoTIFF',
  closure_eval: '闭合评价', gapfill: '独立填洞产品', rebuild: '重建缺失输入',
}
const NODE_STATUS_TEXT = {
  pending: '等待', ready: '就绪', waiting_resource: '等待资源',
  running: '执行中', committing: '提交中', succeeded: '完成',
  failed: '失败', waiting_input: '等待回答', retry_wait: '重试等待',
  cancelled: '已取消', blocked: '上游阻塞',
}

// 每张任务卡展开显示自己的真实节点清单（新链路真实进度）
const NODE_ICON_STATUS = {
  succeeded: 'completed', running: 'running', committing: 'running',
  failed: 'failed', retry_wait: 'pending',
}
function taskNodes(task) {
  // 参数不能叫 t——会遮蔽 i18n 的 t()
  return (task.nodes || []).map((n) => {
    const key = n.node_type || n.type || n.node_key
    const iconStatus = NODE_ICON_STATUS[n.status] || 'pending'
    const labelKey = `node.${key}`
    const labelTr = t(labelKey)
    const statusKey = `nodestatus.${n.status}`
    const statusTr = t(statusKey)
    return {
      ...n,
      label: labelTr !== labelKey ? labelTr : (NODE_LABELS[key] || n.node_key || key),
      statusText: statusTr !== statusKey ? statusTr
        : (NODE_STATUS_TEXT[n.status] || n.status),
      wait: n.wait_reason ? `：${n.wait_reason}` : '',
      cls: `wf-item--${iconStatus}`,
      iconPath: (ICON_META[iconStatus] || ICON_META.pending).path,
      iconColor: (ICON_META[iconStatus] || ICON_META.pending).color,
    }
  })
}

// 每张任务卡：把真实调度节点聚合到用户熟悉的 8 步主链（直观展示），
// 节点细节保留在 currentNodeLabel 详情行。
const STEP_GROUPS = {
  data_acquisition: ['search_scene', 'select_scene', 'acquire_asset', 'prepare_local', 'data_check'],
  data_pipeline: ['preprocess_split', 'prep_check'],
  ttri_compute: ['ttri', 'ttri_check'],
  rf_model: ['rf_round', 'train_decision', 'promote_best'],
  tcr_compute: ['tcr'],
  lst_export: ['export'],
  accuracy_eval: ['closure_eval'],
  postprocess: ['gapfill'],
}
function mainSteps(task) {
  const nodes = task.nodes || []
  const ntype = (n) => n.node_type || n.type || n.node_key || ''
  return Object.entries(STEP_GROUPS).map(([id, keys]) => {
    const sel = nodes.filter((n) => keys.includes(ntype(n)))
    let status = 'pending'
    let statusText = t('wf.status.pending')
    if (sel.length) {
      if (sel.some((n) => n.status === 'failed')) { status = 'failed'; statusText = t('wf.status.failed') }
      else if (sel.some((n) => n.status === 'waiting_input')) { status = 'running'; statusText = t('nodestatus.waiting_input') }
      else if (sel.every((n) => n.status === 'succeeded')) { status = 'completed'; statusText = t('wf.status.completed') }
      else if (sel.some((n) => n.status === 'running' || n.status === 'committing')) { status = 'running'; statusText = t('wf.status.running') }
      else if (sel.some((n) => ['waiting_resource', 'retry_wait', 'ready', 'blocked'].includes(n.status))) { status = 'running'; statusText = t('wf.status.queued') }
    }
    return {
      id,
      name: t(`wf.${id}`),  // 中文态返回中文名，英文态返回英文名
      statusText,
      cls: status,
      iconPath: (ICON_META[status] || ICON_META.pending).path,
      iconColor: (ICON_META[status] || ICON_META.pending).color,
    }
  })
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
    <!-- 第六阶段：任务卡（按任务编号，多任务各自推进互不阻塞）
         每张卡内嵌本任务的真实节点清单（新链路真实进度） -->
    <div v-if="taskCards.length" class="kernel-tasks">
      <!-- 注意：循环变量不能叫 t——会遮蔽翻译函数 t()，卡片内 t('…') 会崩溃导致面板白屏 -->
      <div
        v-for="(task, ti) in taskCards" :key="task.task_id" class="kernel-task"
        :class="{ 'kernel-task--active': chat.activeTaskId === task.task_id }"
        @click="chat.setActiveTask(task.task_id)"
      >
        <div class="kernel-task__head">
          <span class="kernel-task__num">{{ ti + 1 }}</span>
          <strong>{{ task.label || task.region }}</strong>
          <span class="tag">{{ taskStatusText(task.summary_status) }}</span>
        </div>
        <div v-if="currentNodeLabel(task)" class="kernel-task__detail">
          {{ currentNodeLabel(task) }}
        </div>
        <div v-if="taskQuestion(task.task_id)" class="kernel-task__detail kernel-task__wait">
          {{ t('wf.waitAnswer') }}{{ taskQuestion(task.task_id).prompt }}
        </div>
        <div class="kernel-task__subtitle">{{ t('wf.progressOf', { n: ti + 1 }) }}</div>
        <div class="kernel-task__nodes wf-list">
          <div
            v-for="s in mainSteps(task)" :key="s.id"
            class="wf-item"
            :class="`wf-item--${s.cls}`"
          >
            <span class="wf-item__icon">
              <svg
                width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                :style="{ color: s.iconColor }"
                v-html="s.iconPath"
              ></svg>
            </span>
            <span class="wf-item__label">{{ s.name }}</span>
            <span class="tag">{{ s.statusText }}</span>
          </div>
        </div>
        <div class="kernel-task__ops">
          <button
            v-if="task.summary_status === 'running' || task.summary_status === 'queued'"
            class="btn btn--sm" @click.stop="onCancel(task.task_id, task.version)"
          >{{ t('wf.cancel') }}</button>
          <button
            v-if="task.summary_status === 'failed'"
            class="btn btn--sm btn--primary" @click.stop="onRetry(task.task_id, task.version)"
          >{{ t('wf.retry') }}</button>
        </div>
      </div>
    </div>
    <!-- 旧链路（无台账任务）时保留原步骤面板；新链路下隐藏避免误导 -->
    <div class="wf-list" v-if="!taskCards.length">
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
