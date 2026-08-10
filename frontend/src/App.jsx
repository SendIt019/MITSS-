import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api'
import DiffText, { DiffLegend } from './components/DiffText'
import Matrix from './components/Matrix'
import {
  Card, CopyButton, Dropzone, Empty, Field, Stat, Tabs, Verdict, VerdictPicker,
  timeAgo, verdictMeta,
} from './components/Panels'

const SAMPLE = `You are extracting structured facts from an incident report.

Read the report below and return, for each distinct event:
  - what happened, in one sentence
  - when it happened
  - which system was affected
  - whether it was resolved

Return the events in the order they occurred. If a detail is not stated
in the report, write "not stated" rather than inferring it.

REPORT:
{paste the report here}`

export default function App() {
  const [prompts, setPrompts] = useState([])
  const [prompt, setPrompt] = useState(null)
  const [draft, setDraft] = useState('')
  const [versionNote, setVersionNote] = useState('')
  const [viewVersion, setViewVersion] = useState(null)

  const [runs, setRuns] = useState([])
  const [matrix, setMatrix] = useState(null)
  const [openRun, setOpenRun] = useState(null)

  const [outputText, setOutputText] = useState('')
  const [modelName, setModelName] = useState('')
  const [runNotes, setRunNotes] = useState('')

  const [compareA, setCompareA] = useState('')
  const [compareB, setCompareB] = useState('')
  const [comparison, setComparison] = useState(null)

  const [tab, setTab] = useState('prompt')
  const [llm, setLlm] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [flash, setFlash] = useState('')

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
    }
  }

  const say = (message) => {
    setFlash(message)
    setTimeout(() => setFlash(''), 2500)
  }

  const refreshPrompts = useCallback(async () => {
    const body = await api.prompts().catch(() => null)
    if (body) setPrompts(body.prompts)
  }, [])

  useEffect(() => {
    api.llm().then(setLlm).catch(() => setLlm(null))
    refreshPrompts()
  }, [refreshPrompts])

  const loadPrompt = useCallback(async (id, version) => {
    const detail = await api.prompt(id, version)
    setPrompt(detail)
    setDraft(detail.selected_version?.text || '')
    setViewVersion(detail.selected_version?.version ?? null)
    setVersionNote('')

    const [runsBody, grid] = await Promise.all([
      api.runs(id).catch(() => ({ runs: [] })),
      api.matrix(id).catch(() => null),
    ])
    setRuns(runsBody.runs)
    setMatrix(grid)
    setOpenRun(null)
    setComparison(null)
    setCompareA('')
    setCompareB('')
    return detail
  }, [])

  const refreshCurrent = useCallback(async () => {
    if (prompt) await loadPrompt(prompt.id, viewVersion || undefined)
  }, [prompt, viewVersion, loadPrompt])

  // -- prompt actions --------------------------------------------------

  const onNewPrompt = () =>
    guard(async () => {
      const created = await api.createPrompt('Untitled prompt', SAMPLE, 'starting point')
      await refreshPrompts()
      await loadPrompt(created.id)
      setTab('prompt')
      say('New prompt created')
    })

  const onUpload = (file, intoCurrent) =>
    guard(async () => {
      const body = await api.upload(file, intoCurrent && prompt ? prompt.id : '')
      await refreshPrompts()
      await loadPrompt(body.id)
      setTab('prompt')
      say(intoCurrent ? 'Saved as a new version' : 'Prompt created from file')
    })

  const onSaveVersion = () =>
    guard(async () => {
      await api.addVersion(prompt.id, draft, versionNote)
      await refreshPrompts()
      await loadPrompt(prompt.id)
      say('New version saved')
    })

  const onRename = (name) =>
    guard(async () => {
      await api.renamePrompt(prompt.id, name)
      await refreshPrompts()
      await loadPrompt(prompt.id, viewVersion || undefined)
    })

  // -- run actions -----------------------------------------------------

  const onRecord = () =>
    guard(async () => {
      const run = await api.recordRun({
        prompt_id: prompt.id,
        version: viewVersion,
        model: modelName,
        output: outputText,
        notes: runNotes,
        verdict: 'unrated',
      })
      setOutputText('')
      setRunNotes('')
      await refreshCurrent()
      setOpenRun(run)
      setTab('runs')
      say('Output recorded — read it and set a verdict')
    })

  const onReview = (runId, verdict, notes) =>
    guard(async () => {
      const updated = await api.review(runId, verdict, notes)
      setOpenRun(updated)
      const [runsBody, grid] = await Promise.all([
        api.runs(prompt.id), api.matrix(prompt.id),
      ])
      setRuns(runsBody.runs)
      setMatrix(grid)
    })

  const onOpenRun = (runId) =>
    guard(async () => {
      const run = await api.run(runId)
      setOpenRun(run)
      setTab('runs')
    })

  const onDeleteRun = (runId) =>
    guard(async () => {
      await api.deleteRun(runId)
      setOpenRun(null)
      await refreshCurrent()
      say('Run deleted — it stays in the activity log')
    })

  const onCompare = () =>
    guard(async () => {
      const body = await api.compare(compareA, compareB)
      setComparison(body)
    })

  const onGenerate = () =>
    guard(async () => {
      const run = await api.generate(prompt.id, viewVersion, modelName)
      await refreshCurrent()
      setOpenRun(run)
      setTab('runs')
      say('Output fetched from your model')
    })

  // -- derived ---------------------------------------------------------

  const currentText = prompt?.selected_version?.text || ''
  const dirty = prompt && draft !== currentText
  const tally = matrix?.totals
  const unreviewed = runs.filter((r) => r.verdict === 'unrated').length

  const runLabel = (run) =>
    `v${run.version} · ${run.model || 'unnamed'} · ${timeAgo(run.created_at)}`

  const compareOptions = useMemo(
    () => runs.map((r) => ({ id: r.id, label: runLabel(r) })), [runs])

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <h1>MITSS</h1>
          <p>Write a prompt, run it through your model, record what came back.</p>
        </div>
        <div className="row">
          {llm && (
            <span className={`chip ${llm.available ? 'ok' : 'wait'}`}>
              <span className="dot" />
              {llm.provider}{llm.available ? '' : ' · paste'}
            </span>
          )}
        </div>
      </header>

      {error && <div className="banner error">{error}</div>}
      {flash && <div className="banner info">{flash}</div>}

      <div className="layout">
        <aside className="sidebar">
          <div className="side-head">
            <span>Prompts</span>
            <button onClick={onNewPrompt} disabled={busy}>New</button>
          </div>

          {prompts.length === 0 ? (
            <Empty>Nothing yet.</Empty>
          ) : (
            <div className="prompt-list">
              {prompts.map((entry) => (
                <button
                  key={entry.id}
                  className={`prompt-item${prompt?.id === entry.id ? ' active' : ''}`}
                  onClick={() => guard(() => loadPrompt(entry.id))}
                >
                  <span className="name">{entry.name}</span>
                  <span className="meta">
                    v{entry.latest_version} · {timeAgo(entry.updated_at)}
                  </span>
                </button>
              ))}
            </div>
          )}

          <div className="side-drop">
            <Dropzone onFile={(file) => onUpload(file, false)} busy={busy}
                      label="Drop a .txt prompt" />
          </div>
        </aside>

        <main className="main">
          {!prompt ? (
            <Card
              title="Start here"
              hint="Create a prompt, or drop a .txt file into the panel on the left."
            >
              <button className="primary" onClick={onNewPrompt} disabled={busy}>
                New prompt
              </button>
            </Card>
          ) : (
            <>
              <div className="prompt-head">
                <input
                  className="title-input"
                  value={prompt.name}
                  onChange={(event) => setPrompt({ ...prompt, name: event.target.value })}
                  onBlur={(event) => {
                    if (event.target.value.trim() && event.target.value !== prompt.name) return
                    onRename(event.target.value)
                  }}
                  onKeyDown={(event) => event.key === 'Enter' && event.target.blur()}
                />
                <div className="row">
                  {tally && (
                    <span className="chip">
                      {tally.total} run{tally.total === 1 ? '' : 's'}
                      {unreviewed > 0 && ` · ${unreviewed} unread`}
                    </span>
                  )}
                </div>
              </div>

              <Tabs
                value={tab}
                onChange={setTab}
                items={[
                  { value: 'prompt', label: 'Prompt' },
                  { value: 'runs', label: 'Outputs', badge: runs.length || null },
                  { value: 'matrix', label: 'Matrix' },
                  { value: 'compare', label: 'Compare' },
                ]}
              />

              {tab === 'prompt' && (
                <>
                  <Card
                    title={`Version ${viewVersion ?? '—'}${dirty ? ' (edited)' : ''}`}
                    hint="Editing never overwrites a version. Saving creates the next one, so every recorded output stays tied to the exact text that produced it."
                    right={
                      <div className="row">
                        <select
                          value={viewVersion ?? ''}
                          onChange={(event) =>
                            guard(() => loadPrompt(prompt.id, Number(event.target.value)))}
                        >
                          {prompt.versions.map((v) => (
                            <option key={v.version} value={v.version}>
                              v{v.version}{v.note ? ` — ${v.note}` : ''}
                            </option>
                          ))}
                        </select>
                        <CopyButton text={draft} />
                      </div>
                    }
                  >
                    <textarea
                      className="prompt-editor"
                      value={draft}
                      onChange={(event) => setDraft(event.target.value)}
                      spellCheck={false}
                    />
                    <div className="row" style={{ marginTop: 10 }}>
                      <Field label="What changed?">
                        <input
                          type="text"
                          value={versionNote}
                          onChange={(event) => setVersionNote(event.target.value)}
                          placeholder="tightened the output rules"
                        />
                      </Field>
                      <button className="primary" onClick={onSaveVersion} disabled={busy || !dirty}>
                        Save as v{(prompt.latest_version || 0) + 1}
                      </button>
                    </div>
                    {!dirty && (
                      <p className="hint" style={{ marginTop: 8, marginBottom: 0 }}>
                        No unsaved changes.
                      </p>
                    )}
                  </Card>

                  <Card
                    title="Record an output"
                    hint="Copy the prompt above, run it through your model, then paste what came back."
                    right={
                      llm?.available ? (
                        <button onClick={onGenerate} disabled={busy}>
                          Fetch from {llm.provider}
                        </button>
                      ) : null
                    }
                  >
                    <div className="row">
                      <Field label="Model">
                        <input
                          type="text"
                          value={modelName}
                          onChange={(event) => setModelName(event.target.value)}
                          placeholder="your-custom-llm"
                        />
                      </Field>
                      <Field label="Note (optional)">
                        <input
                          type="text"
                          value={runNotes}
                          onChange={(event) => setRunNotes(event.target.value)}
                          placeholder="temperature 0, second attempt…"
                        />
                      </Field>
                    </div>
                    <label className="field" style={{ marginTop: 10 }}>Output</label>
                    <textarea
                      className="output-input"
                      value={outputText}
                      onChange={(event) => setOutputText(event.target.value)}
                      placeholder="Paste the model's output verbatim."
                      spellCheck={false}
                    />
                    <div className="row" style={{ marginTop: 10 }}>
                      <button className="primary" onClick={onRecord}
                              disabled={busy || !outputText.trim() || dirty}>
                        Record output
                      </button>
                      {dirty && (
                        <span className="hint" style={{ margin: 0 }}>
                          Save the edited prompt first, so the output is tied to a real version.
                        </span>
                      )}
                    </div>
                  </Card>
                </>
              )}

              {tab === 'runs' && (
                <RunsTab
                  runs={runs}
                  openRun={openRun}
                  onOpenRun={onOpenRun}
                  onReview={onReview}
                  onDelete={onDeleteRun}
                  busy={busy}
                  runLabel={runLabel}
                />
              )}

              {tab === 'matrix' && (
                <Card
                  title="Versions against models"
                  hint="Each cell shows the worst verdict recorded for that pairing. Click one to read it."
                >
                  {tally && tally.total > 0 && (
                    <div className="tiles" style={{ marginBottom: 16 }}>
                      <Stat label="Outputs" value={tally.total} />
                      <Stat label="Accurate" value={tally.accurate} />
                      <Stat label="Partly right" value={tally.partial} />
                      <Stat label="Inaccurate" value={tally.inaccurate} />
                      <Stat label="Not reviewed" value={tally.unrated} />
                    </div>
                  )}
                  <Matrix matrix={matrix} onPick={onOpenRun}
                          selected={openRun ? [openRun.id] : []} />
                </Card>
              )}

              {tab === 'compare' && (
                <CompareTab
                  options={compareOptions}
                  a={compareA} b={compareB}
                  setA={setCompareA} setB={setCompareB}
                  onCompare={onCompare}
                  comparison={comparison}
                  busy={busy}
                />
              )}
            </>
          )}
        </main>
      </div>
    </div>
  )
}

