"""The two shell scripts for the thinking fix, run against fakes only.

- scripts/patch_mlx_lm.sh against a fake ~/models-env holding a copy of the
  0.31.3 ArraysCache.advance() block, never the real install.
- scripts/watch_server.sh wrapping a fake server that prints a crash
  traceback, never mlx_lm.server.

Skipped where bash is missing. No network, no models.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATCH = os.path.join(REPO, "scripts", "patch_mlx_lm.sh")
WATCH = os.path.join(REPO, "scripts", "watch_server.sh")

# ArraysCache.advance() as mlx-lm 0.31.3 ships it, with a neighbour after it.
CACHE_PY = textwrap.dedent('''\
    import mlx.core as mx


    class ArraysCache:
        def advance(self, N):
            if self.lengths is not None:
                self.lengths -= N
            if self.left_padding is not None:
                self.left_padding -= N

        def make_mask(self, N: int):
            return None
    ''')


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class PatchScript(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = tmp.name
        site = os.path.join(self.env, "lib", "python3.14", "site-packages")
        os.makedirs(os.path.join(site, "mlx_lm", "models"))
        self.cache = os.path.join(site, "mlx_lm", "models", "cache.py")
        with open(self.cache, "w", encoding="utf-8") as handle:
            handle.write(CACHE_PY)
        self.dist = os.path.join(site, "mlx_lm-0.31.3.dist-info")
        os.makedirs(self.dist)
        self.version("0.31.3")

    def version(self, number):
        with open(os.path.join(self.dist, "METADATA"), "w", encoding="utf-8") as handle:
            handle.write(f"Metadata-Version: 2.1\nName: mlx-lm\nVersion: {number}\n")

    def run_patch(self, *args):
        env = dict(os.environ, MITSS_MODELS_ENV=self.env)
        return subprocess.run(["bash", PATCH, *args], capture_output=True, text=True, check=False,
                              env=env, timeout=60)

    def text(self):
        with open(self.cache, encoding="utf-8") as handle:
            return handle.read()

    def test_patch_adds_the_evals_backs_up_and_shows_the_change(self):
        result = self.run_patch()
        self.assertEqual(result.returncode, 0, result.stderr)
        patched = self.text()
        self.assertIn("            mx.eval(self.left_padding)\n", patched)
        self.assertIn("            mx.eval(self.lengths)\n", patched)
        # The evals come after the subtractions, still inside advance().
        self.assertLess(patched.index("self.left_padding -= N"),
                        patched.index("mx.eval(self.left_padding)"))
        self.assertLess(patched.index("mx.eval(self.left_padding)"),
                        patched.index("def make_mask"))
        compile(patched, self.cache, "exec")
        with open(self.cache + ".orig-mitss", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), CACHE_PY)
        self.assertIn("+            mx.eval(self.left_padding)", result.stdout)

    def test_running_twice_changes_nothing_more(self):
        self.run_patch()
        once = self.text()
        result = self.run_patch()
        self.assertEqual(result.returncode, 0)
        self.assertIn("already patched", result.stdout)
        self.assertEqual(self.text(), once)

    def test_undo_restores_the_original(self):
        self.run_patch()
        result = self.run_patch("--undo")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.text(), CACHE_PY)
        self.assertFalse(os.path.exists(self.cache + ".orig-mitss"))
        self.assertIn("nothing to undo", self.run_patch("--undo").stdout)

    def test_another_version_is_refused_untouched(self):
        self.version("0.32.0")
        result = self.run_patch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing: mlx-lm is 0.32.0", result.stderr)
        self.assertEqual(self.text(), CACHE_PY)
        self.assertFalse(os.path.exists(self.cache + ".orig-mitss"))

    def test_an_unexpected_advance_is_refused_untouched(self):
        changed = CACHE_PY.replace("self.left_padding -= N", "self.left_padding = self.left_padding - N")
        with open(self.cache, "w", encoding="utf-8") as handle:
            handle.write(changed)
        result = self.run_patch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assertEqual(self.text(), changed)
        self.assertFalse(os.path.exists(self.cache + ".orig-mitss"))


FAKE_SERVER = textwrap.dedent('''\
    import sys, time
    mode = sys.argv[1]
    print("server up", flush=True)
    time.sleep(0.2)
    if mode == "exit":
        sys.exit(3)
    thread = "Thread-1 (_generate)" if mode != "handler" else "Thread-7 (process_request_thread)"
    error = {"malloc": "RuntimeError: [metal::malloc] Resource limit (499000) exceeded.",
             "other": "ValueError: something else broke",
             "handler": "BrokenPipeError: [Errno 32] Broken pipe"}[mode]
    sys.stderr.write(f"Exception in thread {thread}:\\nTraceback (most recent call last):\\n"
                     f"  File \\"generate.py\\", line 1369, in _step\\n    step()\\n{error}\\n")
    sys.stderr.flush()
    time.sleep(30)
    ''')


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class Watchdog(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.server = os.path.join(tmp.name, "fake_server.py")
        with open(self.server, "w", encoding="utf-8") as handle:
            handle.write(FAKE_SERVER)

    def watch(self, mode, stop_after=None):
        process = subprocess.Popen(["bash", WATCH, sys.executable, self.server, mode],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        try:
            if stop_after is not None:
                time.sleep(stop_after)
                still_running = process.poll() is None
                process.terminate()
                out, err = process.communicate(timeout=20)
                return still_running, out, err
            out, err = process.communicate(timeout=20)
            return process.returncode, out, err, time.monotonic() - started
        finally:
            if process.poll() is None:
                process.kill()

    def test_a_metal_malloc_crash_stops_the_server_at_once(self):
        code, out, err, took = self.watch("malloc")
        self.assertEqual(code, 70)
        self.assertLess(took, 10)                    # not the fake's 30 s sleep
        self.assertEqual(out, "server up\n")         # stdout passes through
        # The whole traceback reaches the log before the one clear line.
        self.assertIn("RuntimeError: [metal::malloc] Resource limit (499000) exceeded.\n"
                      "watch_server.sh: the model server's generation thread crashed",
                      err)
        self.assertEqual(err.count("watch_server.sh:"), 1)

    def test_any_generation_thread_traceback_counts(self):
        code, _, err, took = self.watch("other")
        self.assertEqual(code, 70)
        self.assertLess(took, 10)
        self.assertIn("ValueError: something else broke\nwatch_server.sh:", err)

    def test_a_request_handler_traceback_leaves_the_server_running(self):
        still_running, _, err = self.watch("handler", stop_after=2.0)
        self.assertTrue(still_running)
        self.assertIn("BrokenPipeError", err)
        self.assertNotIn("watch_server.sh:", err)

    def test_a_normal_exit_keeps_its_status(self):
        code, out, err, _ = self.watch("exit")
        self.assertEqual((code, out, err), (3, "server up\n", ""))


if __name__ == "__main__":
    unittest.main()
