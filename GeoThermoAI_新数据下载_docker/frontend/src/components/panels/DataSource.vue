<script setup>
import { ref, computed } from 'vue'
import { api } from '../../api'

const testing = ref('')
const result = ref('')

async function runTest(key, path) {
  testing.value = key
  result.value = ''
  try {
    const r = await api.post(path)
    result.value = r.result || '（空结果）'
  } catch (e) {
    result.value = `❌ 测试失败：${e.message}`
  } finally {
    testing.value = ''
  }
}

function testPlanetary() { runTest('planetary', '/api/test/planetary') }
function testCdse() { runTest('cdse', '/api/test/cdse') }
function testGdal() { runTest('gdal', '/api/test/gdal') }

// 测试页图标（与工作面板「测试」tab 同一图标）
const ZAP_ICON = '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>'

// 测试结果行解析：按状态符号着色并替换为 SVG 图标（升级点 25）
const resultLines = computed(() => {
  if (!result.value) return []
  // 行尾句号不显示（升级点：测试说明行末尾不再加「。」）
  const noEndPeriod = (s) => s.replace(/。$/, '')
  return String(result.value)
    .split('\n')
    .map((line) => {
      const t = line.trim()
      if (!t || t === '---') return null // 删除旧的横线分隔符
      if (t.startsWith('✅')) return { type: 'ok', text: noEndPeriod(t.replace(/^✅\s*/, '')) }
      if (t.startsWith('⚠️')) return { type: 'warn', text: noEndPeriod(t.replace(/^⚠️\s*/, '')) }
      if (t.startsWith('❌')) return { type: 'fail', text: noEndPeriod(t.replace(/^❌\s*/, '')) }
      return { type: 'plain', text: noEndPeriod(t) }
    })
    .filter(Boolean)
})

// 状态图标（与整体线性图标风格一致，替换 emoji）
const STATUS_ICONS = {
  ok: '<path d="M20 6L9 17l-5-5"/>',
  warn: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  fail: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
}
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
    <div v-if="resultLines.length" class="test-result">
      <div
        v-for="(l, i) in resultLines"
        :key="i"
        class="test-result__row"
        :class="l.type === 'ok' ? 'test-result__row--ok' : (l.type === 'warn' ? 'test-result__row--warn' : (l.type === 'fail' ? 'test-result__row--fail' : 'test-result__row--plain'))"
      >
        <svg v-if="STATUS_ICONS[l.type]" class="test-result__icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" v-html="STATUS_ICONS[l.type]"></svg>
        <span>{{ l.text }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.test-result {
  margin-top: 4px; padding: 10px 12px; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  display: flex; flex-direction: column; gap: 5px;
}
.test-result__row { display: flex; align-items: flex-start; gap: 7px; font-size: 12.5px; line-height: 1.6; color: var(--text); word-break: break-all; }
.test-result__icon { flex-shrink: 0; margin-top: 3px; }
.test-result__row--ok { color: var(--success); }
.test-result__row--warn { color: var(--warning); }
.test-result__row--fail { color: var(--danger); }
.test-result__row--plain { color: var(--text-secondary); }
</style>
