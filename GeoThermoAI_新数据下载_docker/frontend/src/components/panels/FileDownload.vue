<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useProjectStore } from '../../stores/project'
import { api, downloadUrl } from '../../api'

const project = useProjectStore()
const files = ref([])
const selectedSet = ref(new Set())
const loading = ref(false)
const status = ref('')
const downloading = ref(false)
const progress = ref(0)

// 状态图标（线性 SVG，与整体风格一致，替换 emoji）
const STATUS_ICONS = {
  ok: '<path d="M20 6L9 17l-5-5"/>',
  warn: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  fail: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
}

// 状态行解析：emoji 前缀 → 类型 + 去掉 emoji 文本（与整体线性图标风格一致）
const statusInfo = computed(() => {
  const s = status.value || ''
  if (!s) return { type: '', text: '' }
  if (s.startsWith('✅')) return { type: 'ok', text: s.replace(/^✅\s*/, '') }
  if (s.startsWith('⚠️')) return { type: 'warn', text: s.replace(/^⚠️\s*/, '') }
  if (s.startsWith('❌')) return { type: 'fail', text: s.replace(/^❌\s*/, '') }
  return { type: 'plain', text: s }
})

function fmtSize(n) {
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

async function refresh() {
  if (!project.projectDir) {
    status.value = '❌ 请先在左侧边栏新建项目'
    return
  }
  loading.value = true
  try {
    const r = await api.get(`/api/files?project_dir=${encodeURIComponent(project.projectDir)}`)
    if (!r.ok) {
      status.value = r.message || '目录不存在'
      files.value = []
      return
    }
    files.value = r.files || []
    selectedSet.value = new Set()
    status.value = files.value.length ? `✅ 共 ${files.value.length} 个文件（含子目录）` : '⚠️ 目录为空'
  } catch (e) {
    status.value = `❌ ${e.message}`
  } finally {
    loading.value = false
  }
}

// 多选（升级点 26）：点击行切换勾选；点击文件行默认选中该项
const selectedCount = computed(() => selectedSet.value.size)
const totalSize = computed(() => {
  let sum = 0
  for (const f of files.value) if (selectedSet.value.has(f.path)) sum += f.size
  return sum
})

function toggle(path) {
  const next = new Set(selectedSet.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  selectedSet.value = next
}

function toggleAll() {
  const next = new Set(selectedSet.value)
  const allPaths = files.value.map((f) => f.path)
  const allSelected = allPaths.every((p) => next.has(p))
  if (allSelected) allPaths.forEach((p) => next.delete(p))
  else allPaths.forEach((p) => next.add(p))
  selectedSet.value = next
}

/**
 * 批量下载：多文件 → 后端打包 zip（/api/download/multiple）；
 * 单文件 → 保持原有 fetch 流式 + 进度条 + Blob 保存。
 */
async function doDownload() {
  if (!selectedCount.value || downloading.value) return
  const selectedPaths = files.value
    .map((f) => f.path)
    .filter((p) => selectedSet.value.has(p))

  if (selectedPaths.length === 1) {
    await downloadSingle(selectedPaths[0])
    return
  }
  // 多文件：POST 打包 zip，fetch 流式读取并保存
  downloading.value = true
  progress.value = 0
  try {
    const res = await fetch('/api/download/multiple', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('gtai_token') || ''}`,
      },
      body: JSON.stringify({
        project_dir: project.projectDir,
        paths: selectedPaths,
      }),
    })
    if (!res.ok) {
      const txt = await res.text().catch(() => '')
      alert(`下载失败 ${res.status}: ${txt.slice(0, 200)}`)
      return
    }
    const total = Number(res.headers.get('content-length') || 0)
    const reader = res.body.getReader()
    const chunks = []
    let received = 0
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(value)
      received += value.length
      if (total) progress.value = Math.min(100, Math.round((received / total) * 100))
    }
    const blob = new Blob(chunks, { type: 'application/zip' })
    saveBlob(blob, 'geothermoai_download.zip')
  } catch (e) {
    alert(`下载失败: ${e.message}`)
  } finally {
    downloading.value = false
    progress.value = 0
  }
}

async function downloadSingle(path) {
  const url = downloadUrl(project.projectDir, path)
  downloading.value = true
  progress.value = 0
  try {
    const res = await fetch(url)
    if (!res.ok) {
      const txt = await res.text().catch(() => '')
      alert(`下载失败 ${res.status}: ${txt.slice(0, 200)}`)
      return
    }
    const total = Number(res.headers.get('content-length') || 0)
    const reader = res.body.getReader()
    const chunks = []
    let received = 0
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(value)
      received += value.length
      if (total) progress.value = Math.min(100, Math.round((received / total) * 100))
    }
    const blob = new Blob(chunks)
    saveBlob(blob, path.split('/').pop())
  } catch (e) {
    alert(`下载失败: ${e.message}`)
  } finally {
    downloading.value = false
    progress.value = 0
  }
}

function saveBlob(blob, filename) {
  const objUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(objUrl)
}

onMounted(refresh)
// 项目目录变化（含重新加载后状态恢复）时自动刷新文件列表
watch(() => project.projectDir, () => refresh())
</script>

<template>
  <div>
    <p class="form-hint dl-tip">下载项目目录中的文件（含子目录，相对路径显示为「子目录/文件名」）；支持勾选多个文件打包下载</p>
    <button class="btn btn--block" :disabled="loading" @click="refresh">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
      刷新文件列表
    </button>
    <div v-if="status" class="form-hint dl-status" :class="`dl-status--${statusInfo.type}`">
      <svg v-if="STATUS_ICONS[statusInfo.type]" class="dl-status__icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" v-html="STATUS_ICONS[statusInfo.type]"></svg>
      <span>{{ statusInfo.text }}</span>
    </div>

    <template v-if="files.length">
      <div class="form-group" style="margin-top:8px">
        <label>选择文件（可多选）</label>
        <div class="dl-list">
          <div class="dl-list__inner">
            <div class="dl-list__item dl-list__item--all" @click="toggleAll">
              <input
                type="checkbox"
                class="dl-list__check"
                :checked="selectedCount === files.length"
                @click.stop
                @change="toggleAll"
              />
              <span class="dl-list__path">全选 / 取消全选</span>
            </div>
            <div
              v-for="f in files"
              :key="f.path"
              class="dl-list__item"
              :class="{ 'dl-list__item--active': selectedSet.has(f.path) }"
              :title="f.path"
              @click="toggle(f.path)"
            >
              <input
                type="checkbox"
                class="dl-list__check"
                :checked="selectedSet.has(f.path)"
                @click.stop
                @change="toggle(f.path)"
              />
              <span class="dl-list__path">{{ f.path }}</span>
              <span class="dl-list__size">{{ fmtSize(f.size) }}</span>
            </div>
          </div>
        </div>
      </div>
      <button
        v-if="selectedCount"
        class="btn btn--primary btn--block"
        :disabled="downloading"
        @click="doDownload"
      >
        <svg v-if="!downloading" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        {{ downloading ? `打包下载中… ${progress}%` : `下载已选 ${selectedCount} 个文件（${fmtSize(totalSize)}）` }}
      </button>
      <div v-if="downloading" class="dl-progress">
        <div class="dl-progress__bar" :style="{ width: progress + '%' }"></div>
      </div>
      <a
        v-if="selectedCount === 1"
        class="btn btn--ghost btn--sm"
        style="margin-top:6px"
        :href="downloadUrl(project.projectDir, files.find((f) => selectedSet.has(f.path))?.path)"
        download
      >浏览器直接下载（备用，无进度）</a>
      <p class="form-hint" style="margin-top:6px">
        点击主按钮后前端会显示实时下载进度，多文件将打包为 zip，下载完成自动保存到浏览器下载目录
      </p>
    </template>
  </div>
</template>

<style scoped>
/* 下载说明与刷新按钮间距：与下方「状态 → 列表」组件的间距保持一致（升级点：间距统一） */
.dl-tip { margin-bottom: 10px; }
/* 状态行：SVG 图标 + 文本（替换 emoji） */
.dl-status { margin: 8px 0; display: flex; align-items: flex-start; gap: 6px; }
.dl-status__icon { flex-shrink: 0; margin-top: 2px; }
.dl-status--ok { color: var(--success); }
.dl-status--warn { color: var(--warning); }
.dl-status--fail { color: var(--danger); }
.dl-status--plain { color: inherit; }
/* 文件列表：支持上下 + 左右双向滚动（完整路径通过横向滚动查看） */
.dl-list {
  max-height: 200px;
  overflow: auto;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-panel);
  scrollbar-width: thin;
}
.dl-list__inner { width: max-content; min-width: 100%; }
.dl-list__item {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 10px; cursor: pointer; white-space: nowrap;
  font-size: 13px; border-bottom: 1px solid var(--border);
}
.dl-list__item:last-child { border-bottom: none; }
.dl-list__item--all { color: var(--text-secondary); font-weight: 500; }
.dl-list__item:hover { background: #eef0f5; }
.dl-list__item--active { background: var(--primary-soft); color: var(--primary); }
.dl-list__check { accent-color: var(--primary); margin: 0; flex-shrink: 0; cursor: pointer; }
.dl-list__path { color: inherit; }
.dl-list__size { margin-left: auto; color: var(--text-muted); font-size: 12px; flex-shrink: 0; }

/* 前端实时下载进度条 */
.dl-progress {
  margin-top: 6px; height: 6px; border-radius: 3px; overflow: hidden;
  background: var(--border-strong); position: relative;
}
.dl-progress__bar {
  height: 100%; background: var(--primary); border-radius: 3px;
  transition: width 0.15s linear;
}
</style>
