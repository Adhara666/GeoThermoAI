<script setup>
import { computed, watch, nextTick, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const props = defineProps({ content: { type: String, default: '' } })

marked.setOptions({
  breaks: true,
  gfm: true,
})

// 兼容预处理：marked 对「**中文**」紧贴前后中文字符（如"基于**TTRI**方法"）不渲染，
// 因为 ** 前面紧跟的是 unicode 单词字符。先把这种粗体转成 HTML，再交给 marked 解析。
// 保护围栏代码块：代码块内的 ** 保持原样，不被误替换。
function preprocessBold(src) {
  if (!src || !src.includes('**')) return src
  const blocks = []
  const protectedSrc = (src || '').replace(/```[\s\S]*?```/g, (m) => {
    blocks.push(m)
    return `\u0000BLOCK${blocks.length - 1}\u0000`
  })
  const out = protectedSrc.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
  return out.replace(/\u0000BLOCK(\d+)\u0000/g, (_, i) => blocks[Number(i)])
}

// 防御性修复：marked 的 HTML block 解析会把行首的块级 HTML 标签（<div> 等）
// 连同其后所有非空行吞进原始 HTML，直到空行才恢复 markdown 解析。
// 旧消息里 step-gap 的 <div> 后只跟单个换行，紧随其后的 "## 第 N 步" 标题
// 会以字面 "##" 显示。这里给块级 HTML 行后补一个空行，恢复后续 markdown 解析
//（服务端已改为输出空行，此预处理兼容存量历史消息 + 防未来再犯）。
function protectHtmlBlocks(src) {
  if (!src || !/^[ \t]*</m.test(src)) return src
  const lines = (src || '').split('\n')
  const out = []
  for (let i = 0; i < lines.length; i++) {
    out.push(lines[i])
    const t = lines[i].trim()
    const isHtmlBlockLine =
      /^<(?:div|details|blockquote|p|ul|ol|li|table|hr|h[1-6])(?:\s|>)/i.test(t) && t.endsWith('>')
    const next = lines[i + 1]
    const nextBlank = next === undefined || next.trim() === ''
    if (isHtmlBlockLine && !nextBlank) {
      out.push('')
    }
  }
  return out.join('\n')
}

// 兜底：模型偶尔漏转义下划线（如 $n_estimators$），MathJax 会把每个下划线
// 当成下标起点，导致字母错位；这里在排版前自动修复 $...$ / $$...$$ 内的下划线。
// ⚠ 关键教训：Markdown（marked）会把【单反斜杠】转义吃掉（`\_` 经 marked 后
// HTML 里变回 `_`），所以单层转义会被还原后依旧成下标；必须写成【双反斜杠】：
// `\\_` 经 marked 后才是 `\_`，MathJax 才按字面下划线渲染。
function fixLatexUnderscores(src) {
  if (!src || !src.includes('$')) return src
  const blocks = []
  const protectedSrc = src.replace(/```[\s\S]*?```/g, (m) => {
    blocks.push(m)
    return `\u0000LATEX${blocks.length - 1}\u0000`
  })
  const D2 = String.raw`\\_`   // 双反斜杠+下划线（Markdown 源中的正确形态）
  const D1 = String.raw`\_`    // 单反斜杠+下划线（模型手工转义形态）
  const fix = (body) => body
    .split(D2).join('\u0001')  // 已双反斜杠：暂存
    .split(D1).join('\u0002')  // 单反斜杠：暂存
    .split('_').join(D2)       // 其余单下划线 → 双反斜杠形态
    .split('\u0001').join(D2)
    .split('\u0002').join(D2)
  let out = protectedSrc.replace(/\$\$([\s\S]+?)\$\$/g,
    (m, body) => `$$${fix(body)}$$`)
  out = out.replace(/\$([^$\n]+?)\$/g, (m, body) => `$${fix(body)}$`)
  return out.replace(/\u0000LATEX(\d+)\u0000/g, (_, i) => blocks[Number(i)])
}

const html = computed(() => {
  try {
    const raw = marked.parse(preprocessBold(
      fixLatexUnderscores(protectHtmlBlocks(props.content || ''))))
    // 净化：marked 不清理 HTML，用户输入/LLM 输出中的 <script>/<img onerror> 等
    // 会经 v-html 直接执行（存储型 XSS）。DOMPurify 白名单化后再渲染。
    return DOMPurify.sanitize(raw)
  } catch (_) {
    return props.content || ''
  }
})

const el = ref(null)

// MathJax 是 index.html 里 async 加载的本地脚本（startup.typeset=false、仅手动排版）。
// 若消息先于脚本就绪渲染，旧实现直接跳过且永不补排 → 页面上永久显示裸的 $...$。
// 这里改为：未就绪时等待启动完成（或轮询重试），就绪后补排版；
// 并开启 immediate，刷新后存量历史消息也会被重新排版。
let _mathWaitTries = 0
async function typesetMath() {
  if (!el.value) return
  const mj = window.MathJax
  if (!mj) return
  if (!mj.typesetPromise) {
    if (mj.startup && mj.startup.promise) {
      try { await mj.startup.promise } catch (_) { return }
    } else if (_mathWaitTries < 50) {
      _mathWaitTries += 1
      setTimeout(typesetMath, 200)
      return
    } else {
      return
    }
  }
  try {
    if (window.MathJax && window.MathJax.typesetPromise) {
      await window.MathJax.typesetPromise([el.value])
    }
  } catch (_) {}
}

watch(html, async () => {
  await nextTick()
  typesetMath()
}, { immediate: true })
</script>

<template>
  <div ref="el" v-html="html"></div>
</template>