function RunsTab({ runs, openRun, onOpenRun, onReview, onDelete, busy, runLabel }) {
  const [notesDraft, setNotesDraft] = useState('')

  useEffect(() => { setNotesDraft(openRun?.notes || '') }, [openRun?.id])

  if (runs.length === 0) {
    return (
      <Card title="Outputs">
        <Empty>Nothing recorded for this prompt yet.</Empty>
      </Card>
    )
  }

  return (
    <>
      <Card title="Recorded outputs" tight>
        <div className="run-list">
          {runs.map((run) => (
            <button
              key={run.id}
              className={`run-row${openRun?.id === run.id ? ' active' : ''}`}
              onClick={() => onOpenRun(run.id)}
            >
              <Verdict value={run.verdict} />
              <span className="run-main">{runLabel(run)}</span>
              <span className="run-meta">{run.output_words} words</span>
            </button>
          ))}
        </div>
      </Card>

      {openRun && (
        <Card
          title={`v${openRun.version} · ${openRun.model || 'unnamed model'}`}
          hint="Read it, then say what you concluded. That verdict is what the matrix shows."
          right={
            <button className="danger" onClick={() => onDelete(openRun.id)} disabled={busy}>
              Delete
            </button>
          }
        >
          <VerdictPicker
            value={openRun.verdict}
            disabled={busy}
            onChange={(verdict) => onReview(openRun.id, verdict, notesDraft)}
          />

          <label className="field" style={{ marginTop: 12 }}>Your notes</label>
          <input
            type="text"
            value={notesDraft}
            onChange={(event) => setNotesDraft(event.target.value)}
            onBlur={() => {
              if (notesDraft !== (openRun.notes || '')) {
                onReview(openRun.id, openRun.verdict, notesDraft)
              }
            }}
            placeholder="what was right, what was wrong"
          />

          <h3 className="section">Output</h3>
          <DiffText plain={openRun.output} />

          <details className="fold">
            <summary>The exact prompt that produced this</summary>
            <DiffText plain={openRun.prompt_text} />
          </details>
        </Card>
      )}
    </>
  )
}

