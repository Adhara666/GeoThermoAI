<script setup>
// 魔搭嵌入顶栏偏移量的人工校准控件（技术方案 附录C，实现期修订 v1.2）。
//
// 只在检测到嵌入环境时出现（右下角一个不起眼的小按钮，默认收起）。跨域 iframe 内部
// 无法自动判断顶栏真实高度（猜大了露灰边、猜小了裁切输入框，两种情况内部量出来的
// 间隙一样大，无法区分），只能靠人在真实页面上用肉眼确认。这个控件让确认之后的数值
// 直接保存到 localStorage，不需要每次手工拼 ?embedChrome= 参数。
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { currentEmbedChromePx, saveEmbedChromeOverride } from '../embedFit.js'

const STEP_PX = 4

const visible = ref(false)
const open = ref(false)
const chromePx = ref(0)
const saved = ref(false)

function refreshVisibility() {
  const fit = document.documentElement.dataset.embedFit || ''
  visible.value = fit.startsWith('modelscope')
  chromePx.value = currentEmbedChromePx()
}

function nudge(delta) {
  chromePx.value = Math.max(0, chromePx.value + delta)
  applyEmbedFitPreview()
}

function applyEmbedFitPreview() {
  // 先只影响本次会话的显示效果，点「保存」才落盘，避免误触就永久改掉配置
  const root = document.documentElement
  root.style.setProperty('--embed-chrome', `${chromePx.value}px`)
  const raw = window.visualViewport?.height || window.innerHeight || root.clientHeight || 800
  const height = Math.max(320, Math.floor(raw - chromePx.value))
  root.style.setProperty('--app-height', `${height}px`)
  saved.value = false
}

function save() {
  saveEmbedChromeOverride(chromePx.value)
  saved.value = true
}

function reset() {
  saveEmbedChromeOverride(null)
  chromePx.value = currentEmbedChromePx()
  saved.value = true
}

let timers = []
onMounted(() => {
  refreshVisibility()
  timers = [window.setTimeout(refreshVisibility, 100), window.setTimeout(refreshVisibility, 400)]
  window.addEventListener('resize', refreshVisibility)
})
onBeforeUnmount(() => {
  timers.forEach((t) => window.clearTimeout(t))
  window.removeEventListener('resize', refreshVisibility)
})
</script>

<template>
  <div v-if="visible" class="embed-calibrate">
    <button
      class="embed-calibrate__fab"
      type="button"
      title="校准嵌入顶栏偏移量"
      @click="open = !open"
    >⚙</button>

    <div v-if="open" class="embed-calibrate__panel">
      <div class="embed-calibrate__title">顶栏遮挡高度校准</div>
      <p class="embed-calibrate__hint">
        用下面的按钮微调，直到底部灰边刚好消失、输入框也没被顶栏裁切，再点保存。
      </p>
      <div class="embed-calibrate__row">
        <button type="button" @click="nudge(-STEP_PX)">−</button>
        <span class="embed-calibrate__value">{{ chromePx }} px</span>
        <button type="button" @click="nudge(STEP_PX)">＋</button>
      </div>
      <div class="embed-calibrate__actions">
        <button type="button" class="embed-calibrate__save" @click="save">
          {{ saved ? '已保存' : '保存' }}
        </button>
        <button type="button" class="embed-calibrate__reset" @click="reset">恢复默认</button>
      </div>
    </div>
  </div>
</template>
