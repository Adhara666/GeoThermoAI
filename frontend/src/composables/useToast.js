// 全局 Toast（轻量，无第三方依赖）
import { reactive } from 'vue'

export const toasts = reactive([])
let _id = 0

// 成功（绿色）弹窗统一去掉 emoji 图标：覆盖 ✅/⚠️/❌/🎉/🟢 等常用 emoji 及其
// 变体选择符（U+FE0F），避免服务端消息里的 emoji 出现在绿色弹窗中。
const EMOJI_RE = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2190}-\u{21FF}\u{FE0F}]/gu

function cleanSuccessText(msg) {
  return String(msg ?? '')
    .replace(EMOJI_RE, '')
    .replace(/[ \t]+/g, ' ')
    .replace(/^[\s\u3000]+|[\s\u3000]+$/g, '')
}

export function toast(msg, type = 'info', timeout = 3200) {
  const text = type === 'success' ? cleanSuccessText(msg) : String(msg ?? '')
  const id = ++_id
  toasts.push({ id, msg: text, type })
  setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === id)
    if (i >= 0) toasts.splice(i, 1)
  }, timeout)
}

export function useToast() {
  return {
    info: (m) => toast(m, 'info'),
    success: (m) => toast(m, 'success'),
    error: (m) => toast(m, 'error'),
  }
}
