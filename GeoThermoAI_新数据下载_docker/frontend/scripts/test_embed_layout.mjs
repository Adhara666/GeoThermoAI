/**
 * Local regression for ModelScope-like embedding:
 * parent page has a Studio chrome bar; iframe is wrongly sized to 100vh.
 * App shell must remain fully inside the visible viewport (below the chrome).
 *
 * Usage:
 *   node scripts/test_embed_layout.mjs
 */
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const distDir = path.resolve(__dirname, '../dist')
const CHROME = 64
const VIEWPORT = { width: 1440, height: 900 }

function contentType(filePath) {
  const ext = path.extname(filePath).toLowerCase()
  return ({
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
    '.svg': 'image/svg+xml',
    '.json': 'application/json',
  })[ext] || 'application/octet-stream'
}

function startStaticServer(root) {
  const server = http.createServer((req, res) => {
    const [rawPath, rawQuery] = (req.url || '/').split('?')
    const urlPath = decodeURIComponent(rawPath)
    let rel = urlPath === '/' ? '/index.html' : urlPath
    if (urlPath === '/__harness.html') {
      // ?chrome=N lets each scenario simulate a different *real* Studio chrome
      // height without touching the app's own built-in guess (DEFAULT_MODELSCOPE_CHROME_PX
      // stays untouched) — this is what makes a "wrong guess, corrected by the
      // calibration widget" scenario actually reproducible in this harness.
      const params = new URLSearchParams(rawQuery || '')
      const chromePx = Number(params.get('chrome'))
      const realChrome = Number.isFinite(chromePx) && chromePx >= 0 ? chromePx : CHROME
      const html = `<!doctype html>
<html><head><meta charset="utf-8"><title>embed harness</title>
<style>
  html,body{margin:0;height:100%;overflow:hidden;background:#111}
  #chrome{height:${realChrome}px;background:#1f2430;color:#fff;display:flex;align-items:center;padding:0 16px;font:14px sans-serif;flex-shrink:0}
  iframe{border:0;width:100%;height:100vh;display:block;background:#fff}
</style></head>
<body>
  <div id="chrome">Fake ModelScope Studio chrome (${realChrome}px)</div>
  <iframe id="app" src="/"></iframe>
</body></html>`
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
      res.end(html)
      return
    }
    const filePath = path.normalize(path.join(root, rel))
    if (!filePath.startsWith(root)) {
      res.writeHead(403); res.end('forbidden'); return
    }
    fs.readFile(filePath, (err, data) => {
      if (err) { res.writeHead(404); res.end('not found'); return }
      res.writeHead(200, { 'Content-Type': contentType(filePath) })
      res.end(data)
    })
  })
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      resolve({ server, port })
    })
  })
}

async function measureFrame(frame, iframeEl) {
  const iframeBox = await iframeEl.boundingBox()
  const css = await frame.evaluate(() => {
    const root = document.documentElement
    const shell = document.querySelector('.app-shell, .login-page')
    const shellRect = shell ? shell.getBoundingClientRect() : null
    return {
      appHeight: parseFloat(root.style.getPropertyValue('--app-height')) || 0,
      embedChrome: parseFloat(root.style.getPropertyValue('--embed-chrome')) || 0,
      embedFit: root.dataset.embedFit || '',
      shellHeight: shellRect?.height ?? null,
      shellBottom: shellRect?.bottom ?? null,
      innerHeight: window.innerHeight,
    }
  })
  const shellBottomInParent = iframeBox.y + css.shellBottom
  const overflowPx = shellBottomInParent - VIEWPORT.height
  return { iframeTop: iframeBox.y, iframeHeight: iframeBox.height, ...css, shellBottomInParent, overflowPx }
}

async function main() {
  if (!fs.existsSync(path.join(distDir, 'index.html'))) {
    throw new Error(`dist missing: ${distDir}. Run npm run build first.`)
  }

  const { server, port } = await startStaticServer(distDir)
  const browser = await chromium.launch({ headless: true })

  // ── Scenario 1: guess exactly matches the simulated chrome (regression guard) ──
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(`http://127.0.0.1:${port}/__harness.html`, { waitUntil: 'networkidle' })
  const iframeEl = await page.$('#app')
  const frame = await iframeEl.contentFrame()
  if (!frame) throw new Error('iframe contentFrame unavailable')
  await frame.waitForFunction(() => document.documentElement?.dataset?.embedFit === 'modelscope', null, { timeout: 10000 })
  await page.waitForTimeout(400)
  const metrics = { chromePx: CHROME, ...(await measureFrame(frame, iframeEl)) }

  const ok1 =
    metrics.embedFit === 'modelscope' &&
    metrics.embedChrome === CHROME &&
    metrics.appHeight > 0 &&
    metrics.appHeight <= (VIEWPORT.height - CHROME + 1) &&
    metrics.overflowPx <= 1

  // ── Scenario 2 (v1.2): the built-in guess (CHROME=64) is *wrong* for this
  // "real" page — the true Studio chrome here is only 40px, so the default guess
  // over-shrinks the shell and would leave the reported gray strip. Nothing
  // inside the iframe can detect that on its own (see embedFit.js docstring);
  // the fix is a value manually calibrated on the real page and saved to
  // localStorage. This harness run keeps the app's own DEFAULT_MODELSCOPE_CHROME_PX
  // untouched (still 64) and instead renders the fake Studio chrome bar at the
  // *true* 40px via ?chrome=40, while seeding localStorage with the matching
  // calibrated override — regression guard for the calibration widget added
  // alongside the technical-doc appendix C update.
  const REAL_CHROME = 40
  await page.close()
  const page2 = await browser.newPage({ viewport: VIEWPORT })
  // Seed the *iframe's* localStorage before navigation (same-origin in this local
  // harness, unlike the real cross-origin ModelScope embedding — that asymmetry
  // is exactly why calibration has to be done by hand on the real page once).
  await page2.addInitScript((chrome) => {
    window.localStorage.setItem('gtai_embed_chrome', String(chrome))
  }, REAL_CHROME)
  await page2.goto(`http://127.0.0.1:${port}/__harness.html?chrome=${REAL_CHROME}`, { waitUntil: 'networkidle' })
  const iframeEl2 = await page2.$('#app')
  const frame2 = await iframeEl2.contentFrame()
  await frame2.waitForFunction(() => document.documentElement?.dataset?.embedFit === 'modelscope', null, { timeout: 10000 })
  await page2.waitForTimeout(400)
  const metrics2 = { realChromePx: REAL_CHROME, ...(await measureFrame(frame2, iframeEl2)) }

  const ok2 =
    metrics2.embedChrome === REAL_CHROME &&
    metrics2.appHeight === (VIEWPORT.height - REAL_CHROME) &&
    metrics2.overflowPx <= 1

  await browser.close()
  server.close()

  const ok = ok1 && ok2
  console.log(JSON.stringify({ ok, scenario1_default_guess: metrics, scenario2_calibrated: metrics2 }, null, 2))
  if (!ok) {
    console.error('EMBED LAYOUT TEST FAILED: default-guess or calibrated-override scenario is wrong')
    process.exit(1)
  }
  console.log('EMBED LAYOUT TEST PASSED (default guess + localStorage calibration override)')
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
