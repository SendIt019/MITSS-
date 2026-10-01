#!/usr/bin/env bash
# Run a model server command and stop it the moment its generation thread
# crashes. Used by start_model_server.sh; usable on its own:
#
#   scripts/watch_server.sh ~/models-env/bin/mlx_lm.server --model ... --port 8080
#
# Why: when mlx_lm.server's generation thread dies (for example
# `RuntimeError: [metal::malloc] Resource limit (499000) exceeded`, mlx-lm
# issue #1332), its HTTP side stays up and says nothing, so a client waits out
# its whole idle timeout. Stopping the server makes the connection drop at
# once, and the runner records a failure instead.
#
# The trigger is a traceback from the generation thread
# (`Exception in thread ... (_generate):`, fired on the exception line that
# ends it, so the whole traceback reaches the log) or any line containing
# `RuntimeError: [metal::malloc]`. Tracebacks from request handlers (a client
# hanging up, for instance) are not crashes and are passed through untouched.
#
# stdout and stderr pass through unchanged. The Mac is kept awake while the
# server runs (caffeinate -w). Ctrl-C or SIGTERM stops the server. Exits with
# the server's status, or 70 after a crash.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 SERVER_COMMAND [ARGS...]" >&2
  exit 64
fi

CRASH_EXIT=70
PARENT=$$
crashed=0
server=""

stop_server() {
  if [ -n "$server" ]; then kill -TERM "$server" 2>/dev/null || true; fi
}
on_crash() { crashed=1; stop_server; }
trap on_crash USR1
trap stop_server INT TERM

# Reads the server's stderr line by line, writes every line straight back to
# stderr, and signals the script once when the generation thread has died.
watch_stderr() {
  local line in_thread=0 fired=0
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "$line" >&2
    [ "$fired" = 1 ] && continue
    case "$line" in
      *"RuntimeError: [metal::malloc]"*) fired=1 ;;
      "Exception in thread "*"(_generate)"*) in_thread=1 ;;
      "Traceback "*|" "*) ;;                     # inside the traceback
      *) [ "$in_thread" = 1 ] && fired=1 ;;      # the exception line ends it
    esac
    if [ "$fired" = 1 ]; then
      echo "watch_server.sh: the model server's generation thread crashed; stopping the server so clients fail now instead of waiting out their timeout" >&2
      kill -USR1 "$PARENT" 2>/dev/null || true
    fi
  done
}

"$@" 2> >(watch_stderr) &
server=$!
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -w "$server" &
fi

status=0
while :; do
  # A trap interrupts wait; loop until the server has really gone.
  if wait "$server"; then status=0; else status=$?; fi
  kill -0 "$server" 2>/dev/null || break
done
wait 2>/dev/null || true    # let the stderr reader drain

if [ "$crashed" = 1 ]; then
  exit "$CRASH_EXIT"
fi
exit "$status"
