<script setup>
import { computed, watch, nextTick, ref } from 'vue'
import { marked } from 'marked'

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

const html = computed(() => {
  try {
    return marked.parse(preprocessBold(protectHtmlBlocks(props.content || '')))
  } catch (_) {
    return props.content || ''
  }
})

const el = ref(null)
watch(html, async () => {
  await nextTick()
  try {
    if (window.MathJax && window.MathJax.typesetPromise) {
      window.MathJax.typesetPromise([el.value])
    }
  } catch (_) {}
})
</script>

<template>
  <div ref="el" v-html="html"></div>
</template>
