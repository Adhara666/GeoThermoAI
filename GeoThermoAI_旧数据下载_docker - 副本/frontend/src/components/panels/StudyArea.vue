<script setup>
import { useProjectStore } from '../../stores/project'

const project = useProjectStore()

function onUpload(e) {
  const files = e.target.files
  if (files && files.length) project.uploadStudyArea(files)
  e.target.value = ''
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
      <span class="field-label">已上传研究区</span>
      <div class="layer-list">
        <div v-for="(f, i) in project.studyAreas" :key="f" class="layer-item">
          <span class="dot" :class="i === 0 ? 'dot--yes' : 'dot--no'"></span>
          <span :style="{ fontWeight: i === 0 ? 600 : 'normal' }">{{ f }}</span>
          <span v-if="i === 0" class="tag tag--success" style="margin-left:auto">当前使用</span>
        </div>
      </div>
    </div>
  </div>
</template>
