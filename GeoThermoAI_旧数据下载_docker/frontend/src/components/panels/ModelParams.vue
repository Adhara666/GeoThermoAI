<script setup>
import { ref, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'

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
      <label>机器学习方法</label>
      <input class="form-input" :value="'Random Forest'" disabled />
    </div>
    <div class="form-group">
      <label>n_estimators（决策树数量）：{{ params.n_estimators }}</label>
      <input v-model.number="params.n_estimators" type="range" min="50" max="1000" step="50" class="form-range" style="width:100%" />
    </div>
    <div class="form-group">
      <label>max_depth（最大深度）：{{ params.max_depth }}</label>
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
    <button class="btn btn--primary btn--block" @click="save">💾 保存参数</button>
  </div>
</template>
