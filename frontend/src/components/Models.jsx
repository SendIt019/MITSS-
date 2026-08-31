import { useState } from 'react'
import { api } from '../api'
import { Card, Empty, Field } from './Panels'

// The team model registry: how a teammate hands their model to the operator.
//
// A registration is connection details only. The key field takes the NAME of
// an environment variable set on the machine running the backend — the backend
// reads it at call time and never stores or returns the value.

const BLANK = {
  name: '', owner: '', url: '', format: 'openai', model: '', key_env: '', notes: '',
}

export default function Models({ models, onChanged, busy, guard, say }) {
  const [form, setForm] = useState(BLANK)
  const [editing, setEditing] = useState('')   // model id being edited, '' = new

  const set = (key) => (event) =>
    setForm((prev) => ({ ...prev, [key]: event.target.value }))

  const openEdit = (entry) => {
    setEditing(entry.id)
    setForm({
      name: entry.name, owner: entry.owner, url: entry.url,
      format: entry.format, model: entry.model, key_env: entry.key_env,
      notes: entry.notes,
    })
  }

  const reset = () => { setEditing(''); setForm(BLANK) }

  const save = () =>
    guard(async () => {
      if (editing) {
        const { name, ...patch } = form
        await api.updateModel(editing, patch)
        say('Model updated')
      } else {
        await api.registerModel(form)
        say('Model registered — it now appears in Run on and in batch runs')
      }
      reset()
      await onChanged()
    })

  const remove = (entry) =>
    guard(async () => {
      await api.deleteModel(entry.id)
      if (editing === entry.id) reset()
      await onChanged()
      say('Registration removed — recorded runs keep its name')
    })

  return (
    <>
      <Card
        title="Registered models"
        hint="Each entry is a model someone on the team has sent in. With a URL the backend can call it directly — one at a time or all at once; without one it is paste-only and just keeps run labels consistent."
      >
        {models.length === 0 ? (
          <Empty>
            No models registered yet. Fill in the form below — or have a teammate
            do it from this page on your machine.
          </Empty>
        ) : (
          <div className="run-list">
            {models.map((entry) => (
              <div className="run-row model-row" key={entry.id}>
                <span className={`chip ${entry.callable ? 'ok' : 'wait'}`}>
                  <span className="dot" />
                  {entry.callable ? 'callable' : 'paste-only'}
                </span>
                <span className="run-main">
                  <strong>{entry.name}</strong>
                  {entry.owner && <span className="muted"> · {entry.owner}</span>}
                  {entry.url && <span className="muted mono"> · {entry.url}</span>}
                  {entry.key_env && (
                    <span className="muted"> · key from {entry.key_env}
                      {entry.key_set ? ' (set)' : ' (NOT SET)'}
                    </span>
                  )}
                </span>
                <span className="row" style={{ gap: 6 }}>
                  <button onClick={() => openEdit(entry)} disabled={busy}>Edit</button>
                  <button className="danger" onClick={() => remove(entry)} disabled={busy}>
                    Remove
                  </button>
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card
        title={editing ? `Edit ${form.name}` : 'Register a model'}
        hint={editing
          ? 'The name is fixed — recorded runs carry it. Everything else can change.'
          : 'Name and owner are enough to start; add the URL when the endpoint is up. Never put a key here — only the name of the environment variable that holds it.'}
        right={editing ? <button onClick={reset}>Cancel</button> : null}
      >
        <div className="row">
          <Field label="Name (labels every run)">
            <input type="text" value={form.name} onChange={set('name')}
                   placeholder="team-7b" disabled={!!editing} />
          </Field>
          <Field label="Owner">
            <input type="text" value={form.owner} onChange={set('owner')}
                   placeholder="who sent it" />
          </Field>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <Field label="Endpoint URL (blank = paste-only)">
            <input type="text" value={form.url} onChange={set('url')}
                   placeholder="http://127.0.0.1:8080/v1/chat/completions" />
          </Field>
          <div>
            <label className="field">Body shape</label>
            <select value={form.format} onChange={set('format')}>
              <option value="openai">openai (llama.cpp, vLLM, Ollama, LM Studio)</option>
              <option value="raw">raw ({'{"prompt": ...}'})</option>
            </select>
          </div>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <Field label="Model name sent in the request (optional)">
            <input type="text" value={form.model} onChange={set('model')}
                   placeholder="defaults to the name above" />
          </Field>
          <Field label="Key env var NAME (optional, never the key)">
            <input type="text" value={form.key_env} onChange={set('key_env')}
                   placeholder="TEAM_7B_KEY" />
          </Field>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <Field label="Notes">
            <input type="text" value={form.notes} onChange={set('notes')}
                   placeholder="quantised q4, runs on the lab box" />
          </Field>
          <button className="primary" onClick={save}
                  disabled={busy || !form.name.trim()}>
            {editing ? 'Save changes' : 'Register'}
          </button>
        </div>
      </Card>
    </>
  )
}
