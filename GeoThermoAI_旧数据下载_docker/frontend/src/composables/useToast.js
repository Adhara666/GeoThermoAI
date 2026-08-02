// 全局 Toast（轻量，无第三方依赖）
import { reactive } from 'vue'

export const toasts = reactive([])
let _id = 0

export function toast(msg, type = 'info', timeout = 3200) {
  const id = ++_id
  toasts.push({ id, msg, type })
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
