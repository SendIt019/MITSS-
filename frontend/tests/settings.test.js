// Run with `npm test` (node's built-in runner; no extra dependencies).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { describeUsage, formatFlops } from '../src/settings.js'

test('a new run shows first token, decode speed and estimated FLOPs', () => {
  const line = describeUsage({
    prompt_tokens: 3922, completion_tokens: 812, tokens_per_second: 6.1,
    time_to_first_token_ms: 4213, decode_tokens_per_second: 7.6,
    flops_estimate: 1.3e15, finish_reason: 'stop',
  })
  assert.equal(line, '3922 in / 812 out tokens  ·  6.1 tok/s  ·  first token 4.2 s'
    + '  ·  decode 7.6 tok/s  ·  ~1.3 PFLOPs  ·  stopped: stop')
})

test('an old run without the new keys reads as before', () => {
  assert.equal(describeUsage({ prompt_tokens: 10, completion_tokens: 5, tokens_per_second: 2.5 }),
    '10 in / 5 out tokens  ·  2.5 tok/s')
})

test('null metrics are left out rather than shown as zero', () => {
  assert.equal(describeUsage({
    completion_tokens: 1, time_to_first_token_ms: null,
    decode_tokens_per_second: null, flops_estimate: null,
  }), '? in / 1 out tokens')
})

test('FLOPs use readable units with a tilde', () => {
  assert.equal(formatFlops(2.5e9), '~2.5 GFLOPs')
  assert.equal(formatFlops(118.8e12), '~118.8 TFLOPs')
  assert.equal(formatFlops(1.3e15), '~1.3 PFLOPs')
  assert.equal(formatFlops(500), '~500 FLOPs')
})
