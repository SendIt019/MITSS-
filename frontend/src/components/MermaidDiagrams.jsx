import { Component, useEffect, useMemo, useState } from 'react'
import { extractMermaidBlocks } from '../mermaidBlocks'
import { renderMermaid, savePng } from '../mermaidRender'

// Draws every ```mermaid block in a run's output, in order, below the output
// text. Renders nothing at all when there are none, so those runs look exactly
// as they did before. A block that cannot be drawn shows why and its raw code
// in its place; it never leaves a gap or takes the panel down with it.

export default function MermaidDiagrams({ text, runId }) {
  const blocks = useMemo(() => extractMermaidBlocks(text), [text])
  if (blocks.length === 0) return null

  return (
    <div className="diagrams">
      <h3 className="section">
        {blocks.length === 1 ? 'Diagram' : `Diagrams (${blocks.length})`}
      </h3>
      {blocks.map((block, index) => (
        <Guard key={`${runId}-${index}`} block={block} index={index}>
          <Diagram block={block} index={index} runId={runId} />
        </Guard>
      ))}
    </div>
  )
}

function Diagram({ block, index, runId }) {
  const [state, setState] = useState({ status: 'drawing' })
  const [saveError, setSaveError] = useState('')

  useEffect(() => {
    let live = true
    setState({ status: 'drawing' })
    setSaveError('')
    renderMermaid(block.code).then(
      (image) => live && setState({ status: 'drawn', image }),
      (error) => live && setState({ status: 'failed', error: describe(error) }),
    )
    return () => { live = false }
  }, [block.code])

  const label = `Diagram ${index + 1}`

  if (state.status === 'failed') {
    return <Failed block={block} label={label} error={state.error} />
  }

  if (state.status === 'drawing') {
    return (
      <figure className="diagram">
        <div className="diagram-pending">Drawing {label.toLowerCase()}…</div>
      </figure>
    )
  }

  const { image } = state
  return (
    <figure className="diagram">
      <div className="diagram-canvas">
        <img
          src={image.src}
          width={image.width}
          height={image.height}
          alt={`${label} from the model's Mermaid code`}
        />
      </div>
      <figcaption className="diagram-actions">
        <span className="hint">{label}</span>
        <button
          onClick={() => {
            setSaveError('')
            savePng(image, pngName(runId, index)).catch((error) =>
              setSaveError(`Could not save a PNG: ${describe(error)}`))
          }}
        >
          Save PNG
        </button>
        {saveError && <span className="diagram-error-inline">{saveError}</span>}
      </figcaption>
    </figure>
  )
}

function Failed({ block, label, error }) {
  return (
    <figure className="diagram failed">
      <p className="diagram-error">
        <strong>{label} could not be drawn.</strong> {error}
        {block.unclosed && ' The block has no closing fence, so the output may have been cut off.'}
      </p>
      <pre className="output">{block.code}</pre>
    </figure>
  )
}

// If anything in a diagram throws while rendering, show that block's code
// instead of unmounting the whole run panel.
class Guard extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <Failed
          block={this.props.block}
          label={`Diagram ${this.props.index + 1}`}
          error={describe(this.state.error)}
        />
      )
    }
    return this.props.children
  }
}

function describe(error) {
  const message = (error && (error.message || error.str || String(error))) || 'unknown error'
  // Mermaid's image shapes wait for the picture, which the page's content
  // security policy refuses to fetch (index.html).
  if (/image cannot be decoded/i.test(message)) {
    return 'It links an image, and images named in model output are never loaded.'
  }
  return message.trim()
}

function pngName(runId, index) {
  const safe = String(runId || 'run').replace(/[^A-Za-z0-9_-]/g, '_')
  return `${safe}-diagram-${index + 1}.png`
}
