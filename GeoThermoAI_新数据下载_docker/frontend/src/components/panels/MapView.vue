<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useProjectStore } from '../../stores/project'
import { api, getToken } from '../../api'

const project = useProjectStore()
const conv = computed(() => project.currentConv)
const projectDir = computed(() => project.projectDir)

const mapEl = ref(null)
const panelOpen = ref(true)
const layers = ref([]) // [{id,label,group,available,visible,opacity,bounds}]
const currentBase = ref('gaode')

const BASE_DEFS = [
  { id: 'gaode', label: '街道地图', url: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', maxZoom: 18, attr: '© 高德地图', subdomains: '1234' },
  { id: 'gaode_sat', label: '卫星影像', url: 'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}', maxZoom: 18, attr: '© 高德地图', subdomains: '1234' },
  { id: 'esri', label: 'Esri 卫星', url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', maxZoom: 18, attr: '© Esri' },
]

let map = null
let baseLayer = null
const overlayMap = {} // id -> L.TileLayer

function tileUrl(id, ts) {
  // 瓦片由 <img> 加载，无法携带 Header，鉴权 token 走查询参数
  return `/api/layer/${encodeURIComponent(id)}/tile/{z}/{x}/{y}?conv=${encodeURIComponent(conv.value || '')}&t=${ts}&token=${encodeURIComponent(getToken())}`
}

function setBase(id) {
  currentBase.value = id
  if (!map) return
  if (baseLayer) map.removeLayer(baseLayer)
  const def = BASE_DEFS.find((b) => b.id === id)
  if (!def) return
  baseLayer = L.tileLayer(def.url, { maxZoom: def.maxZoom, attribution: def.attr, subdomains: def.subdomains || 'abc' })
  baseLayer.addTo(map)
  // 升级点 18：底图置于最底层，避免切换底图时盖住已勾选的数据图层
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
}

function removeOverlays() {
  if (!map) return
  for (const id in overlayMap) {
    if (overlayMap[id]) map.removeLayer(overlayMap[id])
    delete overlayMap[id]
  }
}

async function refresh() {
  if (!map) return
  if (!conv.value) { removeOverlays(); layers.value = []; return }
  let list
  try {
    const r = await api.get(`/api/layers?conv=${encodeURIComponent(conv.value)}`)
    list = r.layers || []
  } catch (_) {
    return // 请求失败：保留当前图层，避免瞬时断连导致图层闪空
  }

  removeOverlays()
  const ts = Date.now()
  layers.value = list.map((l) => ({
    ...l,
    visible: !!l.visible,
    opacity: typeof l.opacity === 'number' ? l.opacity : 0.7,
  }))

  let fitted = false
  for (const l of layers.value) {
    if (!l.available || !l.bounds) continue
    // 瓦片金字塔渲染：按原生分辨率加载，bounds 限定图层地理范围
    const overlay = L.tileLayer(tileUrl(l.id, ts), {
      opacity: l.opacity,
      zIndex: 500, // 升级点 18：数据图层始终高于底图，切换底图不被覆盖
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

// 透明度滑条（0-100%）→ l.opacity（0-1），供 Leaflet setOpacity 使用
function onOpacity(l, val) {
  const pct = Number(val)
  if (!Number.isFinite(pct)) return
  l.opacity = Math.min(1, Math.max(0, pct / 100))
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
  const ids = checkedLstLayers.value.map((l) => l.id)
  if (!ids.length) {
    tempValues.value = {}
    return
  }
  try {
    const r = await api.post(
      `/api/lst-values?conv=${encodeURIComponent(conv.value || '')}`,
      { lat, lon, layers: ids },
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
const groups = computed(() => {
  const g = {}
  for (const l of layers.value) {
    if (!l.available) continue
    ;(g[l.group || '图层'] = g[l.group || '图层'] || []).push(l)
  }
  return g
})
</script>

<template>
  <div class="map-frame-wrap">
    <div class="map-toolbar">
      <button class="btn btn--sm" @click="refresh">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
        刷新
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
        {{ panelOpen ? '▸ 收起图层' : '◂ 图层控制' }}
      </button>
    </div>

    <div class="map-holder">
      <div ref="mapEl" class="map-canvas"></div>

      <div v-if="!hasAny" class="map-hint">
        <div>未发现可渲染的数据图层</div>
        <div class="map-hint__sub">请选择已设置项目目录的对话，并完成数据下载 / LST 生成后刷新</div>
      </div>

      <div v-if="panelOpen && hasAny" class="layer-panel">
        <div class="layer-panel__title">
          <span>图层控制</span>
          <button
            v-if="lstLayers.length"
            class="temp-btn"
            :class="{ 'temp-btn--active': tempMode }"
            :title="tempMode ? '关闭温度显示，恢复地图操作' : '显示鼠标所在像元在各 LST 图层上的温度'"
            @click="toggleTempMode"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
            {{ tempMode ? '关闭温度' : '显示温度' }}
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
              <span class="layer-row__label" :title="l.label">{{ l.label || l.id }}</span>
            </label>
            <input
              type="range"
              class="layer-row__opacity"
              min="0" max="100"
              :value="Math.round(l.opacity * 100)"
              :title="`透明度 ${Math.round(l.opacity * 100)}%`"
              @input="onOpacity(l, $event.target.value)"
            />
            <span class="layer-row__pct">{{ Math.round(l.opacity * 100) }}%</span>
          </div>
        </div>
      </div>

      <div v-if="tempMode" class="temp-panel">
        <div class="temp-panel__head">
          <span class="temp-panel__ttl">像元温度</span>
          <span v-if="tempLocked" class="temp-panel__lock">已锁定</span>
        </div>
        <div v-if="showCoord" class="temp-panel__coord">{{ coordText }}</div>
        <div v-if="checkedLstLayers.length" class="temp-panel__rows">
          <div v-for="l in checkedLstLayers" :key="l.id" class="temp-panel__row">
            <span class="temp-panel__name" :title="l.label">{{ l.label }}</span>
            <span class="temp-panel__val">{{ fmtTemp(showValues[l.id]) }}</span>
          </div>
        </div>
        <div v-else class="temp-panel__hint">请勾选至少一个LST图层</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.map-frame-wrap { flex: 1; min-height: 0; display: flex; flex-direction: column; }

.map-toolbar { display: flex; gap: 8px; padding: 10px; border-bottom: 1px solid var(--border); align-items: center; flex-wrap: wrap; }
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
/* 升级点 21：标签悬停或聚焦时完整展示图层名称（title 提示 + 悬停换行） */
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
