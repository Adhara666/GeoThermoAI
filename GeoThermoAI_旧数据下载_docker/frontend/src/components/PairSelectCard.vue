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
</script>

<template>
  <div class="pair-card">
    <div class="pair-card__title">📋 找到 {{ chat.pairs.length }} 组影像配对，请选择一组</div>
    <div class="pair-options">
      <label v-for="(p, i) in chat.pairs" :key="i" class="pair-option" :class="{ 'pair-option--selected': selected === i }">
        <input type="radio" :value="i" v-model="selected" />
        <span class="pair-option__text">{{ i + 1 }}. {{ pairText(p) }}</span>
      </label>
    </div>
    <div class="pair-card__actions">
      <button class="btn btn--primary" @click="confirm">✅ 确认选择</button>
    </div>
  </div>
</template>
