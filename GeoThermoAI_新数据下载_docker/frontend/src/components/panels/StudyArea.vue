<script setup>
import { ref } from 'vue'
import { useProjectStore } from '../../stores/project'

const project = useProjectStore()
const deleteTarget = ref('')

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

async function confirmDelete() {
  const name = deleteTarget.value
  deleteTarget.value = ''
  await project.deleteStudyArea(name)
}
</script>

<template>
  <div class="study-area">
    <p class="form-hint">上传研究区文件（GeoJSON / Shapefile），Agent 执行数据获取/全流程前需要使用研究区范围</p>
    <label class="file-drop">
      <input type="file" accept=".geojson,.json,.shp,.dbf,.shx,.prj" multiple style="display:none" @change="onUpload" />
      点击选择研究区文件
    </label>

    <div v-if="project.studyAreas.length" class="study-area__uploaded">
      <span class="field-label">已上传研究区（点击行切换当前使用）</span>
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
          <span v-if="f === project.currentStudyArea" class="tag tag--success" style="margin-left:auto">当前使用</span>
          <span v-else class="tag tag--muted" style="margin-left:auto">设为当前</span>
          <button class="layer-item__del" title="删除该研究区" @click.stop="onAskDelete(f)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
      </div>
    </div>

    <!-- 删除研究区确认弹窗（页面内弹窗，替代浏览器 confirm） -->
    <div v-if="deleteTarget" class="modal-mask" @click.self="deleteTarget = ''">
      <div class="modal-card modal-card--sm">
        <h3>删除研究区</h3>
        <p class="modal-text">确定删除研究区「{{ deleteTarget }}」？将同时删除该文件及其配套的 Shapefile（.shp/.dbf/.shx/.prj）文件，此操作不可撤销。</p>
        <div class="modal-actions">
          <button class="btn btn--danger" @click="confirmDelete">删除</button>
          <button class="btn btn--cancel" @click="deleteTarget = ''">取消</button>
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
