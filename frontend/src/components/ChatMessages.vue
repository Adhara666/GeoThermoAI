<script setup>
import { ref, watch, nextTick, computed, onMounted, onBeforeUnmount } from 'vue'
import { useChatStore } from '../stores/chat'
import { t } from '../i18n'
import MarkdownRender from './MarkdownRender.vue'

const chat = useChatStore()
const scrollEl = ref(null)

// 配对类候选（带 info）：云量/覆盖率格式化
function fmtPct(v) {
  return typeof v === 'number' ? `${v.toFixed(1)}%` : '—'
}
function fmtNum(v) {
  return v === null || v === undefined || v === '' ? '—' : String(v)
}

// 历史气泡防护：极长正文/思考（如模型异常复读的历史数据）截断渲染
function clipText(s, limit = 3000) {
  const t2 = String(s || '')
  return t2.length > limit
    ? `${t2.slice(0, limit)}\n\n（内容过长，已截断显示）`
    : t2
}

// 第六阶段：待答问题卡（按问题编号）——在消息流末尾以可点击选项呈现，
// 点击即提交答案（与文字回复共用同一通道）；问题被回答/失效后自动消失。
const openQuestions = computed(() =>
  (chat.kernelQuestionsOrder || [])
    .map((id) => chat.kernelQuestions[id])
    .filter((q) => q && (q.candidates || []).length)
    .map((q) => {
      const tid = (q.targets || [{}])[0]?.task_id
      const task = tid ? chat.kernelTasks[tid] : null
      return { ...q, taskLabel: task ? (task.label || task.region || '') : '' }
    }))

// 问题卡的“时间锚点”：卡片固定插在提问气泡之后，用户回复与后续气泡都出现在
// 卡片下面，不再被新消息挤到底部再消失；问题被回答/失效后卡片就地消失。
const pendingAnchor = new Set()

const norm = (s) => String(s || '').replace(/\s+/g, '')

// 按内容找回提问气泡：刷新页面后锚点丢失时，用问题文本在历史消息里
// 回溯匹配提问气泡（返回“应插在该下标消息之后”的锚点，未命中返回 0）
function findAskIndex(q, msgs) {
  const needle = norm(q && q.prompt).slice(0, 24)
  if (!needle) return 0
  for (let i = (msgs || []).length - 1; i >= 0; i--) {
    const m = msgs[i]
    if (m && m.role === 'assistant' && norm(m.content).includes(needle)) {
      return i + 1
    }
  }
  return 0
}

watch(
  () => (chat.kernelQuestionsOrder || []).join(','),
  () => {
    const ids = chat.kernelQuestionsOrder || []
    for (const id of ids) {
      if (id in chat.questionAnchors || pendingAnchor.has(id)) continue
      const q = chat.kernelQuestions[id]
      const idx = findAskIndex(q, chat.messages)
      if (idx > 0) {
        chat.questionAnchors[id] = idx
        continue
      }
      const last = chat.messages[chat.messages.length - 1]
      if (last && last.role === 'assistant') {
        chat.questionAnchors[id] = chat.messages.length
      } else {
        // 追问气泡可能稍后到达：先挂起，等 assistant 气泡落地再定锚
        pendingAnchor.add(id)
      }
    }
    for (const id of Object.keys(chat.questionAnchors)) {
      if (!ids.includes(id)) delete chat.questionAnchors[id]
    }
  },
  { immediate: true },
)

watch(
  () => chat.messages.length,
  () => {
    if (!pendingAnchor.size) return
    // 消息晚到时优先回头找“提问气泡”（刷新场景）；仍找不到才退末尾兜底
    for (const id of Array.from(pendingAnchor)) {
      const q = chat.kernelQuestions[id]
      const idx = findAskIndex(q, chat.messages)
      if (idx > 0) {
        chat.questionAnchors[id] = idx
        pendingAnchor.delete(id)
        continue
      }
      const last = chat.messages[chat.messages.length - 1]
      if (last && last.role === 'assistant') {
        chat.questionAnchors[id] = chat.messages.length
        pendingAnchor.delete(id)
      }
    }
  },
)

