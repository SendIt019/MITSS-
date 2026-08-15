import { useEffect, useState } from 'react'
import { api } from '../api'
import { Card, Dropzone, Empty, Field } from './Panels'
import {
  SALUTE_FIELDS, assembleSalute, emptyFields, hasAnyField, parseSalute,
} from '../salute'

// The input library: reusable material a prompt gets applied to.
//
// Inputs are structured as M-SALUTE reports: the form below assembles the
// seven fields into a canonical block that is stored as the input text and
// dropped into {input}. Editing one here does not rewrite past runs — each run
// froze the input text it was actually given.
//
// Uploaded or legacy free-text inputs that don't parse as M-SALUTE fall back to
// a plain textarea, so nothing is ever silently dropped.

export default function Inputs({ inputs, onChanged, busy, guard }) {
  const [selected, setSelected] = useState(null)
  const [name, setName] = useState('')
  const [fields, setFields] = useState(emptyFields())
  const [raw, setRaw] = useState('')     // set only for non-M-SALUTE inputs
  const [structured, setStructured] = useState(true)
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    if (!selected) return
    const still = inputs.find((i) => i.id === selected.id)
    if (!still) setSelected(null)
  }, [inputs, selected])

  const loadInto = (body) => {
    const parsed = parseSalute(body.text)
    setName(body.name)
    if (parsed) {
      setStructured(true)
      setFields(parsed)
      setRaw('')
    } else {
      setStructured(false)
      setRaw(body.text)
      setFields(emptyFields())
    }
  }

  const open = (id) =>
    guard(async () => {
      const body = await api.input(id)
      setSelected(body)
      setCreating(false)
      loadInto(body)
      return body
    })

  const startNew = () => {
    setSelected(null)
    setCreating(true)
    setStructured(true)
    setName('')
    setFields(emptyFields())
    setRaw('')
  }

  const setField = (key, value) => setFields((prev) => ({ ...prev, [key]: value }))

  // What actually gets stored: the assembled report, or the raw text for a
  // legacy input being edited in place.
  const composedText = structured ? assembleSalute(fields) : raw
  const canSave = structured ? hasAnyField(fields) : raw.trim().length > 0

  const save = () =>
    guard(async () => {
      if (creating) {
        const body = await api.createInput(name || 'Untitled input', composedText)
        setSelected(body)
        setCreating(false)
        loadInto(body)
      } else if (selected) {
        const body = await api.updateInput(selected.id, { name, text: composedText })
        setSelected(body)
        loadInto(body)
      }
      await onChanged()
    })

  const remove = () =>
    guard(async () => {
      await api.deleteInput(selected.id)
      setSelected(null)
      await onChanged()
    })

  const upload = (file) =>
    guard(async () => {
      const body = await api.uploadInput(file)
      await onChanged()
      setSelected(body)
      setCreating(false)
      loadInto(body)
      return body
    })

  const dirty = selected && (name !== selected.name || composedText !== selected.text)

  return (
    <>
      <Card
        title="Input library"
        hint="Material your prompts get applied to, captured as M-SALUTE reports. Reusable, so the same test case can be run against every version."
        right={<button onClick={startNew} disabled={busy}>New input</button>}
      >
        {inputs.length === 0 ? (
          <Empty>No inputs yet. Create one, or drop a .txt file below.</Empty>
        ) : (
          <div className="run-list">
            {inputs.map((entry) => (
              <button
                key={entry.id}
                className={`run-row${selected?.id === entry.id ? ' active' : ''}`}
                onClick={() => open(entry.id)}
              >
                <span className="mono muted">{entry.id}</span>
                <span className="run-main">{entry.name}</span>
                <span className="run-meta">{entry.words} words</span>
              </button>
            ))}
          </div>
        )}
        <div style={{ marginTop: 12 }}>
          <Dropzone onFile={upload} busy={busy} label="Drop a .txt input" />
        </div>
      </Card>

      {(selected || creating) && (
        <Card
          title={creating ? 'New M-SALUTE input' : selected.name}
          hint={
            creating
              ? 'Fill in the M-SALUTE fields. Mission objective carries the GFC intent for the mission file being produced.'
              : 'Editing this does not change past runs — each froze the text it was given.'
          }
          right={
            !creating && selected ? (
              <button className="danger" onClick={remove} disabled={busy}>Delete</button>
            ) : null
          }
        >
          <div className="row">
            <Field label="Name">
              <input
                type="text"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="e.g. Objective BRAVO recon"
              />
            </Field>
          </div>

          {structured ? (
            <div className="salute-form">
              {SALUTE_FIELDS.map((f) => (
                <div className="salute-field" key={f.key}>
                  <span className="salute-letter" aria-hidden="true">{f.letter}</span>
                  <div className="salute-body">
                    <label className="field">{f.label}</label>
                    <textarea
                      className="salute-input"
                      rows={f.rows}
                      value={fields[f.key]}
                      onChange={(event) => setField(f.key, event.target.value)}
                      spellCheck={false}
                    />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <>
              <label className="field" style={{ marginTop: 10 }}>
                Text (free-form — this input was not written as M-SALUTE)
              </label>
              <textarea
                className="output-input"
                value={raw}
                onChange={(event) => setRaw(event.target.value)}
                spellCheck={false}
              />
            </>
          )}

          <div className="row" style={{ marginTop: 12 }}>
            <button
              className="primary"
              onClick={save}
              disabled={busy || !canSave || (!creating && !dirty)}
            >
              {creating ? 'Create input' : 'Save changes'}
            </button>
            {!creating && !dirty && (
              <span className="hint" style={{ margin: 0 }}>No unsaved changes.</span>
            )}
            {structured && !canSave && (
              <span className="hint" style={{ margin: 0 }}>Fill in at least one field.</span>
            )}
          </div>
        </Card>
      )}
    </>
  )
}
