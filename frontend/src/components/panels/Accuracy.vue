<script setup>
import { ref, watch, computed } from 'vue'
import { useProjectStore } from '../../stores/project'
import { api } from '../../api'
import { t, trServer } from '../../i18n'

const project = useProjectStore()
const loading = ref(false)
const test = ref({ status: 'missing' })
const closure = ref({ status: 'missing' })

const hasTest = computed(() => test.value.status === 'ok' && !!test.value.data?.metrics)
const hasClosure = computed(() => closure.value.status === 'ok')
const hasAny = computed(() => hasTest.value || hasClosure.value)

/** 闭合对照的 30m/10m 值域与填洞产物值域（填补产物未生成时为 null） */
const vr = computed(() =>
  closure.value.status === 'ok' ? (closure.value.data?.value_range || {}) : {})
const filledRange = computed(() =>
  closure.value.status === 'ok' ? (closure.value.data?.filled_range || null) : null)

async function refresh() {
  if (!project.currentConv) return
  loading.value = true
  try {
    const r = await api.get(`/api/accuracy?conv=${encodeURIComponent(project.currentConv)}`)
    test.value = r.test_metrics || { status: 'missing' }
    closure.value = r.coarse_constraint_closure || { status: 'missing' }
  } catch (_) {
    // 失败时保留旧数据：不重置为 missing，避免面板闪回"暂无精度数据"
    //（如后处理刚完成时接口首次全图读填洞产物较慢而短暂失败）
    if (test.value.status === 'missing' && !test.value.data) test.value = { status: 'error', message: t('acc.loadFailed') }
    if (closure.value.status === 'missing' && !closure.value.data) closure.value = { status: 'error', message: t('acc.loadFailed') }
  } finally {
    loading.value = false
  }
}

watch(() => project.currentConv, refresh, { immediate: true })

function fmt(v, digits = 4) {
  return typeof v === 'number' ? v.toFixed(digits) : '—'
}
function fmtSigned(v, digits = 4) {
  if (typeof v !== 'number') return '—'
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(digits)}`
}
function fmtRange(lo, hi, digits = 4) {
  if (typeof lo !== 'number' || typeof hi !== 'number') return '—'
  return `${lo.toFixed(digits)} – ${hi.toFixed(digits)} K`
}
</script>

<template>
  <div>
    <button class="btn btn--block" :disabled="loading" @click="refresh">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
      {{ t('acc.refresh') }}
    </button>

    <p v-if="!hasAny" class="form-hint" style="margin-top:10px">
      {{ loading ? t('acc.loading') : t('acc.none') }}
    </p>

    <div v-if="hasTest" class="acc-section">
      <div class="acc-section__title">{{ t('acc.testSet') }}</div>
      <p v-if="test.status === 'error'" class="form-hint" style="color:var(--danger)">{{ t('acc.errPrefix', { msg: trServer(test.message) }) }}</p>
      <table v-else class="metric-table">
        <tbody>
          <tr><td>R²</td><td>{{ test.data.metrics.r2_null_reason ? '—' : fmt(test.data.metrics.R2) }}</td></tr>
          <tr><td>MAE (K)</td><td>{{ fmt(test.data.metrics.MAE) }}</td></tr>
          <tr><td>RMSE (K)</td><td>{{ fmt(test.data.metrics.RMSE) }}</td></tr>
          <tr><td>{{ t('acc.mb') }}</td><td>{{ fmtSigned(test.data.metrics.MB) }}</td></tr>
          <tr><td>{{ t('acc.samples') }}</td><td>{{ test.data.n_samples ?? '—' }}</td></tr>
        </tbody>
      </table>
    </div>

    <div v-if="hasClosure" class="acc-section">
      <div class="acc-section__title">{{ t('acc.closure') }}</div>
      <p v-if="closure.status === 'error'" class="form-hint" style="color:var(--danger)">{{ t('acc.errPrefix', { msg: trServer(closure.message) }) }}</p>
      <template v-else>
        <div class="acc-subtitle">{{ t('acc.subClosure') }}</div>
        <table class="metric-table">
          <tbody>
            <tr><td>{{ t('acc.minDiff') }}</td><td>{{ fmtSigned(vr.low_end_difference_K) }} K</td></tr>
            <tr><td>{{ t('acc.maxDiff') }}</td><td>{{ fmtSigned(vr.high_end_difference_K) }} K</td></tr>
            <tr><td>{{ t('acc.range30') }}</td><td>{{ fmtRange(vr.min_30m_K, vr.max_30m_K) }}</td></tr>
            <tr><td>{{ t('acc.range10') }}</td><td>{{ fmtRange(vr.min_10m_K, vr.max_10m_K) }}</td></tr>
            <tr v-if="filledRange"><td>{{ t('acc.range10f') }}</td><td>{{ fmtRange(filledRange.min_K, filledRange.max_K) }}</td></tr>
          </tbody>
        </table>
      </template>
    </div>
  </div>
</template>

<style scoped>
.acc-section { margin-top: 16px; }
.acc-section__title { font-size: 13px; font-weight: 600; color: var(--text); margin-bottom: 6px; }
.acc-subtitle { font-size: 12px; color: var(--text-muted); margin: 10px 0 4px; }
</style>