// 渲染行：消息 + 锚点位置的问题卡（未锚定者放末尾兜底）
const rows = computed(() => {
  const msgs = chat.messages || []
  const byIndex = {}
  const tail = []
  for (const q of openQuestions.value) {
    const a = Number(chat.questionAnchors[q.id] || 0)
    if (a > 0 && a <= msgs.length) {
      ;(byIndex[a - 1] = byIndex[a - 1] || []).push(q)
    } else {
      tail.push(q)
    }
  }
  const out = []
  msgs.forEach((m, i) => {
    out.push({ type: 'msg', key: `m${i}`, m, i })
    if (byIndex[i]) out.push({ type: 'cards', key: `q${i}`, qs: byIndex[i] })
  })
  if (tail.length) out.push({ type: 'cards', key: 'q-tail', qs: tail })
  return out
})

// 卡片宽度规则：优先与“本卡自己的提问气泡”等宽；找不到自己的提问气泡时
// （如配对追问卡，上方只有“答案已接收”短气泡），沿用最近一条提问气泡的
// 宽度——同一问答链的卡片保持一致。左对齐；内容/窗口/面板宽度变化时重同步。
function syncCardWidths() {
  const root = scrollEl.value
  if (!root) return
  let lastAskWidth = 0 // 最近一条“提问气泡”（含问号的助手气泡）宽度
  for (const el of Array.from(root.children)) {
    if (el.classList && el.classList.contains('msg')) {
      const bubble = el.querySelector('.msg__bubble')
      if (bubble && el.classList.contains('msg--ai')
          && (bubble.textContent || '').includes('？')) {
        const w = Math.round(bubble.getBoundingClientRect().width)
        if (w > 0) lastAskWidth = w
      }
      continue
    }
    if (!el.classList || !el.classList.contains('kernel-questions')) continue
    // 找该卡行上方最近的一条消息气泡
    let prev = el.previousElementSibling
    while (prev && !(prev.classList && prev.classList.contains('msg'))) {
      prev = prev.previousElementSibling
    }
    const bubble = prev ? prev.querySelector('.msg__bubble') : null
    // 自己的提问气泡：其文本包含卡面问题文本（前 24 字归一化匹配）
    const firstCard = el.querySelector('.kernel-question')
    const promptEl = firstCard
      ? firstCard.querySelector('.kernel-question__prompt') : null
    const needle = norm(promptEl ? promptEl.textContent : '').slice(0, 24)
    let ref = 0
    if (bubble && needle && norm(bubble.textContent).includes(needle)) {
      ref = Math.round(bubble.getBoundingClientRect().width)
    } else {
      ref = lastAskWidth
        || (bubble ? Math.round(bubble.getBoundingClientRect().width) : 0)
    }
    if (ref <= 0) continue
    for (const card of el.querySelectorAll('.kernel-question')) {
      card.style.width = `${ref}px`
    }
  }
}

let cardRO = null
onMounted(() => {
  if (typeof ResizeObserver !== 'undefined' && scrollEl.value) {
    cardRO = new ResizeObserver(() => syncCardWidths())
    cardRO.observe(scrollEl.value)
  }
})
onBeforeUnmount(() => {
  if (cardRO) { cardRO.disconnect(); cardRO = null }
})

watch(
  () => [chat.messages, chat.streaming, (chat.kernelQuestionsOrder || []).join(',')],
  async () => {
    await nextTick()
    if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight
    syncCardWidths()
  },
  { deep: true },
)

// 思考过程默认折叠：无论思考中还是思考结束都不自动展开，用户点击 summary 才查看
</script>

