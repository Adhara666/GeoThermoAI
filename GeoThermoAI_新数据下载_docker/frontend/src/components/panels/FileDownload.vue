<script setup>
import { ref, onMounted, watch } from 'vue'
import { useProjectStore } from '../../stores/project'
import { api, downloadUrl } from '../../api'

const project = useProjectStore()
const files = ref([])
const selected = ref('')
const loading = ref(false)
const status = ref('')
const downloading = ref(false)
const progress = ref(0)

function fmtSize(n) {
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

async function refresh() {
  if (!project.projectDir) {
    status.value = '❌ 请先在侧边栏设置并保存项目路径'
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
    selected.value = files.value.length ? files.value[0].path : ''
    status.value = files.value.length ? `✅ 共 ${files.value.length} 个文件（含子目录）` : '⚠️ 目录为空'
  } catch (e) {
    status.value = `❌ ${e.message}`
  } finally {
    loading.value = false
  }
}

/**
 * 主下载：fetch 流式读取 + 前端实时进度条 + Blob 触发保存。
 * 不依赖 Content-Disposition 头、不直接导航到 /api/download URL，
 * 规避 ms.show 等代理环境下「<a download> 被拦截 → 浏览器提示没有权限」。
 */
async function doDownload() {
  if (!selected.value || downloading.value) return
  const url = downloadUrl(project.projectDir, selected.value)
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
    const objUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objUrl
    a.download = selected.value.split('/').pop()
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(objUrl)
  } catch (e) {
    alert(`下载失败: ${e.message}`)
  } finally {
    downloading.value = false
    progress.value = 0
  }
}

onMounted(refresh)
// 项目目录变化（含重新加载后状态恢复）时自动刷新文件列表
watch(() => project.projectDir, () => refresh())
</script>

<template>
  <div>
    <p class="form-hint">下载项目目录中的文件（含子目录，相对路径显示为「子目录/文件名」）</p>
    <button class="btn btn--block" :disabled="loading" @click="refresh">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
      刷新文件列表
    </button>
    <div v-if="status" class="form-hint" style="margin:8px 0" :style="{ color: status.startsWith('✅') ? 'var(--success)' : status.startsWith('❌') ? 'var(--danger)' : 'inherit' }">{{ status }}</div>

    <template v-if="files.length">
      <div class="form-group" style="margin-top:8px">
        <label>选择文件</label>
        <div class="dl-list">
          <div class="dl-list__inner">
            <div
              v-for="f in files"
              :key="f.path"
              class="dl-list__item"
              :class="{ 'dl-list__item--active': f.path === selected }"
              :title="f.path"
              @click="selected = f.path"
            >
              <span class="dl-list__path">{{ f.path }}</span>
              <span class="dl-list__size">{{ fmtSize(f.size) }}</span>
            </div>
          </div>
        </div>
      </div>
      <button
        v-if="selected"
        class="btn btn--primary btn--block"
        :disabled="downloading"
        @click="doDownload"
      >{{ downloading ? `⬇️ 下载中… ${progress}%` : `⬇️ 点击下载：${selected}` }}</button>
      <div v-if="downloading" class="dl-progress">
        <div class="dl-progress__bar" :style="{ width: progress + '%' }"></div>
      </div>
      <a
        v-if="selected"
        class="btn btn--ghost btn--sm"
        style="margin-top:6px"
        :href="downloadUrl(project.projectDir, selected)"
        download
      >浏览器直接下载（备用，无进度）</a>
      <p class="form-hint" style="margin-top:6px">
        点击主按钮后前端会显示实时下载进度，下载完成自动保存到浏览器下载目录，若提示「无法下载」可尝试下方备用方式
      </p>
      <code class="dl-url">{{ downloadUrl(project.projectDir, selected) }}</code>
    </template>
  </div>
</template>

<style scoped>
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
.dl-list__item:hover { background: #eef0f5; }
.dl-list__item--active { background: var(--primary-soft); color: var(--primary); }
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
