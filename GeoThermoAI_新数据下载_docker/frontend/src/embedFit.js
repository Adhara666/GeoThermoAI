/**
 * Fit the app shell into the *visible* host area when embedded in ModelScope Studio.
 *
 * ModelScope often hosts the app in an iframe whose CSS height is ~100vh of the
 * parent page, while a Studio chrome bar sits above the iframe. The iframe
 * document still thinks it is full-viewport tall, so height:100%/100vh clips the
 * chat input. Fullscreen mode removes that chrome, which is why it "fixes" itself.
 *
 * Strategy:
 * - Always pin the shell to a measured --app-height.
 * - When embedded (iframe / modelscope referrer), subtract a Studio chrome offset
 *   so the shell fits the visible slice of the oversized iframe.
 * - Allow ?embedChrome=N to override; ?embedChrome=0 disables the offset.
 *
 * KNOWN LIMITATION: DEFAULT_MODELSCOPE_CHROME_PX
 * is an unverified guess, not a value measured on the real page. Guessing too
 * large leaves a gray strip of unfilled <body> background at the bottom of the
 * chat area (the reported symptom); guessing too small clips the chat input
 * instead. Because the app runs inside a cross-origin iframe, it is *impossible*
 * to tell these two cases apart purely from measurements taken inside the
 * iframe — both look identical (same gap size) from in here. So instead of
 * guessing again, `readChromeOverride()` also accepts a value persisted to
 * localStorage by the on-screen calibration widget (see EmbedCalibrate.vue):
 * someone looking at the real deployed page can nudge the offset until the gray
 * strip disappears and the input box is not clipped, then save it — durable
 * across visits without needing to keep appending ?embedChrome=N to the URL.
 */

const DEFAULT_MODELSCOPE_CHROME_PX = 64
const STORAGE_KEY = 'gtai_embed_chrome'

function isEmbedded() {
  try {
    if (window.self !== window.top) return true
  } catch {
    return true
  }
  const ref = String(document.referrer || '')
  return /modelscope\.cn/i.test(ref)
}

function readUrlOverride() {
  try {
    const raw = new URLSearchParams(window.location.search).get('embedChrome')
    if (raw === null) return null
    const n = Number(raw)
    return Number.isFinite(n) ? Math.max(0, n) : null
  } catch {
    return null
  }
}

function readStoredOverride() {
  try {
    const raw = window.localStorage?.getItem(STORAGE_KEY)
    if (raw === null || raw === undefined) return null
    const n = Number(raw)
    return Number.isFinite(n) ? Math.max(0, n) : null
  } catch {
    return null
  }
}

/** Priority: explicit URL param > saved calibration > built-in guess. */
function readChromeOverride() {
  const url = readUrlOverride()
  if (url !== null) return url
  return readStoredOverride()
}

function resolveChromePx() {
  const override = readChromeOverride()
  if (override !== null) return override
  if (!isEmbedded()) return 0
  return DEFAULT_MODELSCOPE_CHROME_PX
}

/** Persist a manually-calibrated chrome offset (px) so it survives reloads. */
export function saveEmbedChromeOverride(px) {
  try {
    if (px === null) {
      window.localStorage?.removeItem(STORAGE_KEY)
    } else {
      window.localStorage?.setItem(STORAGE_KEY, String(Math.max(0, Math.floor(px))))
    }
  } catch {
    /* localStorage unavailable (private mode etc.) — calibration just won't persist */
  }
  return applyEmbedFit()
}

export function currentEmbedChromePx() {
  return resolveChromePx()
}

export function applyEmbedFit() {
  const root = document.documentElement
  const chrome = resolveChromePx()
  const raw = window.visualViewport?.height || window.innerHeight || root.clientHeight || 800
  const height = Math.max(320, Math.floor(raw - chrome))
  root.style.setProperty('--app-height', `${height}px`)
  root.style.setProperty('--embed-chrome', `${chrome}px`)
  root.dataset.embedFit = chrome > 0 ? 'modelscope' : 'standalone'
  return { height, chrome, embedded: chrome > 0 || isEmbedded() }
}

export function startEmbedFit() {
  const run = () => applyEmbedFit()
  run()
  window.addEventListener('resize', run)
  window.visualViewport?.addEventListener('resize', run)
  window.visualViewport?.addEventListener('scroll', run)
  // Studio chrome / iframe can settle after first paint
  window.setTimeout(run, 50)
  window.setTimeout(run, 300)
  return () => {
    window.removeEventListener('resize', run)
    window.visualViewport?.removeEventListener('resize', run)
    window.visualViewport?.removeEventListener('scroll', run)
  }
}
