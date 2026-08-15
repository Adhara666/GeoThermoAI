<script setup>
import { ref, watch, onBeforeUnmount } from 'vue'
import { useProjectStore } from '../../stores/project'
import { t } from '../../i18n'
import StatusResult from '../StatusResult.vue'

const project = useProjectStore()
const deleteTarget = ref('')

// 上传后自动验证研究区是否可正常加载，验证结果短暂展示：
// - 保持当前页面：5 秒后自动消失
// - 切到工作面板其他页面再回来：组件随 v-if 卸载/重建，validationText 归零自然消失
const validationText = ref('')
let validationTimer = null
watch(
  () => project.lastValidation,
  (v) => {
    clearTimeout(validationTimer)
    if (v && v.length) {
      validationText.value = v
        .map((it) => `${it.level === 'ok' ? '✅' : it.level === 'warn' ? '⚠️' : '❌'} ${it.name}: ${it.message}`)
        .join('\n')
      validationTimer = setTimeout(() => { validationText.value = '' }, 5000)
    } else {
      validationText.value = ''
    }
  },
)

function onUpload(e) {
  const files = e.target.files
  if (files && files.length) project.uploadStudyArea(files)
  e.target.value = ''
}

// 点击行切换当前研究区
async function onSetCurrent(name) {
  if (name === project.currentStudyArea) return
  await project.setCurrentStudyArea(name)
}

function onAskDelete(name) {
  deleteTarget.value = name
}

// 删除确认文案按文件扩展名区分：Shapefile 才提示配套文件，
// 避免"上传的是 GeoJSON 却提示删除配套 .shp/.dbf/.shx/.prj"的歧义
function deleteConfirmText() {
  const name = deleteTarget.value || ''
  const ext = (name.split('.').pop() || '').toLowerCase()
  if (ext === 'shp') {
    return t('sa.delShp', { name })
  }
  return t('sa.delGeo', { name })
}

async function confirmDelete() {
  const name = deleteTarget.value
  deleteTarget.value = ''
  await project.deleteStudyArea(name)
}

onBeforeUnmount(() => clearTimeout(validationTimer))
</script>

<template>
  <div class="study-area">
    <p class="form-hint">{{ t('sa.hint') }}</p>
    <label class="file-drop">
      <input type="file" accept=".geojson,.json,.shp,.dbf,.shx,.prj" multiple style="display:none" @change="onUpload" />
      {{ t('sa.upload') }}
    </label>
    <StatusResult v-if="validationText" :text="validationText" />

    <div v-if="project.studyAreas.length" class="study-area__uploaded">
      <span class="field-label">{{ t('sa.uploaded') }}</span>
      <div class="layer-list">
        <div
          v-for="f in project.studyAreas"
          :key="f"
          class="layer-item"
          :class="{ 'layer-item--active': f === project.currentStudyArea }"
          :title="f"
          @click="onSetCurrent(f)"
        >
          <span class="dot" :class="f === project.currentStudyArea ? 'dot--yes' : 'dot--no'"></span>
          <span class="layer-item__name" :style="{ fontWeight: f === project.currentStudyArea ? 600 : 'normal' }">{{ f }}</span>
          <span v-if="f === project.currentStudyArea" class="tag tag--success" style="margin-left:auto">{{ t('sa.current') }}</span>
          <span v-else class="tag tag--muted" style="margin-left:auto">{{ t('sa.setCurrent') }}</span>
          <button class="layer-item__del" :title="t('sa.delTitle')" @click.stop="onAskDelete(f)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
      </div>
    </div>

    <!-- 删除研究区确认弹窗（页面内弹窗，替代浏览器 confirm） -->
    <div v-if="deleteTarget" class="modal-mask" @click.self="deleteTarget = ''">
      <div class="modal-card modal-card--sm">
        <h3>{{ t('sa.delModal') }}</h3>
        <p class="modal-text">{{ deleteConfirmText() }}</p>
        <div class="modal-actions">
          <button class="btn btn--danger" @click="confirmDelete">{{ t('sa.del') }}</button>
          <button class="btn btn--cancel" @click="deleteTarget = ''">{{ t('sa.cancel') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 当前使用行：浅蓝底高亮 */
.layer-item { cursor: pointer; border-radius: var(--radius-sm); }
.layer-item--active { background: var(--primary-soft); }
.layer-item__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.layer-item__del {
  flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; margin-left: 2px; border: none; border-radius: var(--radius-sm);
  background: transparent; color: var(--text-muted); cursor: pointer; transition: all 0.15s;
}
.layer-item__del:hover { color: var(--danger); background: #fdecec; }
</style>