<template>
  <div ref="scrollEl" class="chat-messages">
    <div v-if="!chat.messages.length" class="chat-empty">
      <img class="chat-empty__logo" src="/logo.png?v=2" alt="GeoThermoAI" />
      <h2>GeoThermoAI</h2>
      <p>
        {{ t('chat.emptyIntro') }}<br />
        {{ t('chat.emptyExample') }}
      </p>
    </div>
    <div v-else class="chat-scroll">
      <template v-for="row in rows" :key="row.key">
        <div
          v-if="row.type === 'msg'"
          class="msg"
          :class="row.m.role === 'user' ? 'msg--user' : 'msg--ai'"
        >
          <div class="msg__bubble">
            <!-- 思考过程折叠块：独立于正文，样式明显区分 -->
            <details v-if="row.m.thinking" class="thinking-box" :open="false">
              <summary>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/></svg>
                <span>{{ t('chat.thinking') }}</span>
                <!-- 思考用时，格式 (用时XX秒) -->
                <span v-if="row.m.thinking_seconds" class="thinking-box__seconds">{{ t('chat.thinkingSeconds', { sec: row.m.thinking_seconds }) }}</span>
                <span v-if="chat.streaming && row.i === chat.messages.length - 1" class="thinking-box__live">{{ t('chat.thinkingLive') }}</span>
              </summary>
              <div class="thinking-box__body">{{ clipText(row.m.thinking) }}</div>
            </details>
            <MarkdownRender :content="clipText(row.m.content)" />
            <span v-if="chat.streaming && row.i === chat.messages.length - 1 && row.m.role === 'assistant'" class="typing-cursor"></span>
          </div>
        </div>
        <!-- 待答问题卡：可点击选项，过期自动失效；锚定在提问气泡之后 -->
        <div v-else class="kernel-questions" data-card-anchor>
          <div v-for="q in row.qs" :key="q.id" class="kernel-question">
            <div v-if="q.taskLabel" class="kernel-question__task">{{ q.taskLabel }}</div>
            <div class="kernel-question__prompt">{{ q.prompt }}</div>
            <div class="kernel-question__opts">
              <template v-for="c in q.candidates" :key="c.id">
                <button
                  v-if="!c.info"
                  class="kernel-question__opt"
                  @click="chat.answerKernelQuestion(q.id, c.label)"
                >{{ c.id }}. {{ c.label }}</button>
                <button
                  v-else
                  class="kernel-question__pair"
                  @click="chat.answerKernelQuestion(q.id, c.label)"
                >
                  <span class="kernel-question__pair-head">
                    <span class="kernel-question__no">{{ c.id }}</span>
                    {{ c.label }}
                    <em v-if="c.info.recommended" class="kernel-question__rec">{{ t('pair.recTag') }}</em>
                  </span>
                  <span class="kernel-question__pair-row">
                    Landsat：{{ t('pair.lCloud') }} {{ fmtPct(c.info.landsat && c.info.landsat.cloud) }}
                    · {{ t('pair.lCover') }} {{ fmtPct(c.info.landsat && c.info.landsat.coverage) }}
                    · {{ fmtNum(c.info.landsat && c.info.landsat.count) }}{{ t('pair.lCount') }}
                  </span>
                  <span class="kernel-question__pair-row">
                    Sentinel-2：{{ t('pair.lCloud') }} {{ fmtPct(c.info.sentinel2 && c.info.sentinel2.cloud) }}
                    · {{ t('pair.lCover') }} {{ fmtPct(c.info.sentinel2 && c.info.sentinel2.coverage) }}
                    · {{ fmtNum(c.info.sentinel2 && c.info.sentinel2.count) }}{{ t('pair.lCount') }}
                  </span>
                  <span v-if="c.info.time_diff_days != null" class="kernel-question__pair-row">
                    {{ t('pair.lTimeDiff') }} {{ fmtNum(c.info.time_diff_days) }}{{ t('pair.lDays') }}
                    <template v-if="c.info.recommend_reason"> · {{ c.info.recommend_reason }}</template>
                  </span>
                </button>
              </template>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.typing-cursor {
  display: inline-block; width: 2px; height: 14px; background: currentColor;
  margin-left: 2px; vertical-align: -2px; animation: blink 0.8s step-end infinite;
}
@keyframes blink { 50% { opacity: 0; } }

