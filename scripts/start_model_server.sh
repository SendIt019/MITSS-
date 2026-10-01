#!/usr/bin/env bash
# Start mlx_lm.server with the standard flags, so a restart is one command.
#
#   scripts/start_model_server.sh                 # gemma-3-12b on 8080
#   scripts/start_model_server.sh qwen3.5-9b      # another starting model
#   scripts/start_model_server.sh qwen3.5-9b 8081 # another port
#
# The starting model barely matters: the harness asks for models by path and
# the server loads whichever one a request names. Ctrl-C stops the server.
# Runs under caffeinate so the Mac cannot sleep mid-batch (keep the lid open).
#
# Crash watchdog (scripts/watch_server.sh): if the server's generation thread
# dies - `RuntimeError: [metal::malloc] Resource limit (499000) exceeded` or
# any other traceback from `_generate` - the server is stopped at once, so the
# runner sees a dropped connection and records a failure instead of waiting
# out its idle timeout. Output passes through unchanged; exit 70 after a crash.
#
# Overridable with environment variables:
#   MITSS_MODELS_DIR        folder holding the model folders   (~/Desktop/models)
#   MITSS_MODELS_ENV        the venv that has mlx_lm installed  (~/models-env)
#   MITSS_SERVER_MAX_TOKENS the server's generation cap         (32768)
set -euo pipefail

MODELS_DIR="${MITSS_MODELS_DIR:-$HOME/Desktop/models}"
ENV_DIR="${MITSS_MODELS_ENV:-$HOME/models-env}"
MAX_TOKENS="${MITSS_SERVER_MAX_TOKENS:-32768}"
MODEL="${1:-gemma-3-12b}"
PORT="${2:-8080}"

SERVER="$ENV_DIR/bin/mlx_lm.server"
if [ ! -x "$SERVER" ]; then
  echo "mlx_lm.server not found at $SERVER (set MITSS_MODELS_ENV)" >&2
  exit 1
fi

if [ ! -f "$MODELS_DIR/$MODEL/config.json" ]; then
  echo "no model folder at $MODELS_DIR/$MODEL - available:" >&2
  ls "$MODELS_DIR" >&2
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT is already in use - stop that first (Ctrl-C in its tab):" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  exit 1
fi

echo "mlx_lm.server: model $MODEL, port $PORT, max-tokens $MAX_TOKENS  (Ctrl-C stops it)"
exec "$(dirname "$0")/watch_server.sh" "$SERVER" \
  --model "$MODELS_DIR/$MODEL" \
  --port "$PORT" \
  --max-tokens "$MAX_TOKENS"
