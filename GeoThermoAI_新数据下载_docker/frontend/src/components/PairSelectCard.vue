<script setup>
import { ref } from 'vue'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const selected = ref(0)

function confirm() {
  chat.resume(selected.value)
}

function pairText(p) {
  const s = p.sentinel2_date || p.sentinel_date || '?'
  const sc = p.sentinel2_coverage || p.sentinel_coverage || '?'
  const scn = p.sentinel2_count || p.sentinel_count || '?'
  return `Landsat ${p.landsat_satellite || '?'} ${p.landsat_date || '?'}（${p.landsat_count || '?'} 景, 覆盖 ${p.landsat_coverage || '?'}%）＋ Sentinel ${s}（${scn} 景, 覆盖 ${sc}%）`
}

// 云量有效判断（字段可能缺失或为 '?'）
function hasCloud(v) {
  return v !== undefined && v !== null && v !== '' && v !== '?'
}

// 将每景云量转成 {label, cloud} 渲染行
function scenesRows(scenes) {
  if (!Array.isArray(scenes)) return []
  return scenes.map((s) => ({
    label: `${s.satellite || '?'} ${s.date || '?'}`,
    cloud: s.cloud_cover,
  }))
}
</script>

<template>
  <div class="pair-card">
    <div class="pair-card__title">📋 找到 {{ chat.pairs.length }} 组影像配对，请选择一组</div>
    <div class="pair-options">
      <label v-for="(p, i) in chat.pairs" :key="i" class="pair-option" :class="{ 'pair-option--selected': selected === i }">
        <input type="radio" :value="i" v-model="selected" />
        <span class="pair-option__text">{{ i + 1 }}. {{ pairText(p) }}</span>
        <div class="pair-option__detail">
          <div v-if="hasCloud(p.landsat_cloud)" class="scene-row">
            <span>Landsat 平均云量</span><span>{{ p.landsat_cloud }}%</span>
          </div>
          <div v-for="(s, j) in scenesRows(p.landsat_scenes)" :key="'l' + j" class="scene-row">
            <span>{{ s.label }}</span><span>云量 {{ hasCloud(s.cloud) ? s.cloud + '%' : '未知' }}</span>
          </div>
          <div v-if="hasCloud(p.sentinel_cloud)" class="scene-row">
            <span>Sentinel 平均云量</span><span>{{ p.sentinel_cloud }}%</span>
          </div>
          <div v-for="(s, j) in scenesRows(p.sentinel_scenes)" :key="'s' + j" class="scene-row">
            <span>{{ s.label }}</span><span>云量 {{ hasCloud(s.cloud) ? s.cloud + '%' : '未知' }}</span>
          </div>
        </div>
      </label>
    </div>
    <div class="pair-card__actions">
      <button class="btn btn--primary" @click="confirm">✅ 确认选择</button>
    </div>
  </div>
</template>
