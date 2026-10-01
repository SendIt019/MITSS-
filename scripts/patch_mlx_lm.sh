#!/usr/bin/env bash
# Patch mlx-lm 0.31.3's ArraysCache.advance() so long thinking runs on
# hybrid linear-attention models (qwen3_5, qwen3_next) stop crashing.
#
#   scripts/patch_mlx_lm.sh          # apply (safe to run twice)
#   scripts/patch_mlx_lm.sh --undo   # restore the original cache.py
#
# The bug (mlx-lm issue #1332): advance() runs `self.left_padding -= N` (and
# `self.lengths -= N`) on every decode step without evaluating it, so each
# step leaves one more unevaluated graph node holding a Metal buffer. Around
# 10,500 tokens Qwen 3.8 27B passes Metal's 499000-buffer cap and the
# server's generation thread dies with
# `RuntimeError: [metal::malloc] Resource limit (499000) exceeded`.
# The fix reported in the issue evaluates the two arrays at the end of
# advance(). No released mlx-lm has it yet; 0.31.3 is the latest on PyPI.
#
# This edits a third-party package inside ~/models-env (DECISIONS.md,
# 2026-10-01). Upgrading or reinstalling mlx-lm replaces cache.py and drops
# the patch; this script then refuses to run on the new version until it is
# checked again. The original is kept as cache.py.orig-mitss.
#
# Overridable: MITSS_MODELS_ENV, the venv holding mlx_lm (~/models-env).
set -euo pipefail

ENV_DIR="${MITSS_MODELS_ENV:-$HOME/models-env}"
WANT_VERSION="0.31.3"
MARKER="# MITSS patch (mlx-lm issue #1332)"

UNDO=0
case "${1:-}" in
  "") ;;
  --undo) UNDO=1 ;;
  *) echo "usage: $0 [--undo]" >&2; exit 64 ;;
esac

PACKAGE=""
for candidate in "$ENV_DIR"/lib/python*/site-packages/mlx_lm; do
  [ -d "$candidate" ] && PACKAGE="$candidate" && break
done
if [ -z "$PACKAGE" ]; then
  echo "mlx_lm not found under $ENV_DIR/lib/python*/site-packages (set MITSS_MODELS_ENV)" >&2
  exit 1
fi
CACHE="$PACKAGE/models/cache.py"
BACKUP="$CACHE.orig-mitss"

if [ "$UNDO" = 1 ]; then
  if [ ! -f "$BACKUP" ]; then
    echo "nothing to undo: no backup at $BACKUP"
    exit 0
  fi
  cp -p "$BACKUP" "$CACHE"
  rm -f "$BACKUP"
  echo "restored $CACHE from its backup; mlx-lm is unpatched"
  exit 0
fi

# The version comes from the installed package's metadata, not an import.
VERSION=""
for meta in "$(dirname "$PACKAGE")"/mlx_lm-*.dist-info/METADATA; do
  [ -f "$meta" ] || continue
  VERSION="$(sed -n 's/^Version: *//p' "$meta" | head -1)"
  break
done
if [ "$VERSION" != "$WANT_VERSION" ]; then
  echo "refusing: mlx-lm is ${VERSION:-unknown} at $PACKAGE; this patch is for $WANT_VERSION only." >&2
  echo "A newer release may already fix issue #1332 - check before patching anything." >&2
  exit 1
fi

if grep -qF "$MARKER" "$CACHE"; then
  echo "already patched: $CACHE (undo with $0 --undo)"
  exit 0
fi

MADE_BACKUP=0
if [ ! -f "$BACKUP" ]; then
  cp -p "$CACHE" "$BACKUP"
  MADE_BACKUP=1
fi

# Exact-text replacement: the method must appear exactly as in 0.31.3, once.
if ! python3 - "$CACHE" "$MARKER" <<'PY'
import sys

path, marker = sys.argv[1], sys.argv[2]
old = (
    "    def advance(self, N):\n"
    "        if self.lengths is not None:\n"
    "            self.lengths -= N\n"
    "        if self.left_padding is not None:\n"
    "            self.left_padding -= N\n"
)
new = old + (
    f"        {marker}: evaluate now, so each decode step does not\n"
    "        # leave an unevaluated graph node (and its Metal buffer) behind.\n"
    "        if self.lengths is not None:\n"
    "            mx.eval(self.lengths)\n"
    "        if self.left_padding is not None:\n"
    "            mx.eval(self.left_padding)\n"
)
with open(path, encoding="utf-8") as handle:
    text = handle.read()
found = text.count(old)
if found != 1:
    sys.exit(f"refusing: ArraysCache.advance() in {path} does not match mlx-lm "
             f"0.31.3 (found the expected block {found} times); nothing changed")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(text.replace(old, new))
PY
then
  [ "$MADE_BACKUP" = 1 ] && rm -f "$BACKUP"
  exit 1
fi

echo "patched $CACHE (backup: $BACKUP). The change:"
diff -u "$BACKUP" "$CACHE" || true
echo "Restart mlx_lm.server for it to take effect. Undo: $0 --undo"
