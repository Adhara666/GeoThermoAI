<script setup>
import { ref, onBeforeUnmount } from 'vue'
import { api } from '../../api'
import StatusResult from '../StatusResult.vue'

const testing = ref('')
const result = ref('')
let resultTimer = null

// timeout：结果框显示时长（毫秒），超时后自动清空
async function runTest(key, path, timeout) {
  testing.value = key
  result.value = ''
  clearTimeout(resultTimer)
  try {
    const r = await api.post(path)
    result.value = r.result || '（空结果）'
  } catch (e) {
    result.value = `❌ 测试失败：${e.message}`
  } finally {
    testing.value = ''
    resultTimer = setTimeout(() => { result.value = '' }, timeout)
  }
}

// 前两个测试（数据源连接）5 秒、第三个（地理处理环境）8 秒
function testPlanetary() { runTest('planetary', '/api/test/planetary', 5000) }
function testCdse() { runTest('cdse', '/api/test/cdse', 5000) }
function testGdal() { runTest('gdal', '/api/test/gdal', 8000) }

onBeforeUnmount(() => clearTimeout(resultTimer))

// 测试页图标（与工作面板「测试」tab 同一图标）
const ZAP_ICON = '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>'
</script>

<template>
  <div class="data-source">
    <button class="btn btn--primary btn--block" :disabled="!!testing" @click="testPlanetary">
      <svg v-if="testing !== 'planetary'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" v-html="ZAP_ICON"></svg>
      <span v-else>测试中…</span>
      <span v-if="testing !== 'planetary'">测试 Planetary Computer 连接</span>
    </button>
    <button class="btn btn--primary btn--block" :disabled="!!testing" @click="testCdse">
      <svg v-if="testing !== 'cdse'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" v-html="ZAP_ICON"></svg>
      <span v-else>测试中…</span>
      <span v-if="testing !== 'cdse'">测试 Copernicus Data Space 连接</span>
    </button>
    <button class="btn btn--primary btn--block" :disabled="!!testing" @click="testGdal">
      <svg v-if="testing !== 'gdal'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" v-html="ZAP_ICON"></svg>
      <span v-else>测试中…</span>
      <span v-if="testing !== 'gdal'">测试地理处理环境</span>
    </button>
    <StatusResult :text="result" />
  </div>
</template>
