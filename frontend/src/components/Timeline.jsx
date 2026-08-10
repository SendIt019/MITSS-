import { useMemo, useRef, useState } from 'react'

// A Gantt-style timeline: one row per resource, time running left to right
// across the plan horizon.
//
// Colour carries exactly one job here — status. Identity is carried by the row
// (which resource) and the label on the bar (which task), so the fill is not
// asked to encode identity as well. Clean blocks use the validated series blue;
// blocks carrying a constraint error use status critical AND an icon, never
// colour alone. Warnings never recolour a bar: the warning yellow fails the
// fill lightness band, so warned blocks get the icon and the issues list.

const parse = (value) => new Date(value.replace(' ', 'T')).getTime()

function niceTicks(startMs, endMs, target = 8) {
  const spanMinutes = (endMs - startMs) / 60000
  const steps = [15, 30, 60, 120, 180, 240, 360, 720, 1440]
  const step = steps.find((s) => spanMinutes / s <= target) || 1440
  const ticks = []
  const first = Math.ceil(startMs / (step * 60000)) * step * 60000
  for (let t = first; t <= endMs; t += step * 60000) ticks.push(t)
  return ticks
}

function clockLabel(ms, spanMinutes) {
  const date = new Date(ms)
  const time = date.toTimeString().slice(0, 5)
  if (spanMinutes > 1440) {
    return `${date.toISOString().slice(5, 10)} ${time}`
  }
  return time
}

export default function Timeline({
  plan,
  schedule,
  flaggedTasks = new Set(),
  warnedTasks = new Set(),
  flaggedResources = new Map(),
}) {
  const [hover, setHover] = useState(null)
  const trackRef = useRef(null)

  const { startMs, endMs, rows, ticks, spanMinutes } = useMemo(() => {
    const start = parse(plan.horizon.start)
    const end = parse(plan.horizon.end)
    const byResource = new Map(plan.resources.map((r) => [r.id, []]))

    for (const assignment of schedule?.assignments || []) {
      if (!byResource.has(assignment.resource_id)) byResource.set(assignment.resource_id, [])
      byResource.get(assignment.resource_id).push(assignment)
    }

    // Overlapping work on one resource is stacked into lanes rather than drawn
    // on top of itself. Without this the later bar simply paints over the
    // earlier one and a double-booking — the thing most worth seeing — becomes
    // invisible. Greedy interval partitioning: reuse the first lane that has
    // already finished, otherwise open a new one.
    const laid = [...byResource.entries()].map(([resourceId, assignments]) => {
      const sorted = [...assignments].sort((a, b) => parse(a.start) - parse(b.start))
      const laneEnds = []
      const placed = sorted.map((assignment) => {
        const from = parse(assignment.start)
        const to = parse(assignment.end)
        let lane = laneEnds.findIndex((endsAt) => endsAt <= from)
        if (lane === -1) {
          lane = laneEnds.length
          laneEnds.push(to)
        } else {
          laneEnds[lane] = to
        }
        return { assignment, lane, from, to }
      })
      return { resourceId, placed, lanes: Math.max(1, laneEnds.length) }
    })

    return {
      startMs: start,
      endMs: end,
      spanMinutes: (end - start) / 60000,
      rows: laid,
      ticks: niceTicks(start, end),
    }
  }, [plan, schedule])

  const span = endMs - startMs
  const pct = (ms) => `${((ms - startMs) / span) * 100}%`
  const widthPct = (a, b) => `${((b - a) / span) * 100}%`

  const taskName = (taskId) => plan.tasks.find((t) => t.id === taskId)?.name || taskId

  const showTip = (event, assignment) => {
    const task = plan.tasks.find((t) => t.id === assignment.task_id)
    setHover({
      x: event.clientX,
      y: event.clientY,
      title: taskName(assignment.task_id),
      lines: [
        `${assignment.start.slice(11, 16)} to ${assignment.end.slice(11, 16)}`,
        `${(parse(assignment.end) - parse(assignment.start)) / 60000} min on ${assignment.resource_id}`,
        task?.depends_on?.length ? `after ${task.depends_on.join(', ')}` : null,
        flaggedTasks.has(assignment.task_id) ? 'has a constraint error' : null,
        warnedTasks.has(assignment.task_id) ? 'has a warning' : null,
      ].filter(Boolean),
    })
  }

  const anyFlagged = rows.some(({ placed }) =>
    placed.some(({ assignment }) => flaggedTasks.has(assignment.task_id)))
  const anyStacked = rows.some(({ lanes }) => lanes > 1)

  const LANE_HEIGHT = 26
  const BAR_HEIGHT = 22

  return (
    <div className="timeline">
      {rows.map(({ resourceId, placed, lanes }) => (
        <div className="tl-row" key={resourceId}>
          <div
            className={`tl-label${flaggedResources.has(resourceId) ? ' flagged' : ''}`}
            title={flaggedResources.get(resourceId)?.join('\n') || resourceId}
          >
            {flaggedResources.has(resourceId) ? '✕ ' : ''}{resourceId}
          </div>
          <div className="tl-track" style={{ height: lanes * LANE_HEIGHT + 8 }} ref={trackRef}>
            {ticks.map((t) => (
              <div className="tl-gridline" key={t} style={{ left: pct(t) }} />
            ))}
            {placed.map(({ assignment, lane, from, to }) => {
              const flagged = flaggedTasks.has(assignment.task_id)
              const warned = warnedTasks.has(assignment.task_id)
              return (
                <div
                  key={`${assignment.task_id}-${assignment.start}`}
                  className={`tl-bar${flagged ? ' flagged' : ''}`}
                  style={{
                    left: pct(from),
                    width: widthPct(from, to),
                    top: 4 + lane * LANE_HEIGHT,
                    height: BAR_HEIGHT,
                  }}
                  onMouseMove={(event) => showTip(event, assignment)}
                  onMouseLeave={() => setHover(null)}
                  title={`${assignment.task_id}: ${assignment.start} to ${assignment.end}`}
                >
                  {flagged ? '✕ ' : warned ? '⚠ ' : ''}
                  {assignment.task_id}
                </div>
              )
            })}
          </div>
        </div>
      ))}

      <div className="tl-row">
        <div className="tl-label" />
        <div className="tl-axis">
          {ticks.map((t) => (
            <span className="tl-tick" key={t} style={{ left: pct(t) }}>
              {clockLabel(t, spanMinutes)}
            </span>
          ))}
        </div>
      </div>

      <div className="legend">
        <span className="key">
          <span className="swatch" style={{ background: 'var(--series-1)' }} />
          scheduled
        </span>
        {anyFlagged && (
          <span className="key">
            <span className="swatch" style={{ background: 'var(--status-critical)' }} />
            ✕ constraint error
          </span>
        )}
        {warnedTasks.size > 0 && <span className="key">⚠ warning — see the issues list</span>}
        {anyStacked && <span className="key">stacked rows mean concurrent work on one resource</span>}
      </div>

      {hover && (
        <div className="tooltip" style={{ left: hover.x + 14, top: hover.y + 14 }}>
          <div className="t-title">{hover.title}</div>
          {hover.lines.map((line) => (
            <div className="t-line" key={line}>{line}</div>
          ))}
        </div>
      )}
    </div>
  )
}
