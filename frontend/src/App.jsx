import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api'
import Timeline from './components/Timeline'
import {
  Card, CopyButton, Dropzone, Issues, ScheduleTable, Steps, Tiles,
} from './components/Panels'

const SAMPLE = `SESSION: demo-001
DOMAIN: field-ops
HORIZON: 2026-08-11 08:00 -> 18:00
GRID: 15min
OBJECTIVE: finish as early as possible

RESOURCE: alpha | Team Alpha | cap 1
RESOURCE: bravo | Team Bravo | cap 2

TASK: t1 | Site survey     | 120min
TASK: t2 | Equipment setup | 1h30m | after t1
TASK: t3 | Calibration     | 1h    | after t2 | needs alpha | by 16:00
TASK: t4 | Teardown        | 45min | after t3`

export default function App() {
  const [run, setRun] = useState(null)        // current run payload
  const [runs, setRuns] = useState([])
  const [llm, setLlm] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [planReply, setPlanReply] = useState('')
  const [scheduleReply, setScheduleReply] = useState('')
  const [modelName, setModelName] = useState('')
  const [note, setNote] = useState('')

  const refreshRuns = useCallback(async () => {
    try {
      const body = await api.runs()
      setRuns(body.runs)
    } catch {
      // The run list is supporting information; a failure here should not
      // interrupt whatever the operator is doing.
    }
  }, [])

  useEffect(() => {
    api.llm().then(setLlm).catch(() => setLlm(null))
    refreshRuns()
  }, [refreshRuns])

  const guard = async (work) => {
    setBusy(true)
    setError('')
    try {
      return await work()
    } catch (exc) {
      setError(exc.message || String(exc))
      return null
    } finally {
      setBusy(false)
      refreshRuns()
    }
  }

  const onFile = (file) =>
    guard(async () => {
      const body = await api.upload(file)
      setRun(body)
      setPlanReply('')
      setScheduleReply('')
      return body
    })

  const onSample = () =>
    onFile(new File([SAMPLE], 'demo-001.txt', { type: 'text/plain' }))

  const onAttachPlan = () =>
    guard(async () => {
      const body = await api.attachPlan(run.run_id, planReply)
      setRun((previous) => ({ ...previous, ...body }))
      return body
    })

  const onIngest = () =>
    guard(async () => {
      const body = await api.ingest(run.run_id, scheduleReply, modelName, note)
      setRun((previous) => ({ ...previous, ...body }))
      return body
    })

  const onOpenRun = (runId) =>
    guard(async () => {
      const body = await api.run(runId)
      setRun(body)
      setModelName(body.model || '')
      setNote(body.note || '')
      setScheduleReply('')
      setPlanReply('')
      return body
    })

  // Which tasks carry an error or a warning, so the timeline and table can mark
  // them. Derived from the issue list rather than recomputed on the client.
  const { flaggedTasks, warnedTasks, flaggedResources } = useMemo(() => {
    const flagged = new Set()
    const warned = new Set()
    // Some errors belong to a resource rather than one task — an over-capacity
    // clash implicates every bar in the row, so the row carries the mark.
    const resources = new Map()

    for (const issue of run?.issues || []) {
      const where = issue.where || ''
      const resourceMatch = /^resource:(\S+)/.exec(where)
      if (resourceMatch) {
        if (issue.severity === 'error') {
          const id = resourceMatch[1]
          resources.set(id, [...(resources.get(id) || []), issue.message])
        }
        continue
      }
      const match = /(?:assignment|task):(\S+)/.exec(where)
      const id = match ? match[1] : null
      if (!id) continue
      if (issue.severity === 'error') flagged.add(id)
      else if (issue.severity === 'warn') warned.add(id)
    }
    return { flaggedTasks: flagged, warnedTasks: warned, flaggedResources: resources }
  }, [run])

  const status = run?.status
  const step = !run ? 0 : status === 'needs_llm' ? 1 : status === 'ready' ? 2 : 3
  const packet = run?.packet || run?.structuring_packet || ''
  const errorCount = (run?.issues || []).filter((i) => i.severity === 'error').length

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <h1>MITSS</h1>
          <p>Upload a plan, carry the packet to your model, get the answer checked.</p>
        </div>
        <div className="row">
          {llm && (
            <span className={`chip ${llm.available ? 'ok' : 'wait'}`}>
              <span className="dot" />
              model harness: {llm.provider}{llm.available ? '' : ' (paste)'}
            </span>
          )}
        </div>
      </header>

      <Steps current={step} />

      {error && <div className="banner error">{error}</div>}

      <Card
        title="1 · Upload a plan"
        hint="A .txt file. The backend parses the structured grammar directly; free-form text falls back to your model."
        right={<button onClick={onSample} disabled={busy}>Load sample</button>}
      >
        <Dropzone onFile={onFile} busy={busy} />
        {run && (
          <div className="row" style={{ marginTop: 12 }}>
            <span className="chip"><span className="dot" />{run.run_id}</span>
            <span className={`chip ${status === 'ingested' ? 'ok' : status === 'rejected' ? 'bad' : 'wait'}`}>
              <span className="dot" />{status}
            </span>
            {run.plan && <span className="chip">{run.plan.tasks.length} tasks · {run.plan.resources.length} resources</span>}
          </div>
        )}
      </Card>

      {run && status === 'needs_llm' && (
        <Card
          title="2 · Have your model structure it"
          hint="The text did not match the grammar, so it needs structuring first. Copy this packet to your model, then paste the reply back."
          right={<CopyButton text={packet} />}
        >
          <Issues issues={run.issues} empty="the file did not use the structured grammar at all" />
          <div className="spacer" />
          <pre className="packet">{packet}</pre>
          <div className="spacer" />
          <label className="field">Your model's reply</label>
          <textarea
            value={planReply}
            onChange={(event) => setPlanReply(event.target.value)}
            placeholder="Paste the whole reply. Prose around the json block is fine."
          />
          <div className="spacer" />
          <button className="primary" onClick={onAttachPlan} disabled={busy || !planReply.trim()}>
            Accept plan
          </button>
        </Card>
      )}

      {run && run.plan && (status === 'ready' || status === 'ingested' || status === 'rejected') && (
        <Card
          title={`${status === 'ready' ? '2' : '2'} · Scheduling packet`}
          hint="Copy this to your model. It carries the plan and every rule the answer will be checked against."
          right={<CopyButton text={run.packet || ''} />}
        >
          <pre className="packet">{run.packet}</pre>
        </Card>
      )}

      {run && run.plan && (
        <Card
          title="3 · Paste the schedule back"
          hint="Record which model answered so a later diff between runs still means something."
        >
          <div className="row">
            <div className="grow">
              <label className="field">Model name</label>
              <input
                type="text"
                value={modelName}
                onChange={(event) => setModelName(event.target.value)}
                placeholder="your-custom-llm"
              />
            </div>
            <div className="grow">
              <label className="field">Note (optional)</label>
              <input
                type="text"
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="baseline, second attempt, …"
              />
            </div>
          </div>
          <div className="spacer" />
          <label className="field">Model reply</label>
          <textarea
            value={scheduleReply}
            onChange={(event) => setScheduleReply(event.target.value)}
            placeholder="Paste the whole reply. Prose around the json block is fine."
          />
          <div className="spacer" />
          <div className="row">
            <button className="primary" onClick={onIngest} disabled={busy || !scheduleReply.trim()}>
              Check schedule
            </button>
            {run.schedule && (
              <a href={api.csvUrl(run.run_id)} download>
                <button type="button">Download CSV</button>
              </a>
            )}
          </div>
        </Card>
      )}

      {run && run.schedule && (
        <>
          <Card
            title="4 · Result"
            right={
              <span className={`chip ${run.legal === false || status === 'rejected' ? 'bad' : 'ok'}`}>
                <span className="dot" />
                {status === 'rejected' || run.legal === false
                  ? `${errorCount} constraint error${errorCount === 1 ? '' : 's'}`
                  : 'legal schedule'}
              </span>
            }
          >
            <Tiles summary={run.summary} />
            <div className="spacer" />
            <div className="spacer" />
            <Timeline
              plan={run.plan}
              schedule={run.schedule}
              flaggedTasks={flaggedTasks}
              warnedTasks={warnedTasks}
              flaggedResources={flaggedResources}
            />
          </Card>

          <Card title="Assignments">
            <ScheduleTable plan={run.plan} schedule={run.schedule} flaggedTasks={flaggedTasks} />
          </Card>

          <Card title="Checks" hint="Everything the harness found, worst first.">
            <Issues issues={run.issues} empty="every check passed" />
          </Card>
        </>
      )}

      {runs.length > 0 && (
        <Card title="Runs" hint="Every upload is kept, including the ones that failed.">
          <div className="runs">
            {runs.map((entry) => (
              <button
                key={entry.run_id}
                className={`run${run?.run_id === entry.run_id ? ' active' : ''}`}
                onClick={() => onOpenRun(entry.run_id)}
              >
                <span>
                  <span className="rid">{entry.run_id}</span>
                  {entry.model && <span className="muted"> · {entry.model}</span>}
                </span>
                <span className={`chip ${entry.status === 'ingested' ? 'ok' : entry.status === 'rejected' ? 'bad' : 'wait'}`}>
                  <span className="dot" />{entry.status}
                </span>
              </button>
            ))}
          </div>
        </Card>
      )}
    </div>
  )
}
