<script setup>
import { ref } from 'vue'
import { api } from '../../api'

const testing = ref('')
const result = ref('')

async function testPlanetary() {
  testing.value = 'planetary'
  result.value = '测试中，请稍候…'
  try {
    const r = await api.post('/api/test/planetary')
    result.value = r.result || '（空结果）'
  } catch (e) {
    result.value = `❌ 测试失败：${e.message}`
  } finally {
    testing.value = ''
  }
}

async function testGdal() {
  testing.value = 'gdal'
  result.value = '测试中，请稍候…'
  try {
    const r = await api.post('/api/test/gdal')
    result.value = r.result || '（空结果）'
  } catch (e) {
    result.value = `❌ 测试失败：${e.message}`
  } finally {
    testing.value = ''
  }
}
</script>

<template>
  <div class="data-source">
    <p class="form-hint">数据源：Microsoft Planetary Computer（Landsat 8/9 L2、Sentinel-2 L2A、Copernicus DEM），通过 STAC API 自动搜索下载</p>
    <button class="btn btn--primary btn--block" :disabled="!!testing" @click="testPlanetary">
      {{ testing === 'planetary' ? '测试中…' : '🔌 测试 Planetary Computer 连接' }}
    </button>
    <button class="btn btn--block" :disabled="!!testing" @click="testGdal">
      {{ testing === 'gdal' ? '测试中…' : '🧪 测试 GDAL 环境' }}
    </button>
    <pre v-if="result" class="result-box">{{ result }}</pre>
  </div>
</template>
