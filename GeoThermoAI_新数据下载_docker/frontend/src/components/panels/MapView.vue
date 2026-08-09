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
  // 缩放控件放到左下角，避免与左上角的图层控制面板重叠
  L.control.zoom({ position: 'bottomleft' }).addTo(map)
  map.setView([30.59, 114.3], 10)
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

onMounted(() => {
  initMap()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (map) { map.remove(); map = null }
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
        <div class="layer-panel__title">图层控制</div>
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
  position: absolute; left: 10px; top: 10px; width: 360px; max-height: calc(100% - 20px);
  background: rgba(255, 255, 255, 0.97); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: var(--shadow); padding: 10px; overflow-y: auto; z-index: 500;
}
.layer-panel__title { font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 8px; }
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
</style>