/* ── 第六阶段：待答问题卡（可点击选项） ── */
/* 卡片列：左对齐（与上方气泡同起线，去侧边缩进）；卡片宽度由脚本按“上方最近气泡”等宽设置 */
.kernel-questions { display: flex; flex-direction: column; gap: 10px; padding: 4px 0 12px; align-items: flex-start; }
.kernel-question {
  border: 1px solid #dbe3f0; border-left: 3px solid var(--primary, #2563eb);
  border-radius: 10px; background: #f7faff; padding: 10px 12px;
}
.kernel-question__task { font-size: 12px; font-weight: 600; color: #334155; margin-bottom: 4px; }
.kernel-question__prompt { font-size: 13px; color: #1f2937; margin-bottom: 8px; }
.kernel-question__opts { display: flex; flex-wrap: wrap; gap: 8px; }
.kernel-question__opt {
  border: 1px solid var(--primary, #2563eb); color: var(--primary, #2563eb);
  background: #fff; border-radius: 8px; padding: 4px 12px; font-size: 13px; cursor: pointer;
}
.kernel-question__opt:hover { background: var(--primary, #2563eb); color: #fff; }

/* 配对选择卡片（带结构信息：云量/覆盖/时差） */
.kernel-question__pair {
  width: 100%; display: flex; flex-direction: column; gap: 3px;
  border: 1px solid var(--primary, #2563eb); border-radius: 8px;
  background: #fff; color: var(--text, #1f2937);
  padding: 8px 12px; font-size: 13px; cursor: pointer; text-align: left;
}
.kernel-question__pair:hover { background: #eef4ff; }
.kernel-question__pair-head { display: flex; align-items: center; gap: 6px; font-weight: 600; }
.kernel-question__no {
  color: #9ca3af; font-size: 11px; font-weight: 400; flex-shrink: 0;
  border: 1px solid #e5e7eb; border-radius: 4px; padding: 0 5px;
}
.kernel-question__rec {
  font-style: normal; font-size: 11px; color: #fff;
  background: var(--primary, #2563eb); border-radius: 999px; padding: 1px 8px;
}
.kernel-question__pair-row { color: #6b7280; font-size: 12px; }

/* ── 思考过程折叠块──────────────────────────────
   浅灰底、深灰字，与正文白色气泡区分但风格协调；
   默认折叠（模板 :open="false"），用户点击 summary 展开查看 */
.thinking-box {
  margin: 0 0 10px; border-radius: 10px; overflow: hidden;
  background: #f2f4f7; border: 1px solid #e0e4ea;
  font-size: 13px;
}
.thinking-box summary {
  cursor: pointer; user-select: none; list-style: none;
  display: flex; align-items: center; gap: 7px;
  padding: 7px 12px; color: #5b6472; font-size: 12.5px; font-weight: 600;
}
.thinking-box summary::-webkit-details-marker { display: none; }
.thinking-box summary::after {
  content: ""; width: 6px; height: 6px; margin-left: auto; flex-shrink: 0;
  border-right: 1.5px solid #9aa1ad; border-bottom: 1.5px solid #9aa1ad;
  transform: rotate(-45deg); transition: transform 0.15s;
}
.thinking-box[open] summary::after { transform: rotate(45deg); }
.thinking-box summary:hover { background: #eaecef; }
.thinking-box summary svg { flex-shrink: 0; color: #8a92a0; }
.thinking-box__live {
  margin-left: auto; font-weight: 400; color: #8a92a0; font-size: 12px;
}
.thinking-box__body {
  margin: 0; padding: 8px 12px 10px;
  border-top: 1px dashed #d5d9e0; color: #4a5462;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  font-size: 12px; line-height: 1.7; white-space: pre-wrap;
  word-break: break-word; max-height: 320px; overflow-y: auto;
}
</style>