function CompareTab({ options, a, b, setA, setB, onCompare, comparison, busy }) {
  if (options.length < 2) {
    return (
      <Card title="Compare">
        <Empty>Record at least two outputs for this prompt and they can be compared here.</Empty>
      </Card>
    )
  }

  return (
    <>
      <Card title="Compare two outputs" hint="Any two runs of this prompt — different versions, different models, or the same pairing twice.">
        <div className="row">
          <Field label="Left">
            <select value={a} onChange={(event) => setA(event.target.value)}>
              <option value="">choose a run</option>
              {options.map((option) => (
                <option key={option.id} value={option.id}>{option.label}</option>
              ))}
            </select>
          </Field>
          <Field label="Right">
            <select value={b} onChange={(event) => setB(event.target.value)}>
              <option value="">choose a run</option>
              {options.map((option) => (
                <option key={option.id} value={option.id}>{option.label}</option>
              ))}
            </select>
          </Field>
          <button className="primary" onClick={onCompare} disabled={busy || !a || !b || a === b}>
            Compare
          </button>
        </div>
      </Card>

      {comparison && (
        <Card
          title="Output differences"
          right={
            <div className="row">
              {comparison.same_prompt_version && <span className="chip">same prompt version</span>}
              {comparison.same_model && <span className="chip">same model</span>}
            </div>
          }
        >
          <DiffLegend diff={comparison.output_diff} />
          <div className="side-by-side">
            <div>
              <div className="col-head">
                <Verdict value={comparison.left.verdict} />
                <span>v{comparison.left.version} · {comparison.left.model || 'unnamed'}</span>
              </div>
              <DiffText spans={comparison.output_diff.left} />
            </div>
            <div>
              <div className="col-head">
                <Verdict value={comparison.right.verdict} />
                <span>v{comparison.right.version} · {comparison.right.model || 'unnamed'}</span>
              </div>
              <DiffText spans={comparison.output_diff.right} />
            </div>
          </div>

          {!comparison.same_prompt_version && (
            <details className="fold">
              <summary>These used different prompts — show that diff too</summary>
              <DiffLegend diff={comparison.prompt_diff} />
              <div className="side-by-side">
                <DiffText spans={comparison.prompt_diff.left} />
                <DiffText spans={comparison.prompt_diff.right} />
              </div>
            </details>
          )}
        </Card>
      )}
    </>
  )
}
