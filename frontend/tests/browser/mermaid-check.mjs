// Browser check for Mermaid diagrams in the run panel. Run with
// `npm run check:browser` (it builds first).
//
// Self-contained and safe for real data: it starts its own backend on a
// throwaway MITSS_ROOT in a temp directory and its own `vite preview` of the
// production build, both on free ports, seeds runs there, and drives the
// installed Chrome with playwright-core. Nothing touches backend/data/.
//
// It fails (exit 1) if any case below is wrong, if the page logs an error or
// opens a dialog, or if any request leaves for anything other than the app's
// own /, /assets/ or /api/ paths — so a Mermaid block cannot make the browser
// fetch a URL, local or remote.

import { spawn } from 'node:child_process'
import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND = path.resolve(HERE, '../..')
const BACKEND = path.resolve(FRONTEND, '../backend')
const F = '```'

// Each case: the output recorded, and what the panel must show, in order:
// 'img' for a drawn diagram, 'failed' for error + raw code.
const CASES = {
  none: {
    output: `Plain answer.\n\n${F}python\nprint('hello')\n${F}\nThe word mermaid, unfenced.`,
    expect: [],
  },
  valid: {
    output: `Flow:\n\n${F}mermaid\ngraph TD\n  A[Request] --> B{Cached?}\n  B -- yes --> C[Return]\n  B -- no --> D[Call model]\n  D --> C\n${F}\nDone.`,
    expect: ['img'],
  },
  invalid: {
    output: `Broken:\n\n${F}mermaid\ngraph TD\n  A -->\n  B -->> ((C\n${F}\n`,
    expect: ['failed'],
  },
  mixed: {
    output: `One:\n${F}mermaid\nflowchart LR\n  one --> two\n${F}\nTwo:\n${F}mermaid\nsequenceDiagram\n  Alice ->\n${F}\nThree:\n${F}mermaid\nsequenceDiagram\n  Alice->>Bob: hello\n${F}\n`,
    expect: ['img', 'failed', 'img'],
  },
  unclosed: {
    output: `Cut off:\n${F}mermaid\ngraph TD\n  A --> B\n  B -->`,
    expect: ['failed'],
    unclosed: true,
  },
  hostile: {
    output: [
      `${F}mermaid`,
      `%%{init: {'securityLevel': 'loose', 'htmlLabels': true, 'flowchart': {'htmlLabels': true}}}%%`,
      'graph TD',
      '  A["<img src=x onerror=window.__pwned=1>"] --> B["<script>window.__pwned=2</script>"]',
      '  click A callback "x"',
      '  click B href "javascript:window.__pwned=3"',
      F,
      `${F}mermaid`,
      'flowchart TD',
      '  R@{ img: "https://example.invalid/beacon.png", label: "remote", h: 60, constraint: "on" }',
      '  L@{ img: "/api/health?beacon=1", label: "local", h: 60 }',
      '  R --> L',
      F,
    ].join('\n'),
    // The second block links images; the page blocks them, so it fails visibly.
    expect: ['img', 'failed'],
  },
}

const log = (...a) => console.log(...a)
const failures = []
const check = (ok, message) => { if (!ok) failures.push(message) }

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
    server.on('error', reject)
  })
}

async function waitFor(url, what, child) {
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) throw new Error(`${what} exited early (${child.exitCode})`)
    try {
      if ((await fetch(url)).ok) return
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 200))
  }
  throw new Error(`${what} did not come up at ${url}`)
}

