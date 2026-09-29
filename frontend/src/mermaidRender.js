// Draws Mermaid code into an inert SVG image, and saves one as a PNG.
//
// Model output is untrusted, so there are two layers:
//   1. Mermaid runs with securityLevel "strict": it escapes HTML in labels and
//      disables click handlers. `secure` keeps a %%{init}%% directive in the
//      model's code from turning that, or anything else below, back off.
//   2. The SVG it produces is never put into the page's DOM. It is shown as an
//      <img> from a data: URL, and an SVG loaded as an image cannot run
//      script, follow links or fetch anything.
//
// Labels are plain SVG text (htmlLabels off), not HTML inside
// <foreignObject>: an image containing foreignObject taints the canvas, which
// would make "Save PNG" fail.
//
// The mermaid library is loaded on first use, so outputs without a diagram
// never download it. It is bundled by Vite, so this works offline.

let ready = null
let queue = Promise.resolve()
let counter = 0

function load() {
  if (!ready) {
    ready = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        secure: [
          'secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'maxEdges',
          'suppressErrorRendering', 'htmlLabels', 'fontFamily',
          'themeCSS', 'dompurifyConfig',
        ],
        suppressErrorRendering: true,
        htmlLabels: false,
        flowchart: { htmlLabels: false },
        theme: 'default',
        fontFamily: '"trebuchet ms", verdana, arial, sans-serif',
      })
      return mermaid
    }).catch((error) => {
      ready = null // let a later block try again
      throw new Error(`could not load the diagram library: ${error.message || error}`)
    })
  }
  return ready
}

// Mermaid renders through shared global state, so one diagram at a time.
function serial(task) {
  const run = queue.then(task, task)
  queue = run.catch(() => {})
  return run
}

function removeLeftovers(id) {
  for (const stray of [id, `d${id}`, `i${id}`]) {
    document.getElementById(stray)?.remove()
  }
}

// Mermaid's SVG sizes itself with width="100%" and a max-width style, which an
// <img> cannot resolve. Pin the width and height from the viewBox instead.
function toImage(svgText) {
  const doc = new DOMParser().parseFromString(svgText, 'text/html')
  const svg = doc.querySelector('svg')
  if (!svg) throw new Error('the diagram library returned no drawing')
  const box = (svg.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number)
  let width = box[2]
  let height = box[3]
  if (!(width > 0 && height > 0)) {
    width = parseFloat(svg.getAttribute('width')) || 600
    height = parseFloat(svg.getAttribute('height')) || 400
  }
  width = Math.ceil(width)
  height = Math.ceil(height)
  svg.setAttribute('width', String(width))
  svg.setAttribute('height', String(height))
  svg.style.removeProperty('max-width')
  svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  const xml = new XMLSerializer().serializeToString(svg)
  return {
    src: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(xml)}`,
    width,
    height,
  }
}

// Resolves to { src, width, height }, or rejects with the parse or render
// error. Never leaves Mermaid's scratch elements behind in the page.
export function renderMermaid(code) {
  return serial(async () => {
    const mermaid = await load()
    const id = `mitss-mermaid-${++counter}`
    try {
      await mermaid.parse(code)
      const { svg } = await mermaid.render(id, code)
      return toImage(svg)
    } finally {
      removeLeftovers(id)
    }
  })
}

// Draws the diagram onto a white canvas at twice its size and downloads it.
export async function savePng(image, filename) {
  const img = new Image()
  img.src = image.src
  await img.decode()
  const scale = 2
  const canvas = document.createElement('canvas')
  canvas.width = image.width * scale
  canvas.height = image.height * scale
  const context = canvas.getContext('2d')
  context.fillStyle = '#ffffff'
  context.fillRect(0, 0, canvas.width, canvas.height)
  context.drawImage(img, 0, 0, canvas.width, canvas.height)
  const blob = await new Promise((resolve, reject) => {
    try {
      canvas.toBlob((result) => (result ? resolve(result) : reject(new Error('the browser produced no image'))), 'image/png')
    } catch (error) {
      reject(error)
    }
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
