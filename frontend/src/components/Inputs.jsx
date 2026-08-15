import { useEffect, useState } from 'react'
import { api } from '../api'
import { Card, Dropzone, Empty, Field } from './Panels'

// The input library: reusable material a prompt gets applied to.
//
// Inputs are editable, unlike prompt versions. That is safe because every run
// freezes the input text it actually used, so editing one here cannot rewrite
// what a past run was given.

export default function Inputs({ inputs, onChanged, busy, guard }) {
  const [selected, setSelected] = useState(null)
  const [name, setName] = useState('')
  const [text, setText] = useState('')
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    if (!selected) return
    const still = inputs.find((i) => i.id === selected.id)
    if (!still) setSelected(null)
  }, [inputs, selected])

  const open = (id) =>
    guard(async () => {
      const body = await api.input(id)
      setSelected(body)
      setName(body.name)
      setText(body.text)
      setCreating(false)
      return body
    })

  const startNew = () => {
    setSelected(null)
    setCreating(true)
    setName('')
    setText('')
  }

  const save = () =>
    guard(async () => {
      if (creating) {
        const body = await api.createInput(name || 'Untitled input', text)
        setSelected(body)
        setCreating(false)
      } else if (selected) {
        const body = await api.updateInput(selected.id, { name, text })
        setSelected(body)
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
      setName(body.name)
      setText(body.text)
      setCreating(false)
      return body
    })

  const dirty = selected && (name !== selected.name || text !== selected.text)

  return (
    <>
      <Card
        title="Input library"
        hint="Material your prompts get applied to. Reusable, so the same test case can be run against every version."
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
          title={creating ? 'New input' : selected.name}
          hint={creating ? null : 'Editing this does not change past runs — each froze the text it was given.'}
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
                placeholder="Passage A"
              />
            </Field>
          </div>
          <label className="field" style={{ marginTop: 10 }}>Text</label>
          <textarea
            className="output-input"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Paste the passage, document or record here."
            spellCheck={false}
          />
          <div className="row" style={{ marginTop: 10 }}>
            <button
              className="primary"
              onClick={save}
              disabled={busy || !text.trim() || (!creating && !dirty)}
            >
              {creating ? 'Create input' : 'Save changes'}
            </button>
            {!creating && !dirty && (
              <span className="hint" style={{ margin: 0 }}>No unsaved changes.</span>
            )}
          </div>
        </Card>
      )}
    </>
  )
}