async function post(base, route, body) {
  const response = await fetch(`${base}${route}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`${route}: ${response.status} ${await response.text()}`)
  return response.json()
}

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mitss-mermaid-'))
const children = []
let browser

try {
  if (!fs.existsSync(path.join(FRONTEND, 'dist/index.html'))) {
    throw new Error('no production build; run `npm run check:browser`, which builds first')
  }
  const apiPort = await freePort()
  const webPort = await freePort()
  const api = `http://127.0.0.1:${apiPort}`
  const web = `http://127.0.0.1:${webPort}`

  const backend = spawn('python3', ['-m', 'uvicorn', 'app.main:app', '--port', String(apiPort)], {
    cwd: BACKEND, env: { ...process.env, MITSS_ROOT: root }, stdio: 'ignore',
  })
  children.push(backend)
  const preview = spawn(process.execPath, [path.join(FRONTEND, 'node_modules/vite/bin/vite.js'), 'preview', '--port', String(webPort), '--strictPort'], {
    cwd: FRONTEND, env: { ...process.env, MITSS_API: api }, stdio: 'ignore',
  })
  children.push(preview)
  await waitFor(`${api}/api/health`, 'backend', backend)
  await waitFor(`${web}/api/health`, 'vite preview', preview)

  const prompt = await post(api, '/api/prompts', { name: 'mermaid check', text: 'Draw it.', note: 'browser check' })
  const promptId = prompt.id || prompt.prompt?.id
  for (const [kind, c] of Object.entries(CASES)) {
    await post(api, '/api/runs', {
      prompt_id: promptId, version: 1, model: `case-${kind}`, output: c.output, verdict: 'unrated',
    })
  }

  browser = await chromium.launch({ channel: 'chrome' })
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1400, height: 1000 } })
  const stray = []
  const mermaidChunk = []
  context.on('request', (request) => {
    const url = new URL(request.url())
    if (url.protocol === 'data:' || url.protocol === 'blob:') return
    const own = url.origin === web && (url.pathname === '/' || /^\/(assets|api)\//.test(url.pathname))
    const beacon = url.searchParams.has('beacon')
    if (!own || beacon) stray.push(request)
    if (/mermaid\.core/.test(url.pathname)) mermaidChunk.push(url.pathname)
  })
  // A stray attempt is only acceptable if the page's CSP refused it, so it
  // never reached any server.
  const answered = new Set()
  const refused = new Set()
  context.on('response', (response) => answered.add(response.request()))
  context.on('requestfailed', (request) => {
    if (/BLOCKED_BY_CSP|csp/i.test(request.failure()?.errorText || '')) refused.add(request)
  })
  // Nothing may leave the machine even if a check above is wrong.
  await context.route((url) => url.hostname !== '127.0.0.1', (route) => route.abort())

  const page = await context.newPage()
  const problems = []
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`console: ${m.text()}`) })
  page.on('pageerror', (e) => problems.push(`page error: ${e.message}`))
  page.on('dialog', (d) => { problems.push(`dialog: ${d.message()}`); d.dismiss() })

  await page.goto(web)
  await page.locator('.prompt-item', { hasText: 'mermaid check' }).click()
  await page.getByRole('tab', { name: /Outputs/ }).click()

  for (const [kind, c] of Object.entries(CASES)) {
    await page.locator('.run-row', { hasText: new RegExp(`case-${kind}\\b`) }).click()
    const card = page.locator('section.card', {
      has: page.locator('.card-head h2', { hasText: new RegExp(`^v1 · case-${kind}$`) }),
    })
    await card.waitFor()
    await page.waitForFunction(() => !document.querySelector('.diagram-pending'), null, { timeout: 20000 })
    const shown = await card.locator('figure.diagram').evaluateAll((els) =>
      els.map((e) => (e.classList.contains('failed') ? 'failed' : e.querySelector('.diagram-canvas img') ? 'img' : '?')))
    check(JSON.stringify(shown) === JSON.stringify(c.expect), `${kind}: shown ${JSON.stringify(shown)}, expected ${JSON.stringify(c.expect)}`)
    check((await card.locator('.diagrams').count()) === (c.expect.length ? 1 : 0), `${kind}: diagram section presence`)
    const failed = card.locator('figure.failed')
    for (let i = 0; i < (await failed.count()); i++) {
      const error = await failed.nth(i).locator('.diagram-error').innerText()
      const code = await failed.nth(i).locator('pre.output').innerText()
      check(/could not be drawn/.test(error) && error.length > 30, `${kind}: failed block has no error message`)
      check(code.trim().length > 0 && c.output.includes(code.trim()), `${kind}: failed block does not show its raw code`)
      if (c.unclosed) check(/no closing fence/.test(error), `${kind}: unclosed block not flagged`)
    }
    const drawn = await card.locator('.diagram-canvas img').evaluateAll((els) => els.map((e) => e.naturalWidth > 0 && e.clientHeight > 0))
    check(drawn.every(Boolean), `${kind}: a drawn diagram has no size`)
    check((await card.getByRole('button', { name: 'Save PNG' }).count()) === drawn.length, `${kind}: Save PNG buttons != drawn diagrams`)
    if (kind === 'none') check(mermaidChunk.length === 0, 'none: Mermaid library loaded for an output without Mermaid')
    log(`  ${kind.padEnd(9)} ${JSON.stringify(shown)}`)
  }

  await page.locator('.run-row', { hasText: /case-valid\b/ }).click()
  await page.waitForSelector('.diagram-canvas img')
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Save PNG' }).first().click(),
  ])
  const saved = path.join(root, 'saved.png')
  await download.saveAs(saved)
  const png = fs.readFileSync(saved)
  const pngOk = png.subarray(1, 4).toString() === 'PNG' && png.readUInt32BE(16) > 0
  check(pngOk, 'Save PNG did not produce a PNG')
  check(/^.+-diagram-1\.png$/.test(download.suggestedFilename()), `PNG name: ${download.suggestedFilename()}`)
  log(`  save png  ${download.suggestedFilename()} ${png.readUInt32BE(16)}x${png.readUInt32BE(20)}`)

  check((await page.evaluate(() => window.__pwned ?? null)) === null, 'hostile: script from model output ran')
  check((await page.evaluate(() => [...document.body.children].filter((e) => /mitss-mermaid/.test(e.id)).length)) === 0,
    'Mermaid scratch nodes left in the page')
  const leaked = stray.filter((r) => answered.has(r) || !refused.has(r)).map((r) => r.url())
  check(leaked.length === 0, `requests beyond the app's own assets and API: ${leaked.join(', ')}`)
  check(stray.length > 0, 'hostile: no image fetch was even attempted, so the CSP went untested')
  // The CSP reports each refusal as a console error; those are expected.
  const strayUrls = stray.map((r) => r.url())
  failures.push(...problems.filter((p) => !(/Content Security Policy/.test(p) && strayUrls.some((u) => p.includes(u)))))
  log(`  blocked   ${stray.length} image fetch(es) named in model output, refused by CSP`)
} catch (error) {
  failures.push(`check could not run: ${error.message}`)
} finally {
  await browser?.close()
  for (const child of children) child.kill()
  fs.rmSync(root, { recursive: true, force: true })
}

if (failures.length) {
  log(`\nFAIL (${failures.length})`)
  for (const f of failures) log(`  - ${f}`)
  process.exit(1)
}
log('\nOK')
