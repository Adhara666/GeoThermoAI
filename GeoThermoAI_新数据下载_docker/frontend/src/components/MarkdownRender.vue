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

const html = computed(() => {
  try {
    return marked.parse(preprocessBold(props.content || ''))
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
