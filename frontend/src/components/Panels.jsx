import { useRef, useState } from 'react'

// Small presentational pieces shared across the app.

export function Card({ title, hint, children, right }) {
  return (
    <section className="card">
      {(title || right) && (
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: hint ? 2 : 12 }}>
          {title && <h2>{title}</h2>}
          {right}
        </div>
      )}
      {hint && <p className="hint">{hint}</p>}
      {children}
    </section>
  )
}

export function Dropzone({ onFile, busy }) {
  const [over, setOver] = useState(false)
  const inputRef = useRef(null)

  const take = (fileList) => {
    const file = fileList?.[0]
    if (file) onFile(file)
  }

  return (
    <div
      className={`dropzone${over ? ' over' : ''}`}
      onClick={() => inputRef.current?.click()}
      onDragOver={(event) => { event.preventDefault(); setOver(true) }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault()
        setOver(false)
        take(event.dataTransfer.files)
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".txt,.text,.md"
        style={{ display: 'none' }}
        onChange={(event) => take(event.target.files)}
      />
      {busy ? (
        <span>reading…</span>
      ) : (
        <>
          <div><strong>Drop a .txt plan here</strong> or click to choose one</div>
          <div style={{ fontSize: 12.5, marginTop: 6 }}>
            Structured files parse instantly. Anything else gets handed to your model to structure.
          </div>
        </>
      )}
    </div>
  )
}

export function Issues({ issues, empty = 'no issues' }) {
  if (!issues || issues.length === 0) {
    return <p className="hint" style={{ margin: 0 }}>{empty}</p>
  }
  const rank = { error: 0, warn: 1, info: 2 }
  const sorted = [...issues].sort((a, b) => (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9))
  return (
    <div className="issues">
      {sorted.map((issue, index) => (
        <div className={`issue ${issue.severity}`} key={`${issue.code}-${issue.where}-${index}`}>
          <span className="sev">{issue.severity}</span>
          <span className="where">{issue.where || '—'}</span>
          <span>{issue.message}</span>
        </div>
      ))}
    </div>
  )
}

export function Tiles({ summary }) {
  if (!summary) return null
  const utilization = summary.resource_utilization || {}
  const busiest = Object.entries(utilization)
    .sort((a, b) => (b[1].utilization_pct || 0) - (a[1].utilization_pct || 0))[0]

  return (
    <div className="tiles">
      <div className="tile">
        <div className="label">Tasks scheduled</div>
        <div className="value">{summary.tasks_scheduled}<span className="sub"> / {summary.tasks_total}</span></div>
        {summary.tasks_unscheduled > 0 && (
          <div className="sub">{summary.tasks_unscheduled} left unscheduled</div>
        )}
      </div>
      <div className="tile">
        <div className="label">Makespan</div>
        <div className="value">{summary.makespan_minutes}<span className="sub"> min</span></div>
        <div className="sub">of {summary.horizon_minutes} min horizon</div>
      </div>
      <div className="tile">
        <div className="label">Finishes</div>
        <div className="value" style={{ fontSize: 18 }}>
          {summary.finish_time ? summary.finish_time.slice(11, 16) : '—'}
        </div>
        <div className="sub">{summary.finish_time ? summary.finish_time.slice(0, 10) : 'nothing placed'}</div>
      </div>
      {busiest && (
        <div className="tile">
          <div className="label">Busiest resource</div>
          <div className="value" style={{ fontSize: 18 }}>{busiest[0]}</div>
          <div className="sub">{busiest[1].utilization_pct}% utilized</div>
        </div>
      )}
    </div>
  )
}

export function ScheduleTable({ plan, schedule, flaggedTasks = new Set() }) {
  if (!schedule) return null
  const sorted = [...schedule.assignments].sort((a, b) => a.start.localeCompare(b.start))
  const name = (taskId) => plan.tasks.find((t) => t.id === taskId)?.name || taskId

  return (
    <>
      <table>
        <thead>
          <tr>
            <th>Start</th>
            <th>End</th>
            <th className="num">Min</th>
            <th>Task</th>
            <th>Resource</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((assignment) => {
            const minutes = (new Date(assignment.end) - new Date(assignment.start)) / 60000
            const flagged = flaggedTasks.has(assignment.task_id)
            return (
              <tr key={`${assignment.task_id}-${assignment.start}`} className={flagged ? 'flagged' : ''}>
                <td className="num">{assignment.start.slice(11, 16)}</td>
                <td className="num">{assignment.end.slice(11, 16)}</td>
                <td className="num">{minutes}</td>
                <td>{flagged ? '✕ ' : ''}{name(assignment.task_id)} <span className="muted mono">{assignment.task_id}</span></td>
                <td>{assignment.resource_id}</td>
              </tr>
            )
          })}
        </tbody>
      </table>

      {schedule.unscheduled?.length > 0 && (
        <>
          <div className="spacer" />
          <p className="hint" style={{ marginBottom: 6 }}>Left unscheduled by the model:</p>
          <table>
            <tbody>
              {schedule.unscheduled.map((entry) => (
                <tr key={entry.task_id}>
                  <td>{name(entry.task_id)} <span className="muted mono">{entry.task_id}</span></td>
                  <td>{entry.reason || <span className="muted">no reason given</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  )
}

export function CopyButton({ text, label = 'Copy packet' }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
        } catch {
          // Clipboard permission can be denied; the packet is on screen anyway.
        }
        setCopied(true)
        setTimeout(() => setCopied(false), 1600)
      }}
      disabled={!text}
    >
      {copied ? 'Copied' : label}
    </button>
  )
}

export function Steps({ current }) {
  const steps = ['Upload', 'Packet', 'Paste reply', 'Result']
  return (
    <div className="steps">
      {steps.map((label, index) => {
        const state = index < current ? 'done' : index === current ? 'current' : ''
        return (
          <div className={`step ${state}`} key={label}>
            <span className="n">{index < current ? '✓' : index + 1}</span>
            {label}
          </div>
        )
      })}
    </div>
  )
}
