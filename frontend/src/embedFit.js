/**
 * Fit the app shell to the actual viewport height.
 *
 * 历史：此前嵌入 ModelScope Studio 时，因 iframe 可能按 100vh 撑满而父页面顶栏
 * 遮挡底部，这里曾减去一个「猜的顶栏高度（默认 64px）」并附带手动校准控件
 * （EmbedCalibrate.vue / ?embedChrome= 参数 / localStorage）。跨域 iframe 内部
 * 无法测量父页面顶栏真实高度，只能靠人工校准。实际部署观察确认 0px 即正常
 * （创空间 iframe 高度已正确计算），因此偏移与校准控件已整体移除：
 * 应用始终使用 iframe 视口全高，并在 resize 时刷新（覆盖移动端地址栏/iframe
 * 尺寸变化）。
 */

export function applyEmbedFit() {
  const root = document.documentElement
  const raw = window.visualViewport?.height || window.innerHeight || root.clientHeight || 800
  const height = Math.max(320, Math.floor(raw))
  root.style.setProperty('--app-height', `${height}px`)
  return { height }
}

export function startEmbedFit() {
  const run = () => applyEmbedFit()
  run()
  window.addEventListener('resize', run)
  window.visualViewport?.addEventListener('resize', run)
  window.visualViewport?.addEventListener('scroll', run)
  // Studio chrome / iframe 布局可能在首帧后才稳定
  window.setTimeout(run, 50)
  window.setTimeout(run, 300)
  return () => {
    window.removeEventListener('resize', run)
    window.visualViewport?.removeEventListener('resize', run)
    window.visualViewport?.removeEventListener('scroll', run)
  }
}
