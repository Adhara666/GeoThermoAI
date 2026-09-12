<script setup>
/**
 * 下载面板（用户反馈修订）：只保留“任务产物”单一视图——
 * 原「全部文件」目录扫描视图已完全移除（仅兼容旧对话，新对话不再需要）。
 * 产物按选中任务（TaskResultSelect 中枢）从台账编号解析，直接下载。
 */
import { computed } from 'vue'
import { useChatStore } from '../../stores/chat'
import { getToken } from '../../api'
import { t } from '../../i18n'
import TaskResultSelect from './TaskResultSelect.vue'

const chat = useChatStore()

// 选中任务的产物行（按产物编号；含桥接到项目视图的文件名）
// 下载链接必须带 token（<a> 直连不带 Authorization 头，否则浏览器提示需要授权）
const taskRows = computed(() =>
  chat.activeTaskArtifacts.map((a) => ({
    id: a.id,
    name: (a.view_path || a.path || '').split('/').pop(),
    path: a.view_path || a.path,
    type: a.type,
    availability: a.availability,
    url: `/api/artifacts/${a.id}/download?token=${encodeURIComponent(getToken())}`,
  })))
</script>

<template>
  <div>
    <TaskResultSelect />
    <p class="form-hint dl-tip">{{ t('fd.taskProducts') }}</p>
    <div class="dl-list" style="max-height:320px">
      <div v-if="!taskRows.length" class="dl-task-empty">{{ t('fd.taskEmpty') }}</div>
      <div
        v-for="r in taskRows" :key="r.id"
        class="dl-list__item" :title="r.path"
      >
        <span class="dl-list__path">{{ r.name }}</span>
        <span class="dl-list__size">{{ r.type }}</span>
        <a class="btn btn--ghost btn--sm" :href="r.url" download>{{ t('fd.downloadOne') }}</a>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dl-tip { margin-bottom: 10px; }
/* 产物列表：支持上下 + 左右双向滚动（完整路径通过横向滚动查看） */
.dl-list {
  max-height: 200px;
  overflow: auto;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-panel);
  scrollbar-width: thin;
}
.dl-list__item {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 10px; white-space: nowrap;
  font-size: 13px; border-bottom: 1px solid var(--border);
}
.dl-list__item:last-child { border-bottom: none; }
.dl-list__item:hover { background: #eef0f5; }
.dl-list__path { color: inherit; }
.dl-list__size { margin-left: auto; color: var(--text-muted); font-size: 12px; flex-shrink: 0; }
.dl-task-empty { padding: 16px 12px; font-size: 12px; color: var(--text-muted); text-align: center; }
</style>
