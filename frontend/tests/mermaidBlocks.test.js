// Run with `npm test` (node's built-in runner; no extra dependencies).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { extractMermaidBlocks } from '../src/mermaidBlocks.js'

test('no mermaid means no blocks', () => {
  assert.deepEqual(extractMermaidBlocks(''), [])
  assert.deepEqual(extractMermaidBlocks(undefined), [])
  assert.deepEqual(extractMermaidBlocks('plain answer\n```python\nprint(1)\n```'), [])
})

test('the word mermaid outside a fence is not a block', () => {
  assert.deepEqual(extractMermaidBlocks('I would use mermaid here.\n```\nmermaid\n```'), [])
})

test('one block, content exact', () => {
  const text = 'Here:\n```mermaid\ngraph TD\n  A-->B\n```\nDone.'
  assert.deepEqual(extractMermaidBlocks(text), [{ code: 'graph TD\n  A-->B', unclosed: false }])
})

test('several blocks come back in order, other fences skipped', () => {
  const text = [
    '```mermaid', 'graph LR', 'A-->B', '```',
    '```js', 'const x = 1', '```',
    '~~~ Mermaid title', 'sequenceDiagram', 'A->>B: hi', '~~~',
  ].join('\n')
  assert.deepEqual(extractMermaidBlocks(text).map((b) => b.code), [
    'graph LR\nA-->B',
    'sequenceDiagram\nA->>B: hi',
  ])
})

test('CRLF line endings and up to three spaces of indent', () => {
  const text = '   ```mermaid\r\ngraph TD\r\nA-->B\r\n   ```\r\n'
  assert.deepEqual(extractMermaidBlocks(text), [{ code: 'graph TD\nA-->B', unclosed: false }])
})

test('four spaces of indent is not a fence', () => {
  assert.deepEqual(extractMermaidBlocks('    ```mermaid\n    graph TD\n    ```'), [])
})

test('a shorter or different fence does not close the block', () => {
  const text = '````mermaid\ngraph TD\n```\n~~~~\nA-->B\n````'
  assert.deepEqual(extractMermaidBlocks(text), [{ code: 'graph TD\n```\n~~~~\nA-->B', unclosed: false }])
})

test('mermaid fence quoted inside another code block is left alone', () => {
  const text = '````markdown\n```mermaid\ngraph TD\n```\n````'
  assert.deepEqual(extractMermaidBlocks(text), [])
})

test('an unclosed block runs to the end and is marked', () => {
  const text = 'cut off:\n```mermaid\ngraph TD\nA-->'
  assert.deepEqual(extractMermaidBlocks(text), [{ code: 'graph TD\nA-->', unclosed: true }])
})

test('mermaidjs or mermaid-ish info strings are not mermaid', () => {
  assert.deepEqual(extractMermaidBlocks('```mermaidjs\ngraph TD\n```'), [])
})

test('empty block is returned (and will fail to draw visibly)', () => {
  assert.deepEqual(extractMermaidBlocks('```mermaid\n```'), [{ code: '', unclosed: false }])
})
