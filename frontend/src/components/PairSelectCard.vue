<script setup>
// 影像配对选择卡片
// 数据 Agent 打过分时：最优且未尝试过的一组带「（推荐）」并默认选中，但决定权完全在用户，系统不代选。
// 已尝试过的组合带「已尝试」标记且不再带推荐；全部已尝试时不作推荐。
import { computed, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'
import { t } from '../i18n'

const chat = useChatStore()

const recommendedIndex = computed(() => {
  const i = chat.pairs.findIndex((p) => p.recommended)
  return i >= 0 ? i : 0
})

const allTried = computed(() =>
  chat.pairs.length > 0 && chat.pairs.every((p) => p.tried)
)

const selected = ref(recommendedIndex.value)

watch(recommendedIndex, (i) => { selected.value = i }, { immediate: true })

function confirm() {
  chat.resume(selected.value)
}

function pairText(p) {
  const s = p.sentinel2_date || p.sentinel_date || '?'
  const sc = p.sentinel2_coverage || p.sentinel_coverage || '?'
  const scn = p.sentinel2_count || p.sentinel_count || '?'
  return t('pair.text', {
    ls: p.landsat_satellite || '?',
    ld: p.landsat_date || '?',
    lc: p.landsat_count || '?',
    lco: p.landsat_coverage || '?',
    ss: s,
    sc: scn,
    sco: sc,
  })
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
    <div class="pair-card__title">{{ t('pair.title', { n: chat.pairs.length }) }}</div>
    <div v-if="allTried" class="pair-card__hint">{{ t('pair.allTried') }}</div>
    <div class="pair-options">
      <label v-for="(p, i) in chat.pairs" :key="i" class="pair-option" :class="{ 'pair-option--selected': selected === i }">
        <input type="radio" :value="i" v-model="selected" />
        <span class="pair-option__text">
          {{ i + 1 }}. {{ pairText(p) }}
          <span v-if="p.recommended" class="pair-option__badge">{{ t('pair.recommended') }}</span>
          <span v-else-if="p.tried" class="pair-option__badge pair-option__badge--tried">{{ t('pair.tried') }}</span>
        </span>
        <div v-if="p.recommend_reason" class="pair-option__reason">
          {{ p.recommend_reason }}
        </div>
        <div class="pair-option__detail">
          <div v-if="hasCloud(p.landsat_cloud)" class="scene-row">
            <span>{{ t('pair.lstCloud') }}</span><span>{{ p.landsat_cloud }}%</span>
          </div>
          <div v-for="(s, j) in scenesRows(p.landsat_scenes)" :key="'l' + j" class="scene-row">
            <span>{{ s.label }}</span><span>{{ hasCloud(s.cloud) ? t('pair.cloud', { c: s.cloud + '%' }) : t('pair.cloudUnknown') }}</span>
          </div>
          <div v-if="hasCloud(p.sentinel_cloud)" class="scene-row">
            <span>{{ t('pair.senCloud') }}</span><span>{{ p.sentinel_cloud }}%</span>
          </div>
          <div v-for="(s, j) in scenesRows(p.sentinel_scenes)" :key="'s' + j" class="scene-row">
            <span>{{ s.label }}</span><span>{{ hasCloud(s.cloud) ? t('pair.cloud', { c: s.cloud + '%' }) : t('pair.cloudUnknown') }}</span>
          </div>
        </div>
      </label>
    </div>
    <div class="pair-card__actions">
      <button class="btn btn--primary" @click="confirm">{{ t('pair.confirm') }}</button>
    </div>
  </div>
</template>
