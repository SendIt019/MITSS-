# Sandbox setup

How to point MITSS at a model inside the sandbox and confirm the wiring works.
The harness never depends on a model being reachable: with the default `manual`
provider you copy the rendered prompt, run it yourself, and paste the output
back. Everything below is only needed to have MITSS call a model for you.

All model configuration lives in `backend/.env` (loaded automatically by
`./dev.sh`). Nothing is hardcoded, and no key is ever logged, written into a run
folder, or returned by an API response — `GET /api/llm` reports only whether a
key is set.

## Environment variables

Set these in `backend/.env`, then restart `./dev.sh`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `MITSS_LLM_PROVIDER` | yes | `manual` (default, paste path) or `http` (call a model) |
| `MITSS_LLM_URL` | for `http` | Model endpoint, e.g. `http://127.0.0.1:8080/v1/chat/completions` |
| `MITSS_LLM_FORMAT` | no | `openai` (default; `/chat/completions` body) or `raw` (`{"prompt": ...}`) |
| `MITSS_LLM_MODEL` | no | Default model name sent in the request body |
| `MITSS_LLM_MODELS` | no | Comma-separated list shown in the UI's **Run on ▾** dropdown. Falls back to `MITSS_LLM_MODEL` |
| `MITSS_LLM_API_KEY` | no | Sent as `Authorization: Bearer <key>`. Presence only is ever exposed |
| `MITSS_LLM_TIMEOUT` | no | Request timeout in seconds (default `120`) |

Example `backend/.env`:

```bash
MITSS_LLM_PROVIDER=http
MITSS_LLM_URL=http://127.0.0.1:8080/v1/chat/completions
MITSS_LLM_FORMAT=openai
MITSS_LLM_MODELS=model-a, model-b, model-c
MITSS_LLM_API_KEY=changeme          # optional
```

The `openai` format is what llama.cpp, vLLM, Ollama, and LM Studio all speak, so
an OpenAI-compatible endpoint needs no code. A non-standard internal API needs a
small custom provider (subclass `LLMProvider` in `backend/mitss/llm.py` and call
`register_provider(...)`) — see the module docstring.

## How the UI behaves

- Provider `manual` (or `http` with no models): the **Run on ▾** dropdown is
  hidden; use the copy-prompt / paste-output path. The status chip shows the
  provider name.
- Provider `http` with models configured: the Prompt tab's "Run it" card shows a
  model dropdown next to a **Run** button. Pick a model, click **Run**, and the
  rendered prompt is sent and the output recorded — tagged with that model and
  `source: provider`.

## Verification

### 1. Confirm the harness reads your config

```bash
curl -s http://127.0.0.1:8000/api/llm | python3 -m json.tool
```

Expect `"provider": "http"`, `"available": true`, your `"models": [...]`, and
`"api_key_set": true` when a key is set (the value is never shown).

### 2. Confirm a real call end to end (no real model needed)

This starts a stand-in OpenAI-shaped endpoint, points a throwaway backend at it,
and checks that the *selected* model is what gets sent and recorded. It uses a
temporary data root (`/tmp/mitss_e2e`) so nothing real is touched.

```bash
cd backend

# stand-in "model" that echoes back which model was requested
python3 - <<'PY' &
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length', 0))
        body = json.loads(self.rfile.read(n) or b'{}')
        reply = f"[echo from {body.get('model')}] " + body.get('messages', [{}])[-1].get('content', '')[:40]
        out = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
        self.send_response(200); self.send_header('content-type', 'application/json'); self.end_headers()
        self.wfile.write(out)
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', 9099), H).serve_forever()
PY
SRV=$!
sleep 1

MITSS_ROOT=/tmp/mitss_e2e \
MITSS_LLM_PROVIDER=http \
MITSS_LLM_URL=http://127.0.0.1:9099 \
MITSS_LLM_MODELS="team-7b, team-70b" \
python3 -c "
from app import service
p = service.create_prompt('e2e', 'Make a report from:\n{input}', 'test')
run = service.generate_run(p['id'], None, 'team-70b', '')
print('recorded model:', run['model'])
print('source        :', run['source'])
print('output        :', run['output'])
"

kill $SRV 2>/dev/null
rm -rf /tmp/mitss_e2e
```

Expected output:

```
recorded model: team-70b
source        : provider
output        : [echo from team-70b] Make a report from:
```

`recorded model: team-70b` and `source: provider` confirm the dropdown choice
flows all the way through to the request body and the recorded run.

### 3. Confirm in the running app

With `http` and `MITSS_LLM_MODELS` set in `backend/.env`, restart `./dev.sh`,
open the interface, create or open a prompt, pick a model in **Run on ▾**, and
click **Run**. The output is recorded under the **Outputs** tab tagged with that
model.
