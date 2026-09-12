<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useProjectStore } from '../../stores/project'
import { useChatStore } from '../../stores/chat'
import { api, getToken } from '../../api'
import { t, mapLayerLabel } from '../../i18n'
import TaskResultSelect from './TaskResultSelect.vue'

const project = useProjectStore()
const chat = useChatStore()
const conv = computed(() => project.currentConv)
const projectDir = computed(() => project.projectDir)

const mapEl = ref(null)
const panelOpen = ref(true)
const layers = ref([]) // [{id,label,group,available,visible,opacity,bounds}]
const currentBase = ref('gaode')
const taskEmptyHint = ref('') // 选中任务但无产物时的空态提示（避免看似“坏掉”）

const BASE_DEFS = computed(() => [
  { id: 'gaode', label: t('map.street'), url: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', maxZoom: 18, attr: t('map.attrGaode'), subdomains: '1234' },
  { id: 'gaode_sat', label: t('map.satellite'), url: 'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}', maxZoom: 18, attr: t('map.attrGaode'), subdomains: '1234' },
  { id: 'esri', label: t('map.esri'), url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', maxZoom: 18, attr: '© Esri' },
])

let map = null
let baseLayer = null
const overlayMap = {} // id -> L.TileLayer
let outlineLayer = null // 研究区矢量轮廓图层（启用集叠显 + 选中任务高亮）

// 选中任务绑定的研究区名（任务卡快照字段 region）；无选中/无值返回空串
function selectedTaskRegion() {
  const tid = chat.activeTaskId
  if (!tid) return ''
  const tk = chat.kernelTasks ? chat.kernelTasks[tid] : null
  return (tk && tk.region) || ''
}

// 研究区轮廓：启用集全部叠显（蓝色虚线）；选中任务的区高亮（橙色实线加粗）
async function refreshOutlines() {
  if (!map) return
  let areas = []
  try {
    const r = await api.get('/api/study-areas/outlines')
    areas = r.areas || []
  } catch (_) {
    return // 拉取失败保留现有轮廓，避免闪空
  }
  if (outlineLayer) { map.removeLayer(outlineLayer); outlineLayer = null }
  if (!areas.length) return
  if (!map.getPane('studyOutlinePane')) {
    // 独立 pane 且 zIndex 高于栅格图层（500），保证轮廓不被瓦片盖住
    const pane = map.createPane('studyOutlinePane')
    pane.style.zIndex = 550
  }
  const sel = selectedTaskRegion()
  outlineLayer = L.layerGroup()
  for (const a of areas) {
    const isSel = !!sel && a.name === sel
    const gj = L.geoJSON(a.geojson, {
      pane: 'studyOutlinePane',
      interactive: false,
      style: {
        color: isSel ? '#e8590c' : '#1a73e8',
        weight: isSel ? 4 : 2,
        opacity: 0.95,
        fillColor: isSel ? '#ffa94d' : '#4dabf7',
        fillOpacity: isSel ? 0.16 : 0.05,
        dashArray: isSel ? null : '5,4',
      },
    })
    outlineLayer.addLayer(gj)
  }
  outlineLayer.addTo(map)
}

function tileUrl(l, ts) {
  // 瓦片由 <img> 加载，无法携带 Header，鉴权 token 走查询参数
  if (l && l.input_key && l.task_id) {
    // 任务原始输入层（10m S2 / 30m LST / DEM）：服务端解析路径
    return `/api/tasks/${encodeURIComponent(l.task_id)}/inputs/`
      + `${encodeURIComponent(l.input_key)}/tile/{z}/{x}/{y}`
      + `?t=${ts}&token=${encodeURIComponent(getToken())}`
  }
  if (l && l.artifact_id) {
    // 选中任务的产物：按产物文件直接渲染（§12.4 面板绑定同一产物）
    return `/api/artifacts/${encodeURIComponent(l.artifact_id)}/tile/{z}/{x}/{y}`
      + `?style=${encodeURIComponent(l.style || '')}&t=${ts}&token=${encodeURIComponent(getToken())}`
  }
  const id = (l && l.id) || l
  return `/api/layer/${encodeURIComponent(id)}/tile/{z}/{x}/{y}?conv=${encodeURIComponent(conv.value || '')}&t=${ts}&token=${encodeURIComponent(getToken())}`
}

function setBase(id) {
  currentBase.value = id
  if (!map) return
  if (baseLayer) map.removeLayer(baseLayer)
  const def = BASE_DEFS.value.find((b) => b.id === id)
  if (!def) return
  baseLayer = L.tileLayer(def.url, { maxZoom: def.maxZoom, attribution: def.attr, subdomains: def.subdomains || 'abc' })
  baseLayer.addTo(map)
  // 底图置于最底层，避免切换底图时盖住已勾选的数据图层
  baseLayer.bringToBack()
}

function initMap() {
  if (!mapEl.value) return
  if (map) { map.remove(); map = null }
  for (const k in overlayMap) delete overlayMap[k]

  map = L.map(mapEl.value, { zoomControl: false, attributionControl: true, zoom: 10 })
  // 缩放控件放到左下角，避免与左上角的图层控制面板重叠；比例尺紧随其下
  L.control.zoom({ position: 'bottomleft' }).addTo(map)
  L.control.scale({ position: 'bottomleft', imperial: false }).addTo(map)
  map.setView([30.59, 114.3], 10)
  map.on('mousemove', onMouseMove)
  map.on('click', onMapClick)
  setBase(currentBase.value)
  refresh()
  refreshOutlines()
}

function removeOverlays() {
  if (!map) return
  for (const id in overlayMap) {
    if (overlayMap[id]) map.removeLayer(overlayMap[id])
    delete overlayMap[id]
  }
}

// 请求序号保护：快速切换对话时，迟到的旧响应直接丢弃，避免覆盖新对话图层
let _refreshSeq = 0

async function refresh() {
  const seq = ++_refreshSeq
  if (!map) return
  if (chat.activeTaskId) {
    await refreshFromTask(chat.activeTaskId, seq)
    return
  }
  taskEmptyHint.value = ""
  if (!conv.value) { removeOverlays(); layers.value = []; refreshOutlines(); return }
  let list
  try {
    const r = await api.get(`/api/layers?conv=${encodeURIComponent(conv.value)}`)
    list = r.layers || []
  } catch (_) {
    return // 请求失败：保留当前图层，避免瞬时断连导致图层闪空
  }
  if (seq !== _refreshSeq) return // 已有更新的请求，丢弃本次旧响应

  removeOverlays()
  const ts = Date.now()
  layers.value = list.map((l) => ({
    ...l,
    visible: !!l.visible,
    opacity: typeof l.opacity === 'number' ? l.opacity : 0.7,
    opacityPct: Math.round(
      (typeof l.opacity === 'number' ? l.opacity : 0.7) * 100),
  }))

  let fitted = false
  for (const l of layers.value) {
    if (!l.available || !l.bounds) continue
    // 瓦片金字塔渲染：按原生分辨率加载，bounds 限定图层地理范围
    const overlay = L.tileLayer(tileUrl(l, ts), {
      opacity: l.opacity,
      zIndex: 500, // 数据图层始终高于底图，切换底图不被覆盖
      bounds: [
        [l.bounds[0][0], l.bounds[0][1]],
        [l.bounds[1][0], l.bounds[1][1]],
      ],
      minZoom: 0,
      maxNativeZoom: l.max_native_zoom || 14,
      maxZoom: 20,
      noWrap: true,
      tileSize: 256,
      keepBuffer: 2,
      updateWhenIdle: false,
    })
    overlayMap[l.id] = overlay
    if (l.visible) overlay.addTo(map)
    if (!fitted) {
      fitted = true
      map.fitBounds([[l.bounds[0][0], l.bounds[0][1]], [l.bounds[1][0], l.bounds[1][1]]])
    }
  }
  await refreshOutlines()
}

/** 选中任务：原始输入层（恢复升级前的 10m S2 等原生图层）+ 产物层；
 *  图层名用标准名（style_label），不再直接展示文件名。 */
async function refreshFromTask(tid, seq) {
  const list = []
  // 1) 原始输入层（任务输入，未产出的阶段也能看底图）
  try {
    const r = await api.get(`/api/tasks/${encodeURIComponent(tid)}/inputs`)
    for (const it of (r.inputs || [])) {
      list.push({
        id: `input:${it.key}`,
        task_id: tid,
        input_key: it.key,
        style: it.style_id,
        label: it.label,
        group: it.group || 'acquire',
        available: true,
        visible: true,
        opacity: it.key === 'sentinel2_path' ? 0.8 : 0.7,
        opacityPct: it.key === 'sentinel2_path' ? 80 : 70,
        // 30m LST 输入层参与“显示温度”（按样式的 DN→K 换算采样）
        is_lst: it.key === 'landsat_path',
        // 10m 层按服务端给出的原生缩放显示（避免超出后模糊）；缺省 14
        max_native_zoom: it.max_native_zoom || 14,
        bounds: [[it.bounds[1], it.bounds[0]], [it.bounds[3], it.bounds[2]]],
      })
    }
  } catch (_) { /* 无原始输入（如未走到准备节点）时跳过 */ }

  // 2) 任务产物层：只显示正式结果（keep_forever），中间产物不上图
  const arts = chat.activeTaskArtifacts.filter(
    (a) => a.type === 'geotiff' && a.retention_class === 'keep_forever')
  const metas = []
  for (const a of arts) {
    try {
      const meta = await api.get(`/api/artifacts/${a.id}/meta`)
      if (meta.ok && meta.bounds) metas.push({ art: a, meta })
    } catch (_) { /* 单个产物失败不影响其他 */ }
  }
  if (seq !== _refreshSeq) return
  removeOverlays()
  const ts = Date.now()
  for (const { art, meta } of metas) {
    list.push({
      id: `artifact:${art.id}`,
      artifact_id: art.id,
      style: meta.style,
      label: meta.label_dated || meta.style_label || meta.name,
      group: 'result',
      available: true,
      visible: true,
      opacity: 0.7,
      opacityPct: 70,
      // LST 产物层参与“显示温度”（lst_10m / lst_10m_filled）
      is_lst: meta.style === 'lst_10m' || meta.style === 'lst_10m_filled',
      max_native_zoom: meta.max_native_zoom || 14,
      bounds: [[meta.bounds[1], meta.bounds[0]], [meta.bounds[3], meta.bounds[2]]],
    })
  }
  layers.value = list
  taskEmptyHint.value = layers.value.length ? "" : t('map.noTaskLayers')
  let fitted = false
  for (const l of layers.value) {
    const overlay = L.tileLayer(tileUrl(l, ts), {
      opacity: l.opacity, zIndex: 500,
      bounds: [[l.bounds[0][0], l.bounds[0][1]], [l.bounds[1][0], l.bounds[1][1]]],
      minZoom: 0, maxNativeZoom: l.max_native_zoom || 14, maxZoom: 20,
      noWrap: true, tileSize: 256, keepBuffer: 2, updateWhenIdle: false,
    })
    overlayMap[l.id] = overlay
    if (l.visible) overlay.addTo(map)
    if (!fitted) {
      fitted = true
      map.fitBounds([[l.bounds[0][0], l.bounds[0][1]], [l.bounds[1][0], l.bounds[1][1]]])
    }
  }
  await refreshOutlines()
}

function onToggle(l) {
  if (!map || !overlayMap[l.id]) return
  if (l.visible) {
    overlayMap[l.id].setOpacity(l.opacity)
    overlayMap[l.id].addTo(map)
  } else {
    map.removeLayer(overlayMap[l.id])
  }
}

// 透明度滑条（0-100%）：显示值走独立的 opacityPct 字段（与滑条同源），
// 避免只更新图层而数字滞后；l.opacity（0-1）供 Leaflet setOpacity 使用
function onOpacity(l, ev) {
  const pct = Number(ev && ev.target ? ev.target.value : ev)
  if (!Number.isFinite(pct)) return
  const clamped = Math.min(100, Math.max(0, Math.round(pct)))
  l.opacityPct = clamped
  l.opacity = clamped / 100
  if (map && overlayMap[l.id]) overlayMap[l.id].setOpacity(l.opacity)
}

function onResize() {
  if (map) map.invalidateSize()
}

// ── 显示温度（像元采样） ─────────────────────────────────────────
const tempMode = ref(false)   // 是否激活"显示温度"（激活后锁定地图交互）
const tempLocked = ref(false) // 是否锁定当前像素（点击一次锁定，再点一次解锁）
const cursorPos = ref(null)   // 鼠标当前所在位置 {lat, lon}
const tempValues = ref({})    // layer_id -> 温度(K) 或 null（仅显示已勾选图层）
const lockInfo = ref(null)    // 锁定时固定的 {lat, lon, values}
let _lastQuery = 0

const lstLayers = computed(() => layers.value.filter((l) => l.available && l.is_lst))
const checkedLstLayers = computed(() => lstLayers.value.filter((l) => l.visible))

// 锁定时显示锁定的坐标与温度；解锁后跟随光标
const showCoord = computed(() => lockInfo.value || cursorPos.value)
const showValues = computed(() => (lockInfo.value ? lockInfo.value.values : tempValues.value) || {})

// 经纬度文本：东经/西经/北纬/南纬用 E/W/N/S 字母表示，如 "114.97639° E  30.42174° N"
const coordText = computed(() => {
  const c = showCoord.value
  if (!c) return ''
  const lonDir = c.lon >= 0 ? 'E' : 'W'
  const latDir = c.lat >= 0 ? 'N' : 'S'
  return `${Math.abs(c.lon).toFixed(5)}° ${lonDir}  ${Math.abs(c.lat).toFixed(5)}° ${latDir}`
})

function lockMapInteractions() {
  if (!map) return
  map.dragging.disable()
  map.touchZoom.disable()
  map.doubleClickZoom.disable()
  map.scrollWheelZoom.disable()
  map.boxZoom.disable()
  map.keyboard.disable()
  if (mapEl.value) mapEl.value.style.cursor = 'crosshair'
}

function unlockMapInteractions() {
  if (!map) return
  map.dragging.enable()
  map.touchZoom.enable()
  map.doubleClickZoom.enable()
  map.scrollWheelZoom.enable()
  map.boxZoom.enable()
  map.keyboard.enable()
  if (mapEl.value) mapEl.value.style.cursor = ''
}

function toggleTempMode() {
  tempMode.value = !tempMode.value
  if (!tempMode.value) {
    tempLocked.value = false
    lockInfo.value = null
    cursorPos.value = null
    tempValues.value = {}
    unlockMapInteractions()
  } else {
    lockMapInteractions()
  }
}

async function queryTemps(lat, lon) {
  const targets = checkedLstLayers.value
  if (!targets.length) {
    tempValues.value = {}
    return
  }
  // 入口协议：旧对话图层（字符串 id）与新任务图层（输入/产物对象）统一批量
  // 一次请求；服务端按各自样式换算为 K
  const payload = targets.map((l) => {
    if (l.input_key && l.task_id) {
      return { kind: 'input', id: l.id, task_id: l.task_id, key: l.input_key }
    }
    if (l.artifact_id) {
      return { kind: 'artifact', id: l.id, artifact_id: l.artifact_id }
    }
    return l.id
  })
  try {
    const r = await api.post(
      `/api/lst-values?conv=${encodeURIComponent(conv.value || '')}`,
      { lat, lon, layers: payload },
    )
    tempValues.value = r.values || {}
  } catch (_) {
    // 查询失败：保留旧值，避免移动时温度闪烁
  }
}

function onMouseMove(e) {
  if (!tempMode.value || !e.latlng) return
  // 锁定期间也记录光标最新位置（解锁后若光标在别的像素就显示那个像素），只是不刷新温度
  cursorPos.value = { lat: e.latlng.lat, lon: e.latlng.lng }
  if (tempLocked.value) return
  const now = Date.now()
  if (now - _lastQuery < 60) return // 60ms 节流，避免移动时高频请求
  _lastQuery = now
  queryTemps(cursorPos.value.lat, cursorPos.value.lon)
}

async function onMapClick(e) {
  if (!tempMode.value || !e.latlng) return
  if (tempLocked.value) {
    // 再点一次：解锁。解锁后立即按当前光标位置刷新坐标与温度
    tempLocked.value = false
    lockInfo.value = null
    if (cursorPos.value) await queryTemps(cursorPos.value.lat, cursorPos.value.lon)
    return
  }
  // 第一次点击：锁定当前像素的坐标与温度
  cursorPos.value = { lat: e.latlng.lat, lon: e.latlng.lng }
  await queryTemps(cursorPos.value.lat, cursorPos.value.lon)
  tempLocked.value = true
  lockInfo.value = { lat: cursorPos.value.lat, lon: cursorPos.value.lon, values: { ...tempValues.value } }
}

function fmtTemp(v) {
  if (v === undefined || v === null) return '–' // 无数据用短横线（比长横线细短）
  return `${Number(v).toFixed(2)} K`
}

onMounted(() => {
  initMap()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (map) {
    map.off('mousemove', onMouseMove)
    map.off('click', onMapClick)
    unlockMapInteractions()
    map.remove()
    map = null
  }
})

watch(conv, async () => {
  await nextTick()
  if (map) {
    map.invalidateSize()
    refresh()
  }
})

watch(projectDir, async () => {
  if (map) { map.invalidateSize(); refresh() }
})

// 选中任务变化（面板联动中枢）：地图切换到该任务的产物图层 + 研究区高亮
watch(() => chat.activeTaskId, () => { if (map) refresh() })
// 启用集变化：刷新研究区轮廓叠显
watch(() => (project.activeStudyAreas || []).join(','), () => refreshOutlines())

// 图层勾选/透明度变化时刷新温度采样（勾选新图层 → 立即补查该点温度）
watch(
  () => layers.value.map((l) => `${l.id}:${l.visible}`).join(','),
  async () => {
    if (tempMode.value && !tempLocked.value && cursorPos.value) {
      await queryTemps(cursorPos.value.lat, cursorPos.value.lon)
    }
  },
)

const hasAny = computed(() => layers.value.some((l) => l.available))

// 分组显示名：新任务图层用语言键（acquire/result），旧图层保留原文并做英文映射
function groupName(g) {
  if (g === 'acquire' || g === '数据获取') return t('map.grpAcquire')
  if (g === 'result' || g === '结果') return t('map.grpResult')
  return mapLayerLabel(g || '') || t('map.defaultGroup')
}

const groups = computed(() => {
  const g = {}
  for (const l of layers.value) {
    if (!l.available) continue
    const gname = groupName(l.group)
    // 必须保留源对象引用（响应式）：此前用 { ...l } 浅拷贝导致
    // 滑条/勾选修改的是副本，透明度数字等界面永远不更新
    ;(g[gname] = g[gname] || []).push(l)
  }
  return g
})
</script>

<template>
  <div class="map-frame-wrap">
    <TaskResultSelect class="map-result-select" />
    <p v-if="taskEmptyHint" class="form-hint map-empty-hint">
      {{ taskEmptyHint }}
    </p>
    <div class="map-toolbar">
      <button class="btn btn--sm" @click="refresh">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
        {{ t('map.refresh') }}
      </button>
      <div class="base-switch">
        <button
          v-for="b in BASE_DEFS"
          :key="b.id"
          class="base-switch__item"
          :class="{ 'base-switch__item--active': currentBase === b.id }"
          @click="setBase(b.id)"
        >{{ b.label }}</button>
      </div>
      <button class="btn btn--sm" @click="panelOpen = !panelOpen">
        {{ panelOpen ? t('map.hideLayers') : t('map.showLayers') }}
      </button>
    </div>

    <div class="map-holder">
      <div ref="mapEl" class="map-canvas"></div>

      <div v-if="!hasAny" class="map-hint">
        <div>{{ t('map.noLayers') }}</div>
        <div class="map-hint__sub">{{ t('map.noLayersSub') }}</div>
      </div>

      <div v-if="panelOpen && hasAny" class="layer-panel">
        <div class="layer-panel__title">
          <span>{{ t('map.layers') }}</span>
          <button
            v-if="lstLayers.length"
            class="temp-btn"
            :class="{ 'temp-btn--active': tempMode }"
            :title="tempMode ? t('map.hideTempTitle') : t('map.showTempTitle')"
            @click="toggleTempMode"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
            {{ tempMode ? t('map.hideTemp') : t('map.showTemp') }}
          </button>
        </div>
        <div v-for="(items, gname) in groups" :key="gname" class="layer-group">
          <div class="layer-group__name">{{ gname }}</div>
          <div v-for="l in items" :key="l.id" class="layer-row">
            <label class="layer-row__check">
              <input
                type="checkbox"
                v-model="l.visible"
                @change="onToggle(l)"
              />
              <span class="layer-row__label" :title="l.label">{{ mapLayerLabel(l.label) || l.label || l.id }}</span>
            </label>
            <input
              type="range"
              class="layer-row__opacity"
              min="0" max="100"
              :value="l.opacityPct"
              :title="t('map.opacityTitle', { pct: l.opacityPct })"
              @input="onOpacity(l, $event)"
            />
            <span class="layer-row__pct">{{ l.opacityPct }}%</span>
          </div>
        </div>
      </div>

      <div v-if="tempMode" class="temp-panel">
        <div class="temp-panel__head">
          <span class="temp-panel__ttl">{{ t('map.pixelTemp') }}</span>
          <span v-if="tempLocked" class="temp-panel__lock">{{ t('map.locked') }}</span>
        </div>
        <div v-if="showCoord" class="temp-panel__coord">{{ coordText }}</div>
        <div v-if="checkedLstLayers.length" class="temp-panel__rows">
          <div v-for="l in checkedLstLayers" :key="l.id" class="temp-panel__row">
            <span class="temp-panel__name" :title="l.label">{{ mapLayerLabel(l.label) || l.label }}</span>
            <span class="temp-panel__val">{{ fmtTemp(showValues[l.id]) }}</span>
          </div>
        </div>
        <div v-else class="temp-panel__hint">{{ t('map.noLst') }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.map-frame-wrap { flex: 1; min-height: 0; display: flex; flex-direction: column; }

/* 任务选择框间距/宽度/对齐：需带更高优先级前缀覆盖子组件作用域样式
   （.task-select{margin-bottom:10px}）；上边距与下方工具栏同取 14px */
.map-frame-wrap .map-result-select { margin: 14px 0 0 10px; width: min(560px, calc(100% - 20px)); }
/* 空态提示（无影像时）：与选择框同起线（10px），上间距 6px，不再用负边距（否则压盖选择框） */
.map-empty-hint { margin: 6px 10px 0; }
/* 选择框内部（子组件元素）：:deep 穿透作用域，带元素级前缀确保确定性生效 */
.map-frame-wrap :deep(.task-select__btn) { position: relative; }
.map-frame-wrap :deep(.task-select__label) { text-align: center; }
.map-frame-wrap :deep(.task-select__caret) {
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
}
.map-frame-wrap :deep(.task-select__caret--open) {
  transform: translateY(-50%) rotate(180deg);
}

.map-toolbar { display: flex; gap: 8px; padding: 14px 10px; border-bottom: 1px solid var(--border); align-items: center; flex-wrap: wrap; }
.base-switch { display: flex; gap: 2px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 2px; }
.base-switch__item { border: none; background: none; font-size: 12px; color: var(--text-secondary); padding: 3px 8px; border-radius: 4px; white-space: nowrap; }
.base-switch__item--active { background: var(--primary); color: #fff; }

.map-holder { flex: 1; min-height: 0; position: relative; }
.map-canvas { position: absolute; inset: 0; background: #dfe6ec; }

.map-hint {
  position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 6px; color: var(--text-muted); font-size: 13px; pointer-events: none;
  background: rgba(247, 248, 250, 0.55);
}
.map-hint__sub { font-size: 12px; }

.layer-panel {
  position: absolute; left: 10px; top: 10px; width: 400px; max-height: calc(100% - 20px);
  background: rgba(255, 255, 255, 0.97); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: var(--shadow); padding: 10px; overflow-y: auto; z-index: 500;
}
.layer-panel__title { font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 8px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.temp-btn {
  display: inline-flex; align-items: center; gap: 4px; margin-left: auto; flex-shrink: 0;
  border: 1px solid var(--border); background: var(--bg); color: var(--text-secondary);
  font-size: 11px; padding: 3px 8px; border-radius: 6px; cursor: pointer; white-space: nowrap;
  transition: all 0.15s;
}
.temp-btn:hover { border-color: var(--primary); color: var(--primary); }
.temp-btn--active { background: var(--primary); border-color: var(--primary); color: #fff; }
.layer-group { margin-bottom: 10px; }
.layer-group:last-child { margin-bottom: 0; }
.layer-group__name {
  font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.3px;
  margin-bottom: 4px;
}
.layer-row { display: flex; align-items: center; gap: 6px; padding: 3px 0; }
.layer-row__check { display: flex; align-items: center; gap: 6px; min-width: 0; flex: 1; cursor: pointer; }
.layer-row__check input { accent-color: var(--primary); margin: 0; flex-shrink: 0; }
.layer-row__label {
  flex: 1; min-width: 0; font-size: 13px; font-weight: 500; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 标签悬停或聚焦时完整展示图层名称（title 提示 + 悬停换行） */
.layer-row__label:hover { white-space: normal; word-break: break-all; }
.layer-row__opacity { flex: 0 0 56px; height: 14px; accent-color: var(--primary); }
.layer-row__pct { font-size: 11px; color: var(--text-muted); width: 30px; text-align: right; flex-shrink: 0; }

/* 显示温度小面板（右下角，避开左上角图层面板与底部 attribution，防止重叠） */
.temp-panel {
  position: absolute; right: 10px; bottom: 36px; min-width: 240px; max-width: 320px;
  background: rgba(255, 255, 255, 0.97); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: var(--shadow); padding: 10px 12px; z-index: 600; font-size: 12px;
}
.temp-panel__head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
.temp-panel__ttl { font-weight: 600; color: var(--text); }
.temp-panel__lock {
  font-size: 11px; color: #fff; background: var(--primary); border-radius: 4px;
  padding: 1px 6px; font-weight: 500;
}
.temp-panel__coord { color: var(--text-secondary); margin-bottom: 6px; font-variant-numeric: tabular-nums; }
.temp-panel__rows { display: flex; flex-direction: column; gap: 3px; max-height: 200px; overflow-y: auto; }
.temp-panel__row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.temp-panel__name { flex: 1; min-width: 0; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.temp-panel__val { width: 64px; text-align: center; font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; flex-shrink: 0; }
.temp-panel__hint { color: var(--warning, #b58900); }
</style>
