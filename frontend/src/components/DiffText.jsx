// Renders the word-level diff spans the backend produces.
//
// Colour is not the only channel: removed text is struck through and added
// text underlined, so the diff survives greyscale printing, colour-vision
// deficiency, and forced-colours mode.

export default function DiffText({ spans, side, plain }) {
  if (plain || !spans) {
    return <pre className="output">{plain}</pre>
  }
  return (
    <pre className="output">
      {spans.map((span, index) => (
        <span key={index} className={`sp-${span.kind}`}>{span.text}</span>
      ))}
    </pre>
  )
}

export function DiffLegend({ diff }) {
  if (!diff) return null
  if (diff.identical) {
    return <p className="hint diff-legend">The two outputs are character-for-character identical.</p>
  }
  return (
    <p className="hint diff-legend">
      <span className="sp-removed">struck through</span> is only on the left,{' '}
      <span className="sp-added">underlined</span> is only on the right.{' '}
      {diff.removed_words} removed, {diff.added_words} added,{' '}
      {Math.round((diff.similarity || 0) * 100)}% similar.
    </p>
  )
}
