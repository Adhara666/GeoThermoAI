<script setup>
import { ref, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'
import { t } from '../../i18n'

const settings = useSettingsStore()
const params = ref({ n_estimators: 200, max_depth: 25, min_samples_split: 16, min_samples_leaf: 8 })

watch(
  () => settings.modelParams,
  (p) => {
    if (p && Object.keys(p).length) params.value = { ...params.value, ...p }
  },
  { immediate: true },
)

async function save() {
  await settings.saveModelParams({
    n_estimators: Number(params.value.n_estimators) || 200,
    max_depth: Number(params.value.max_depth) || 25,
    min_samples_split: Number(params.value.min_samples_split) || 16,
    min_samples_leaf: Number(params.value.min_samples_leaf) || 8,
  })
}
</script>

<template>
  <div>
    <div class="form-group">
      <label>{{ t('mp.method') }}</label>
      <input class="form-input" :value="'Random Forest'" disabled />
    </div>
    <div class="form-group">
      <label>{{ t('mp.ntrees', { n: params.n_estimators }) }}</label>
      <input v-model.number="params.n_estimators" type="range" min="50" max="1000" step="50" class="form-range" style="width:100%" />
    </div>
    <div class="form-group">
      <label>{{ t('mp.maxdepth', { n: params.max_depth }) }}</label>
      <input v-model.number="params.max_depth" type="range" min="5" max="50" step="1" class="form-range" style="width:100%" />
    </div>
    <div class="form-group">
      <label>min_samples_split：{{ params.min_samples_split }}</label>
      <input v-model.number="params.min_samples_split" type="range" min="2" max="50" step="1" class="form-range" style="width:100%" />
    </div>
    <div class="form-group">
      <label>min_samples_leaf：{{ params.min_samples_leaf }}</label>
      <input v-model.number="params.min_samples_leaf" type="range" min="1" max="20" step="1" class="form-range" style="width:100%" />
    </div>
    <button class="btn btn--primary btn--block" @click="save">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
      {{ t('mp.save') }}
    </button>
  </div>
</template>

<style scoped>
/* 滑动条颜色与「保存参数」按钮一致（--primary 蓝） */
.form-range { accent-color: var(--primary); }
</style>
