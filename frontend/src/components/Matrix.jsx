import { Empty, Verdict } from './Panels'

// Prompt versions down the side, models across the top. Each cell shows the
// headline verdict for that pairing — the worst recorded outcome, because a
// model that got it wrong once is the thing worth noticing.

export default function Matrix({ matrix, onPick, selected = [] }) {
  if (!matrix) return null
  const { versions, models, cells, version_notes: notes = {} } = matrix

  if (!models.length) {
    return <Empty>No outputs recorded yet. Record one and this grid fills in.</Empty>
  }

  return (
    <div className="matrix-wrap">
      <table className="matrix">
        <thead>
          <tr>
            <th className="corner">version</th>
            {models.map((model) => (
              <th key={model} title={model}>{model}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {versions.map((version) => (
            <tr key={version}>
              <th className="rowhead" title={notes[version] || ''}>
                v{version}
                {notes[version] && <span className="note">{notes[version]}</span>}
              </th>
              {models.map((model) => {
                const cell = cells[`${version}|${model}`]
                if (!cell || cell.count === 0) {
                  return <td key={model} className="cell blank">—</td>
                }
                const latest = cell.runs[0]
                const isSelected = selected.includes(latest.id)
                return (
                  <td key={model} className="cell">
                    <button
                      className={`cell-button${isSelected ? ' selected' : ''}`}
                      onClick={() => onPick?.(latest.id)}
                      title={`${cell.count} run${cell.count === 1 ? '' : 's'} — click to open`}
                    >
                      <Verdict value={cell.verdict} count={cell.count} />
                      <span className="cell-sub">{latest.output_words} words</span>
                    </button>
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
