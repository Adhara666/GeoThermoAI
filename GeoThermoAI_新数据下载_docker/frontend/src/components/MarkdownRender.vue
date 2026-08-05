<script setup>
import { computed, watch, nextTick, ref } from 'vue'
import { marked } from 'marked'

const props = defineProps({ content: { type: String, default: '' } })

marked.setOptions({
  breaks: true,
  gfm: true,
})

const html = computed(() => {
  try {
    return marked.parse(props.content || '')
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
