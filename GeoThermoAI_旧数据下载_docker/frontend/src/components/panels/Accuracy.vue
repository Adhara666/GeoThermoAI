<script setup>
import { ref, watch } from 'vue'
import { useProjectStore } from '../../stores/project'
import { api } from '../../api'

const project = useProjectStore()
const rows = ref([])
const loading = ref(false)

async function refresh() {
  if (!project.currentConv) return
  loading.value = true
  try {
    const r = await api.get(`/api/accuracy?conv=${encodeURIComponent(project.currentConv)}`)
    rows.value = r.rows || []
  } catch (_) {
    rows.value = []
  } finally {
    loading.value = false
  }
}

watch(() => project.currentConv, refresh, { immediate: true })
</script>

<template>
  <div>
    <button class="btn btn--block" :disabled="loading" @click="refresh">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
      刷新精度
    </button>
    <table v-if="rows.length" class="metric-table" style="margin-top:10px">
      <tbody>
        <tr v-for="r in rows" :key="r.key">
          <td>{{ r.key }}</td>
          <td>{{ r.value }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="form-hint" style="margin-top:10px">暂无精度数据（需先完成全流程并生成 spatial_consistency 结果）</p>
  </div>
</template>
