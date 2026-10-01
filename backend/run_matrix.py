"""Run one prompt across versions x inputs x models, without anyone watching.

Built in steps (docs/runner/RUNNER_SPEC.md). Start it through the wrapper,
which loads backend/.env the way dev.sh does. This file never opens .env:

    scripts/run_matrix.sh check  [--models ID[,ID...]]
    scripts/run_matrix.sh plan   MATRIX.json [--models ID[,ID...]]
    scripts/run_matrix.sh run    MATRIX.json [--models ID[,ID...]] [--yes]
    scripts/run_matrix.sh resume MATRIX_RUN_ID [--allow-changed-inputs]
    scripts/run_matrix.sh status [MATRIX_RUN_ID]

Exit codes: 0 all good - 1 finished with failed or skipped cells - 2 stopped
early - 3 validation or preflight error.

Standard library only, and it must run on Python 3.9 (CI runs the core there).
Runs are recorded only through app.service. The runner's own files (the
manifest, results.jsonl and summary.txt, all append-only) live in
data/matrices/<matrix_run_id>/; nothing else in data/ is written here.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import math
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from app import service
from mitss.llm import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_PREFLIGHT_TIMEOUT,
    DEFAULT_TIMEOUT,
    LLMModelUnavailable,
    check_model_folder,
    describe_settings,
    is_local_url,
    models_dir,
    resolve_model_path,
)
from pipeline import NotFound, Store

# The store's own id alphabet, so a name that passes here is safe as a
# directory name for the same reason a run id is.
from pipeline.store import _SAFE_ID, now, stamp, text_sha256
from pipeline.transcript import format_flops

EXIT_OK = 0
EXIT_FAILED_CELLS = 1
EXIT_STOPPED = 2
EXIT_INVALID = 3

# The models `check` looks at when not told otherwise (DECISIONS.md,
# 2026-09-29 "Lineup").
LINEUP = ("llama-3-1-8b", "qwen3-8-27b", "gemma-4-26b-a4b")

MAX_REPEATS = 10
MATRIX_KEYS = ("name", "prompt_id", "versions", "inputs", "models", "repeats")
# A matrix run id is "<YYYYMMDD-HHMMSS>-<name>", plus "-N" when two start in
# the same second, and must fit the store's 128-character id limit.
MAX_NAME = 128 - len("20260929-141500-") - len("-999")

MIB = 1024 ** 2
GIB = 1024 ** 3
HEADROOM = 2 * GIB             # KV cache and activations on top of the weights
RAISED_LIMIT_MB = 20480        # what the lineup decision asks Jake to set
SYSTEM_RESERVE = 4 * GIB       # never suggest a limit leaving macOS less
LOW_DISK = 1 * GIB
ESTIMATE_SAMPLE = 10           # recent runs used for a model's time estimate

# The only environment values `check` prints. Everything else stays unseen.
ENV_SHOWN = ("MITSS_ROOT", "MITSS_LLM_TIMEOUT", "MITSS_LLM_PREFLIGHT_TIMEOUT",
             "MITSS_MAX_TOKENS", "MITSS_MODELS_DIR")

COPY_ROOT = os.path.join("~", "Desktop", "AI Outputs", "MITSS Runs")
MODELS_ENV = os.environ.get("MITSS_MODELS_ENV", "").strip() or os.path.join("~", "models-env")

# Read through the mlx install that serves the models; mlx 0.32 has
# mx.device_info(), older releases mx.metal.device_info().
METAL_PROBE = ("import mlx.core as mx; "
               "f = getattr(mx, 'device_info', None) or mx.metal.device_info; "
               "print(f()['max_recommended_working_set_size'])")


class MatrixError(Exception):
    """A matrix file or plan that cannot run, with every reason found."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# --------------------------------------------------------------------------
# reading the store
# --------------------------------------------------------------------------

class ReadOnlyStore(Store):
    """The store, for reading only: it never creates a folder.

    `Store.__init__` calls `ensure()`, which makes the data folders. `check`
    and `plan` must leave a data directory exactly as they found it, missing
    or not. Runs are recorded in Step 2 through app.service, never through this.
    """

    def ensure(self) -> None:
        pass


def read_store(root: str | None = None) -> ReadOnlyStore:
    # The same data root service.store() resolves, so the runner and the web
    # app always read the same place.
    return ReadOnlyStore(root or os.environ.get("MITSS_ROOT", service.BACKEND_ROOT))


def registration(shelf: Store, model_id: str) -> dict[str, Any]:
    """A registration's own summary.

    Deliberately not service.get_model: that looks up the key variable to
    report `key_set`, and the runner never touches a key variable, not even
    to see whether it is set. Raises NotFound.
    """
    return shelf.get_model(model_id).summary()


# --------------------------------------------------------------------------
# matrix file
# --------------------------------------------------------------------------

@dataclass
class Matrix:
    name: str
    prompt_id: str
    versions: list[int]
    inputs: list[str]
    models: list[str]
    repeats: int = 1


def _is_safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_ID.fullmatch(value))


def _id_list(raw: dict[str, Any], key: str, problems: list[str]) -> list[str]:
    values = raw.get(key)
    if not isinstance(values, list) or not values:
        problems.append(f"{key} must be a non-empty list of ids")
        return []
    for value in values:
        if not _is_safe_id(value):
            problems.append(f"{key}: {value!r} is not a valid id")
    if len(set(map(str, values))) != len(values):
        problems.append(f"{key} lists the same id twice")
    return [v for v in values if _is_safe_id(v)]


def parse_matrix(raw: Any) -> Matrix:
    """Check a matrix file's shape. The store is not consulted here."""
    if not isinstance(raw, dict):
        raise MatrixError(["the matrix file must hold a JSON object"])
    problems: list[str] = []
    unknown = sorted(set(raw) - set(MATRIX_KEYS))
    if unknown:
        problems.append(f"unknown key(s): {', '.join(unknown)}; "
                        f"allowed: {', '.join(MATRIX_KEYS)}")

    name = raw.get("name")
    if not _is_safe_id(name):
        problems.append("name must be lowercase letters, digits and hyphens, "
                        f"starting with a letter or digit, got {name!r}")
    elif len(name) > MAX_NAME:
        problems.append(f"name is too long ({len(name)} characters, limit {MAX_NAME})")

    prompt_id = raw.get("prompt_id")
    if not _is_safe_id(prompt_id):
        problems.append(f"prompt_id: {prompt_id!r} is not a valid id")

    versions = raw.get("versions")
    if not isinstance(versions, list) or not versions:
        problems.append("versions must be a non-empty list of version numbers")
        versions = []
    for version in versions:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            problems.append(f"versions: {version!r} is not a version number")
    if len(set(map(repr, versions))) != len(versions):
        problems.append("versions lists the same version twice")

    inputs = _id_list(raw, "inputs", problems)
    models = _id_list(raw, "models", problems)

    repeats = raw.get("repeats", 1)
    if isinstance(repeats, bool) or not isinstance(repeats, int) \
            or not 1 <= repeats <= MAX_REPEATS:
        problems.append(f"repeats must be a whole number from 1 to {MAX_REPEATS}, "
                        f"got {repeats!r}")

    if problems:
        raise MatrixError(problems)
    return Matrix(name, prompt_id, list(versions), inputs, models, repeats)


def load_matrix(path: str) -> Matrix:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise MatrixError([f"cannot read {path}: {exc.strerror or exc}"]) from None
    except ValueError as exc:
        raise MatrixError([f"{path} is not valid JSON: {exc}"]) from None
    return parse_matrix(raw)


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

@dataclass
class Cell:
    """One call: a model, a version, an input, and which repeat it is."""

    index: int
    model_id: str
    model: str          # the registration's name, which labels the run
    version: int
    input_id: str
    repeat: int

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "model_id": self.model_id,
                "model": self.model, "version": self.version,
                "input_id": self.input_id, "repeat": self.repeat}


@dataclass
class Plan:
    matrix: Matrix
    models: list[dict[str, Any]]          # registrations, in run order
    inputs: dict[str, dict[str, str]]     # id -> name and sha256 at plan time
    cells: list[Cell] = field(default_factory=list)


def _selected_models(matrix: Matrix, only: Sequence[str] | None,
                     problems: list[str]) -> list[str]:
    if not only:
        return list(matrix.models)
    for model_id in only:
        if model_id not in matrix.models:
            problems.append(f"--models: '{model_id}' is not in this matrix "
                            f"({', '.join(matrix.models)})")
    # The matrix file's order wins: it is the order the models load in.
    return [m for m in matrix.models if m in only]


def build_plan(matrix: Matrix, only: Sequence[str] | None = None,
               root: str | None = None) -> Plan:
    """Check the matrix against the store and lay out every cell.

    Every problem is collected before refusing, so one pass fixes the file.
    A quarantined or paste-only model is refused here rather than skipped
    later: a matrix that silently lost a model would compare less than it says.
    """
    problems: list[str] = []
    model_ids = _selected_models(matrix, only, problems)
    shelf = read_store(root)

    try:
        prompt = shelf.get_prompt(matrix.prompt_id)
        for version in matrix.versions:
            if prompt.version(version) is None:
                problems.append(f"prompt '{matrix.prompt_id}' has no version {version}")
    except NotFound as exc:
        problems.append(f"prompt: {exc}")

    inputs: dict[str, dict[str, str]] = {}
    for input_id in matrix.inputs:
        try:
            found = shelf.get_input(input_id)
        except NotFound as exc:
            problems.append(f"input: {exc}")
            continue
        # The same function the store fingerprints runs with, so a plan-time
        # hash compares directly with a run's input_sha256.
        inputs[input_id] = {"name": found.name, "sha256": text_sha256(found.text)}

    models: list[dict[str, Any]] = []
    for model_id in model_ids:
        try:
            entry = registration(shelf, model_id)
        except NotFound as exc:
            problems.append(f"model: {exc}")
            continue
        refusal = refuse_model(entry)
        if refusal:
            problems.append(f"model '{model_id}' {refusal}")
        else:
            models.append(entry)

    if problems:
        raise MatrixError(problems)

    plan = Plan(matrix, models, inputs)
    # Model-major: every cell for one model before the next one loads.
    for entry in models:
        for version in matrix.versions:
            for input_id in matrix.inputs:
                for repeat in range(1, matrix.repeats + 1):
                    plan.cells.append(Cell(len(plan.cells) + 1, entry["id"],
                                           entry["name"], version, input_id,
                                           repeat))
    return plan


def refuse_model(entry: dict[str, Any]) -> str | None:
    """Why a registration cannot be in a matrix, or None if it can."""
    if entry["quarantine"]:
        return f"is quarantined: {entry['quarantine']}"
    if not entry["callable"]:
        return "is paste-only (no url)"
    if not is_local_url(entry["url"]):
        # Local only (DECISIONS.md 2026-09-29): the runner has no network
        # beyond this machine's model server.
        return (f"is not on this machine ({entry['url']}); the runner only "
                "calls loopback endpoints (127.0.0.1, localhost, ::1)")
    if url_port(entry["url"]) is None:
        return f"has a url with an invalid port ({entry['url']})"
    return None


def url_port(url: str) -> int | None:
    """The port a url names (or its scheme's default), None if it is invalid."""
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:      # not a number, or outside 0-65535
        return None


def served_model(entry: dict[str, Any]) -> str:
    """What the request body names: the model field, else the name, as the
    service's provider does (`model=entry.model or entry.name`)."""
    return entry.get("model") or entry["name"]


def _max_tokens(entry: dict[str, Any]) -> int:
    configured = (entry.get("settings") or {}).get("max_tokens")
    if isinstance(configured, int):
        return configured
    try:
        return int(os.environ.get("MITSS_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    except ValueError:
        return DEFAULT_MAX_TOKENS


def estimate_cell_seconds(entry: dict[str, Any],
                          root: str | None = None) -> dict[str, Any] | None:
    """Seconds per cell from this model's recent runs, or None without data.

    Only runs where the server reported a real token count carry
    tokens_per_second, so the estimate is never built on a guessed rate.
    Output length is the median of those runs, capped at the model's
    max_tokens. Model loading is not included.
    """
    samples = []
    for run in read_store(root).list_runs(model=entry["name"]):   # newest first
        usage = run.usage or {}
        rate = usage.get("tokens_per_second")
        produced = usage.get("completion_tokens")
        if isinstance(rate, (int, float)) and rate > 0 and isinstance(produced, int):
            samples.append((rate, produced))
        if len(samples) == ESTIMATE_SAMPLE:
            break
    if not samples:
        return None
    rate = statistics.median(r for r, _ in samples)
    tokens = min(statistics.median(p for _, p in samples), _max_tokens(entry))
    return {"seconds": tokens / rate, "rate": rate, "runs": len(samples)}


def format_duration(seconds: float) -> str:
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"~{minutes}m"
    return f"~{minutes // 60}h{minutes % 60:02d}m"


def format_plan(plan: Plan, estimates: dict[str, dict[str, Any] | None]) -> str:
    matrix = plan.matrix
    lines = [
        (f"Matrix {matrix.name}: prompt {matrix.prompt_id}, "
         f"versions {', '.join(map(str, matrix.versions))}, "
         f"inputs {', '.join(matrix.inputs)}, repeats {matrix.repeats}"),
        "",
    ]
    total_seconds = 0.0
    estimated_all = True
    width = max(len(e["name"]) for e in plan.models)
    for position, entry in enumerate(plan.models, start=1):
        count = sum(1 for c in plan.cells if c.model_id == entry["id"])
        guess = estimates.get(entry["id"])
        if guess:
            seconds = guess["seconds"] * count
            total_seconds += seconds
            timing = (f"{format_duration(seconds)}  ({guess['rate']:g} tok/s "
                      f"from {guess['runs']} recent run(s), loading not included)")
        else:
            estimated_all = False
            timing = "no estimate (no recent runs with a token rate)"
        settings = describe_settings(entry.get("settings")) or "harness defaults"
        lines.append(f"  {position}. {entry['name']:<{width}}  {count} cells  {timing}")
        lines.append(f"     id {entry['id']} - {settings}")
    lines.append("")
    total = f"Total: {len(plan.cells)} cells"
    if total_seconds:
        total += f", {format_duration(total_seconds)}"
        if not estimated_all:
            total += " for the models with an estimate"
    lines.append(total)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# system facts: commands are argument lists, never a shell string
# --------------------------------------------------------------------------

Runner = Callable[[list[str]], Optional[str]]


def run_command(command: list[str]) -> str | None:
    """stdout of a command that succeeded, else None. No shell, ever."""
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def sysctl_command(name: str) -> list[str]:
    return ["sysctl", "-n", name]


def metal_command() -> list[str]:
    python = os.path.join(os.path.expanduser(MODELS_ENV), "bin", "python")
    return [python, "-c", METAL_PROBE]


def notification_command(title: str, body: str, sound: str = "Glass") -> list[str]:
    """osascript with the text passed as arguments, never built into the script."""
    return ["osascript",
            "-e", "on run argv",
            "-e", ("display notification (item 2 of argv) with title "
                   f"(item 1 of argv) sound name \"{sound}\""),
            "-e", "end run",
            title, body]


def _as_int(text: str | None) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


@dataclass
class Memory:
    limit: int | None    # bytes the GPU may wire, or None if unknown
    source: str
    ram: int | None


def read_memory(run: Runner = run_command) -> Memory:
    """The GPU memory limit, read without sudo (spec §5.5)."""
    ram = _as_int(run(sysctl_command("hw.memsize")))
    wired_mb = _as_int(run(sysctl_command("iogpu.wired_limit_mb")))
    if wired_mb:
        return Memory(wired_mb * MIB, "iogpu.wired_limit_mb", ram)
    metal = _as_int(run(metal_command()))
    if metal:
        return Memory(metal, "Metal recommended working set", ram)
    if ram and ram <= 36 * GIB:
        return Memory(ram * 2 // 3, "two-thirds of RAM (limit not readable)", ram)
    if ram:
        return Memory(ram * 3 // 4, "three-quarters of RAM (limit not readable)", ram)
    return Memory(None, "not readable", None)


def memory_warning(weights: int, limit: int | None,
                   ram: int | None = None) -> str | None:
    """What to tell Jake when a model may not fit, else None."""
    if limit is None or weights + HEADROOM <= limit:
        return None
    over = (f"{gib(weights)} of weights + {gib(HEADROOM)} headroom is over the "
            f"{gib(limit)} limit.")
    needed_mb = math.ceil((weights + HEADROOM) / MIB / 1024) * 1024
    raise_to = max(RAISED_LIMIT_MB, needed_mb)
    if ram and raise_to * MIB > ram - SYSTEM_RESERVE:
        # Wiring nearly all of RAM starves macOS; never suggest it.
        return (f"{over} Raising the limit far enough would leave macOS less "
                f"than {gib(SYSTEM_RESERVE)} of {gib(ram)}; this model does not "
                "fit on this Mac.")
    return (f"{over} Run this yourself first (it resets on restart): "
            f"sudo sysctl iogpu.wired_limit_mb={raise_to}")


def gib(size: float) -> str:
    return f"{size / GIB:.1f} GiB"


def weights_bytes(folder: str) -> int:
    return sum(os.path.getsize(p) for p in glob.glob(os.path.join(folder, "*.safetensors")))


def quantization(config: dict[str, Any]) -> str:
    """Top-level bits, and "mixed" when some layers override them."""
    quant = config.get("quantization") or config.get("quantization_config")
    if not isinstance(quant, dict) or "bits" not in quant:
        return "not quantized (or not stated in config.json)"
    bits = quant["bits"]
    others = sorted({v["bits"] for v in quant.values()
                     if isinstance(v, dict) and v.get("bits") not in (None, bits)})
    if others:
        return f"{bits}-bit, mixed ({'/'.join(map(str, others))}-bit on some layers)"
    return f"{bits}-bit"


def server_listening(url: str, timeout: float = 2.0) -> bool | None:
    """Whether something accepts connections at a local endpoint.

    A TCP connect only: no request is sent and no model is touched. A remote
    endpoint is never probed (None); check refuses those registrations first.
    """
    if not is_local_url(url):
        return None
    port = url_port(url)
    if port is None:
        return False
    try:
        with socket.create_connection((urllib.parse.urlsplit(url).hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def free_bytes(path: str) -> int | None:
    """Free space on the volume holding `path`, or its nearest existing parent."""
    path = os.path.abspath(os.path.expanduser(path))
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return shutil.disk_usage(path).free


def environment_lines(env: dict[str, str] | None = None) -> list[str]:
    """The five variables `check` may show, and nothing else from the environment."""
    env = os.environ if env is None else env
    defaults = {
        "MITSS_ROOT": service.BACKEND_ROOT,
        "MITSS_LLM_TIMEOUT": f"{DEFAULT_TIMEOUT:g}",
        "MITSS_LLM_PREFLIGHT_TIMEOUT": f"{DEFAULT_PREFLIGHT_TIMEOUT:g}",
        "MITSS_MAX_TOKENS": str(DEFAULT_MAX_TOKENS),
        "MITSS_MODELS_DIR": models_dir(),
    }
    lines = []
    for name in ENV_SHOWN:
        value = env.get(name, "").strip()
        shown = value if value else f"{defaults[name]} (not set; default)"
        lines.append(f"  {name} = {shown}")
    return lines


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

class Report:
    """Lines for the console, plus counts of problems and warnings."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.problems = 0
        self.warnings = 0

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    def problem(self, text: str) -> None:
        self.problems += 1
        self.lines.append(f"  PROBLEM: {text}")

    def warn(self, text: str) -> None:
        self.warnings += 1
        self.lines.append(f"  WARNING: {text}")


def _check_model(report: Report, model_id: str, memory: Memory,
                 shelf: Store) -> dict[str, Any] | None:
    """Report one registration; return it when its server should be probed."""
    try:
        entry = registration(shelf, model_id)
    except NotFound:
        report.say(f"{model_id}")
        report.problem("not registered (Step 1 printed the registration payload)")
        return None
    settings = entry.get("settings") or {}
    thinking = (settings.get("chat_template_kwargs") or {}).get("enable_thinking")
    report.say(f"{entry['name']} (id {entry['id']})")
    report.say(f"  settings: {describe_settings(settings) or 'harness defaults'}"
               + ("" if thinking is not None else "  [enable_thinking not set]"))
    refusal = refuse_model(entry)
    if refusal:
        report.problem(refusal)
        if not entry["callable"] or not is_local_url(entry["url"]) \
                or url_port(entry["url"]) is None:
            return None     # nothing on this machine to look at or probe

    folder = resolve_model_path(served_model(entry))
    try:
        check_model_folder(folder)
    except LLMModelUnavailable as exc:
        report.problem(str(exc))
        return entry
    with open(os.path.join(folder, "config.json"), encoding="utf-8") as handle:
        config = json.load(handle)
    weights = weights_bytes(folder)
    report.say(f"  folder {folder}")
    report.say(f"  weights {gib(weights)}, {quantization(config)}")
    if not weights:
        report.warn("no *.safetensors files, so memory cannot be checked")
    warning = memory_warning(weights, memory.limit, memory.ram)
    if warning:
        report.warn(warning)
    return entry


def check(model_ids: Sequence[str] = LINEUP, root: str | None = None,
          run: Runner = run_command,
          listening: Callable[[str], bool | None] = server_listening) -> Report:
    """Everything a matrix needs before it starts, without calling a model."""
    report = Report()
    shelf = read_store(root)
    report.say(f"Data root: {shelf.root}")
    report.say("Environment:")
    for line in environment_lines():
        report.say(line)

    memory = read_memory(run)
    report.say("")
    if memory.limit is None:
        report.say("GPU memory limit: not readable")
        report.warn("memory not checked")
    else:
        report.say(f"GPU memory limit: {gib(memory.limit)} ({memory.source})")

    report.say("")
    urls = []
    for model_id in model_ids:
        entry = _check_model(report, model_id, memory, shelf)
        if entry and entry["url"] not in urls:
            urls.append(entry["url"])

    report.say("")
    for url in urls:
        if listening(url):
            report.say(f"Server {url}: listening")
        else:
            report.say(f"Server {url}: not listening")
            port = urllib.parse.urlsplit(url).port
            start = "scripts/start_model_server.sh"
            if port and port != 8080:
                start += f" llama-3.1-8b {port}"
            report.problem(f"start the model server first: {start}")

    for label, path in (("data root", shelf.data_dir), ("copy folder", COPY_ROOT)):
        free = free_bytes(path)
        if free is None:
            report.warn(f"free space on the {label} volume is not readable")
            continue
        report.say(f"Free space ({label}): {gib(free)}")
        if free < LOW_DISK:
            report.warn(f"less than {gib(LOW_DISK)} free for the {label}")

    sent = run(notification_command("MITSS matrix check",
                                    "Notifications work. No model was called."))
    if sent is None:
        report.warn("the test notification could not be sent")
    else:
        report.say("Test notification sent")

    report.say("")
    report.say(f"check: {report.problems} problem(s), {report.warnings} warning(s)")
    return report


# --------------------------------------------------------------------------
# a matrix run's own files: manifest, results.jsonl, summary.txt
# --------------------------------------------------------------------------

MANIFEST = "manifest.json"
RESULTS = "results.jsonl"
SUMMARY = "summary.txt"
FINAL = ("recorded", "failed", "skipped")


def matrices_dir(root: str | None = None) -> str:
    return os.path.join(read_store(root).data_dir, "matrices")


def matrix_dir(matrix_run_id: str, root: str | None = None) -> str:
    # Checked before it touches a path, like every other id.
    if not _is_safe_id(matrix_run_id):
        raise MatrixError([f"not a matrix run id: {matrix_run_id!r}"])
    return os.path.join(matrices_dir(root), matrix_run_id)


def new_matrix_run_id(name: str, root: str | None = None) -> str:
    base = f"{stamp()}-{name}"
    candidate, counter = base, 2
    while os.path.exists(os.path.join(matrices_dir(root), candidate)):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def build_manifest(plan: Plan, matrix_run_id: str, copy_folder: str,
                   only: Sequence[str] | None) -> dict[str, Any]:
    """Everything resume needs, fixed before the first call."""
    matrix = plan.matrix
    keep = ("id", "name", "url", "model", "settings")   # never key_env
    return {
        "matrix_run_id": matrix_run_id,
        "created_at": now(),
        "matrix": {"name": matrix.name, "prompt_id": matrix.prompt_id,
                   "versions": matrix.versions, "inputs": matrix.inputs,
                   "models": matrix.models, "repeats": matrix.repeats},
        "only_models": list(only) if only else None,
        "copy_folder": copy_folder,
        "inputs": plan.inputs,
        "models": [{k: e[k] for k in keep} for e in plan.models],
        "cells": [c.to_dict() for c in plan.cells],
    }


def write_manifest(folder: str, manifest: dict[str, Any]) -> None:
    os.makedirs(folder, exist_ok=True)
    # "x": written once, never replaced.
    with open(os.path.join(folder, MANIFEST), "x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")


def read_manifest(folder: str) -> dict[str, Any]:
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise MatrixError([f"no readable matrix run at {folder}: {exc}"]) from None


def append_result(folder: str, record: dict[str, Any]) -> None:
    """Append one line to results.jsonl and make it durable.

    A line cut short by a hard stop is closed off first, so the new record
    never runs into it; the reader skips the broken line.
    """
    data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    with open(os.path.join(folder, RESULTS), "ab+") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell():
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                data = b"\n" + data
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def read_results(folder: str) -> list[dict[str, Any]]:
    path = os.path.join(folder, RESULTS)
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue            # a line cut short by a hard stop
            if isinstance(record, dict):
                records.append(record)
    return records


def cell_states(results: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Each cell's latest state. A recorded cell stays recorded."""
    states: dict[int, dict[str, Any]] = {}
    for record in results:
        cell = record.get("cell")
        event = record.get("event")
        if not isinstance(cell, int) or event not in ("started",) + FINAL:
            continue
        state = states.setdefault(cell, {})
        if state.get("event") == "recorded":
            continue
        if event == "started":
            state["started_at"] = record.get("at")
        state["event"] = event
        state["record"] = record
    return states


def _moment(stamp_text: str) -> datetime:
    """A store timestamp as local time to the whole second, for comparing."""
    moment = datetime.fromisoformat(stamp_text)
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.replace(microsecond=0)


# --------------------------------------------------------------------------
# summary and notifications
# --------------------------------------------------------------------------

def format_elapsed(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def tally(manifest: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts per model and in total, from the manifest and results alone."""
    states = cell_states(results)
    names = {m["id"]: m["name"] for m in manifest["models"]}
    per_model: dict[str, dict[str, Any]] = {}
    total = {"cells": 0, "recorded": 0, "failed": 0, "skipped": 0,
             "truncated": 0, "not_run": 0, "seconds": 0.0}
    failures, notes = [], []
    for cell in manifest["cells"]:
        counts = per_model.setdefault(cell["model_id"], {
            "name": names.get(cell["model_id"], cell["model_id"]), "cells": 0,
            "recorded": 0, "failed": 0, "skipped": 0, "truncated": 0,
            "not_run": 0, "seconds": 0.0})
        state = states.get(cell["index"], {})
        event = state.get("event")
        kind = event if event in FINAL else "not_run"
        for bucket in (counts, total):
            bucket["cells"] += 1
            bucket[kind] += 1
        record = state.get("record") or {}
        if kind == "recorded" and record.get("truncated"):
            counts["truncated"] += 1
            total["truncated"] += 1
        if kind in ("failed", "skipped"):
            failures.append(f"cell {cell['index']} {describe_cell(cell, names)}: "
                            f"{kind}: {record.get('error') or record.get('reason')}")
        if record.get("adopted") and not record.get("index_event"):
            notes.append(f"cell {cell['index']}: run {record.get('run_id')} was "
                         "adopted but has no run_recorded event in index.jsonl "
                         "(not repaired)")
    for record in results:
        if record.get("event") in ("recorded", "failed") and not record.get("adopted"):
            seconds = (record.get("elapsed_ms") or 0) / 1000
            bucket = per_model.get(_cell_model(manifest, record.get("cell")))
            if bucket is not None:
                bucket["seconds"] += seconds
            total["seconds"] += seconds
        if record.get("event") == "copy_failed":
            what = (f"cell {record['cell']}" if "cell" in record
                    else ", ".join(record.get("files") or ["matrix files"]))
            notes.append(f"{what}: copy failed: {record.get('error')}")
    return {"per_model": per_model, "total": total, "failures": failures, "notes": notes}


def _cell_model(manifest: dict[str, Any], index: Any) -> str | None:
    for cell in manifest["cells"]:
        if cell["index"] == index:
            return cell["model_id"]
    return None


def describe_cell(cell: dict[str, Any], names: dict[str, str]) -> str:
    text = (f"{names.get(cell['model_id'], cell['model_id'])} "
            f"v{cell['version']} × {cell['input_id']}")
    return text + (f" r{cell['repeat']}" if cell.get("repeat", 1) > 1 else "")


def resume_command(matrix_run_id: str) -> str:
    return f"scripts/run_matrix.sh resume {matrix_run_id}"


def recorded_usage(manifest: dict[str, Any], results: list[dict[str, Any]],
                   root: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Each model's recorded runs' usage, read from their run.json, keeping
    only runs that carry the timing and FLOPs keys (flops_method is always
    written with them). Older runs and unreadable ones are left out."""
    shelf = read_store(root)
    by_cell = {c["index"]: c["model_id"] for c in manifest["cells"]}
    usages: dict[str, list[dict[str, Any]]] = {m: [] for m in by_cell.values()}
    for index, state in cell_states(results).items():
        if state.get("event") != "recorded" or index not in by_cell:
            continue
        try:
            usage = shelf.get_run(state["record"]["run_id"]).usage
        except (NotFound, KeyError, ValueError, OSError):
            continue
        if isinstance(usage, dict) and "flops_method" in usage:
            usages[by_cell[index]].append(usage)
    return usages


def _numbers(usages: list[dict[str, Any]], key: str) -> list[float]:
    return [u[key] for u in usages
            if isinstance(u.get(key), (int, float)) and not isinstance(u.get(key), bool)]


def metrics_line(usages: list[dict[str, Any]]) -> str:
    """Median time to first token and decode speed, total tokens and total
    estimated FLOPs, over runs that carry the new keys."""
    if not usages:
        return "    metrics: n/a"
    first = _numbers(usages, "time_to_first_token_ms")
    decode = _numbers(usages, "decode_tokens_per_second")
    flops = _numbers(usages, "flops_estimate")
    parts = [
        (f"first token median {statistics.median(first) / 1000:.1f} s"
         if first else "first token median n/a"),
        (f"decode median {statistics.median(decode):.1f} tok/s"
         if decode else "decode median n/a"),
        (f"{sum(_numbers(usages, 'prompt_tokens')):.0f} in / "
         f"{sum(_numbers(usages, 'completion_tokens')):.0f} out tokens"),
    ]
    if flops:
        missing = len(usages) - len(flops)
        parts.append(format_flops(sum(flops))
                     + (f" ({missing} run(s) without an estimate)" if missing else ""))
    else:
        parts.append("FLOPs n/a")
    return f"    metrics from {len(usages)} run(s): " + " · ".join(parts)


def summary_text(manifest: dict[str, Any], results: list[dict[str, Any]],
                 heading: str, folder: str, root: str | None = None) -> str:
    counts = tally(manifest, results)
    usages = recorded_usage(manifest, results, root)
    matrix = manifest["matrix"]
    lines = [
        f"MITSS matrix {manifest['matrix_run_id']}: {heading}",
        (f"Written {now()}. Prompt {matrix['prompt_id']}, versions "
         f"{', '.join(map(str, matrix['versions']))}, inputs "
         f"{', '.join(matrix['inputs'])}, repeats {matrix['repeats']}."),
        "",
    ]
    for model_id, bucket in counts["per_model"].items():
        lines.append(_count_line(bucket["name"], bucket))
        lines.append(metrics_line(usages.get(model_id, [])))
    lines.append(_count_line("Total", counts["total"]))
    lines.append(metrics_line([u for runs in usages.values() for u in runs]))
    if counts["failures"]:
        lines += ["", "Failed or skipped:"] + [f"  {f}" for f in counts["failures"]]
    if counts["notes"]:
        lines += ["", "Notes:"] + [f"  {n}" for n in counts["notes"]]
    lines += ["", f"Matrix folder: {folder}",
              f"Copy folder:   {manifest['copy_folder']}"]
    if counts["total"]["recorded"] < counts["total"]["cells"]:
        lines.append(f"Resume:        {resume_command(manifest['matrix_run_id'])}")
    return "\n".join(lines) + "\n"


def _count_line(label: str, bucket: dict[str, Any]) -> str:
    parts = [f"recorded {bucket['recorded']}/{bucket['cells']}",
             f"failed {bucket['failed']}", f"skipped {bucket['skipped']}",
             f"truncated {bucket['truncated']}"]
    if bucket["not_run"]:
        parts.append(f"not run {bucket['not_run']}")
    parts.append(format_elapsed(bucket["seconds"]))
    return f"  {label}: " + " · ".join(parts)


def notification_body(total: dict[str, Any], seconds: float) -> str:
    """Counts only. Never prompt, input or output text."""
    parts = [f"{total['recorded']} recorded", f"{total['failed']} failed"]
    if total["skipped"]:
        parts.append(f"{total['skipped']} skipped")
    parts += [f"{total['truncated']} truncated", format_elapsed(seconds)]
    return " · ".join(parts)


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------

class Interrupts:
    """Ctrl-C handling while a matrix runs.

    The first Ctrl-C lets the current cell finish and record, then stops.
    The second raises KeyboardInterrupt at once, abandoning the request in
    flight; resume reconciles that cell from its `started` line.
    """

    def __init__(self, out: Callable[[str], None]):
        self.out = out
        self.requested = False
        self._previous: Any = None

    def __call__(self, signum: int, frame: Any) -> None:
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        self.out("Ctrl-C: stopping after this cell. Press Ctrl-C again to stop now.")

    def install(self) -> None:
        self._previous = signal.signal(signal.SIGINT, self)

    def restore(self) -> None:
        signal.signal(signal.SIGINT, self._previous)


@dataclass
class Session:
    """One `run` or `resume` of a matrix run."""

    folder: str
    manifest: dict[str, Any]
    root: str | None = None
    call: Callable[..., dict[str, Any]] = service.generate_run
    run: Runner = run_command
    out: Callable[[str], None] = print
    interrupts: Interrupts | None = None

    @property
    def matrix_run_id(self) -> str:
        return self.manifest["matrix_run_id"]

    def append(self, record: dict[str, Any]) -> None:
        record.setdefault("at", now())
        append_result(self.folder, record)

    def copy_run(self, cell: dict[str, Any], run_id: str) -> None:
        """Copy a recorded run folder outside the repo; a failure is noted,
        never fatal - the run itself is safe in data/."""
        target = os.path.join(self.manifest["copy_folder"], "runs", run_id)
        try:
            shutil.copytree(read_store(self.root).run_dir(run_id), target,
                            dirs_exist_ok=True)
        except (OSError, NotFound) as exc:
            self.append({"event": "copy_failed", "cell": cell["index"],
                         "run_id": run_id, "error": str(exc)})

    def copy_own_files(self, names: Sequence[str]) -> str | None:
        """Copy the matrix run's own files outside the repo. A failure is
        recorded as a copy_failed event, so the summary and status show it."""
        try:
            os.makedirs(self.manifest["copy_folder"], exist_ok=True)
            for name in names:
                source = os.path.join(self.folder, name)
                if os.path.exists(source):
                    shutil.copy2(source, os.path.join(self.manifest["copy_folder"], name))
        except OSError as exc:
            self.append({"event": "copy_failed", "files": list(names),
                         "error": str(exc)})
            return str(exc)
        return None


def _tag(cell: dict[str, Any], manifest: dict[str, Any]) -> str:
    same_model = [c for c in manifest["cells"] if c["model_id"] == cell["model_id"]]
    position = same_model.index(cell) + 1
    text = (f"[{cell['model']} {position}/{len(same_model)}] "
            f"v{cell['version']} × {cell['input_id']}")
    return text + (f" r{cell['repeat']}" if manifest["matrix"]["repeats"] > 1 else "")


def execute(session: Session, cells: list[dict[str, Any]]) -> tuple[str, str]:
    """Run cells in manifest order. Returns ("finished" | "stopped", reason)."""
    manifest = session.manifest
    models = {m["id"]: m for m in manifest["models"]}
    unavailable: dict[tuple, str] = {}
    for cell in cells:
        if session.interrupts is not None and session.interrupts.requested:
            return "stopped", "stopped by Ctrl-C after the cell in progress"
        entry = models[cell["model_id"]]
        # The same key batch_generate uses: what the server is asked to load.
        served = (entry["url"], served_model(entry))
        tag = _tag(cell, manifest)
        if served in unavailable:
            session.append({"event": "skipped", "cell": cell["index"],
                            "reason": unavailable[served]})
            session.out(f"{tag} … skipped: {unavailable[served]}")
            continue

        session.append({"event": "started", "cell": cell["index"]})
        started = time.monotonic()
        try:
            run = session.call(manifest["matrix"]["prompt_id"], cell["version"],
                               input_id=cell["input_id"], model_id=cell["model_id"],
                               root=session.root)
        except service.ServiceError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            session.append({"event": "failed", "cell": cell["index"],
                            "error": exc.message, "status": exc.status,
                            "elapsed_ms": elapsed})
            session.out(f"{tag} … FAILED {elapsed / 1000:.1f} s: {exc.message}")
            if exc.unavailable:
                unavailable[served] = exc.message
            if exc.status == 503:
                # A stuck server fails every later cell the same way.
                return "stopped", f"the model server is stuck: {exc.message}"
            continue
        elapsed = int((time.monotonic() - started) * 1000)
        finish = (run.get("usage") or {}).get("finish_reason")
        truncated = finish == "length"
        session.append({"event": "recorded", "cell": cell["index"],
                        "run_id": run["id"], "finish_reason": finish,
                        "truncated": truncated, "elapsed_ms": elapsed})
        session.out(f"{tag} … recorded {elapsed / 1000:.1f} s"
                    + ("  TRUNCATED" if truncated else ""))
        session.copy_run(cell, run["id"])
    return "finished", ""


def run_session(session: Session, work: Callable[[], tuple[str, str]],
                action: str) -> int:
    """Do a session's work under Ctrl-C handling, then always write the
    summary and notify, however it ends. Every write to the matrix run
    (adoptions, results, copies) happens inside `work`."""
    started = time.monotonic()
    session.append({"event": "session", "action": action})
    outcome, reason = "stopped", ""
    session.interrupts = Interrupts(session.out)
    session.interrupts.install()
    try:
        outcome, reason = work()
    except KeyboardInterrupt:
        outcome, reason = "stopped", "stopped at once by a second Ctrl-C"
    except Exception as exc:
        # A bug or a full disk must still leave a summary and a notification.
        logging.getLogger(__name__).exception("matrix stopped by an unexpected error")
        outcome, reason = "stopped", f"stopped by an unexpected error: {exc!r}"
    finally:
        session.interrupts.restore()
        session.interrupts = None
    return finish_session(session, outcome, reason, time.monotonic() - started)


def finish_session(session: Session, outcome: str, reason: str,
                   seconds: float) -> int:
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)   # let it finish
    try:
        session.append({"event": "session_end", "outcome": outcome, "reason": reason})
        results = read_results(session.folder)
        total = tally(session.manifest, results)["total"]
        complete = total["recorded"] == total["cells"]
        if outcome == "refused":
            heading, code = f"not resumed - {reason}", EXIT_INVALID
        elif outcome == "stopped":
            heading, code = f"stopped - {reason}", EXIT_STOPPED
        elif complete:
            heading, code = "finished, every cell recorded", EXIT_OK
        else:
            heading, code = "finished with failed or skipped cells", EXIT_FAILED_CELLS
        # Manifest and results first, so a failure to copy them is recorded
        # and appears in this summary; the summary itself is copied last.
        copy_error = session.copy_own_files((MANIFEST, RESULTS))
        if copy_error:
            results = read_results(session.folder)
        text = summary_text(session.manifest, results, heading, session.folder,
                            session.root)
        path = os.path.join(session.folder, SUMMARY)
        with open(path, "a", encoding="utf-8") as handle:   # append-only too
            if handle.tell():
                handle.write("\n" + "=" * 72 + "\n\n")
            handle.write(text)
        copy_error = session.copy_own_files((SUMMARY,)) or copy_error
        session.out("")
        session.out(text.rstrip())
        if copy_error:
            session.out(f"WARNING: the copy folder was not updated: {copy_error}")

        if outcome in ("stopped", "refused"):
            session.run(notification_command(
                "MITSS matrix stopped",
                f"{reason[:120]}. Resume: {resume_command(session.matrix_run_id)}",
                sound="Basso"))
        else:
            session.run(notification_command("MITSS matrix finished",
                                             notification_body(total, seconds)))
        return code
    finally:
        signal.signal(signal.SIGINT, previous)


# --------------------------------------------------------------------------
# run, resume, status
# --------------------------------------------------------------------------

def preflight(models: list[dict[str, Any]], root: str | None, copy_folder: str,
              run: Runner, listening: Callable[[str], bool | None]
              ) -> tuple[list[str], list[str]]:
    """(problems, warnings) before any call. No model is touched."""
    problems, warnings = [], []
    for url in dict.fromkeys(m["url"] for m in models):
        if not listening(url):
            problems.append(f"the model server is not listening at {url}; start "
                            "it first: scripts/start_model_server.sh")
    memory = read_memory(run)
    for entry in models:
        folder = resolve_model_path(served_model(entry))
        try:
            check_model_folder(folder)
        except LLMModelUnavailable as exc:
            problems.append(f"{entry['name']}: {exc}")
            continue
        warning = memory_warning(weights_bytes(folder), memory.limit, memory.ram)
        if warning:
            warnings.append(f"{entry['name']}: {warning}")
    for label, path in (("data root", read_store(root).data_dir),
                        ("copy folder", copy_folder)):
        free = free_bytes(path)
        if free is not None and free < LOW_DISK:
            warnings.append(f"less than {gib(LOW_DISK)} free for the {label}")
    return problems, warnings


def _print_findings(out: Callable[[str], None], problems: list[str],
                    warnings: list[str]) -> None:
    for warning in warnings:
        out(f"WARNING: {warning}")
    for problem in problems:
        out(f"PROBLEM: {problem}")


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


def start_matrix(matrix_path: str, only: Sequence[str] | None = None,
                 yes: bool = False, root: str | None = None,
                 copy_root: str = COPY_ROOT,
                 call: Callable[..., dict[str, Any]] = service.generate_run,
                 run: Runner = run_command,
                 listening: Callable[[str], bool | None] = server_listening,
                 ask: Callable[[str], str] = _ask,
                 out: Callable[[str], None] = print) -> int:
    """`run`: plan, preflight, confirm, write the manifest, run every cell."""
    try:
        plan = build_plan(load_matrix(matrix_path), only, root=root)
    except MatrixError as exc:
        out(f"Matrix refused ({len(exc.problems)} problem(s)):")
        for problem in exc.problems:
            out(f"  - {problem}")
        return EXIT_INVALID
    out(format_plan(plan, {e["id"]: estimate_cell_seconds(e, root) for e in plan.models}))

    matrix_run_id = new_matrix_run_id(plan.matrix.name, root)
    copy_folder = os.path.join(os.path.expanduser(copy_root), matrix_run_id)
    problems, warnings = preflight(plan.models, root, copy_folder, run, listening)
    out("")
    _print_findings(out, problems, warnings)
    if problems:
        return EXIT_INVALID
    if not yes and not _ask_yes(ask, f"Run {len(plan.cells)} cells as {matrix_run_id}? [y/N] "):
        out("Not started.")
        return EXIT_STOPPED

    folder = matrix_dir(matrix_run_id, root)
    manifest = build_manifest(plan, matrix_run_id, copy_folder, only)
    write_manifest(folder, manifest)
    out(f"Matrix run {matrix_run_id}: {folder}")
    session = Session(folder, manifest, root, call, run, out)
    return run_session(session, lambda: execute(session, manifest["cells"]), "run")


def _ask_yes(ask: Callable[[str], str], prompt: str) -> bool:
    return ask(prompt).strip().lower() in ("y", "yes")


def reconcile(session: Session, cells: list[dict[str, Any]],
              states: dict[int, dict[str, Any]]) -> int:
    """Adopt runs recorded by cells that were interrupted before their
    result line was written. Returns how many were adopted.

    The run folder is the record (store.create_run writes it before the
    transcript and the index event), so this looks in the run folders.
    Read-only: a missing index event is reported, never written.
    """
    shelf = read_store(session.root)
    claimed = {s["record"]["run_id"] for s in states.values()
               if s.get("event") == "recorded"}
    events: list[dict[str, Any]] | None = None
    adopted = 0
    for cell in cells:
        state = states.get(cell["index"], {})
        if state.get("event") != "started" or not state.get("started_at"):
            continue
        since = _moment(state["started_at"])
        candidates = [
            r for r in shelf.list_runs(session.manifest["matrix"]["prompt_id"],
                                       cell["version"], cell["model"], cell["input_id"])
            if r.id not in claimed and r.created_at and _moment(r.created_at) >= since
        ]
        if not candidates:
            continue
        run = min(candidates, key=lambda r: (_moment(r.created_at), r.id))
        if events is None:
            events = shelf.read_events()
        indexed = any(e.get("event") == "run_recorded" and e.get("run_id") == run.id
                      for e in events)
        finish = (run.usage or {}).get("finish_reason")
        session.append({"event": "recorded", "cell": cell["index"], "run_id": run.id,
                        "finish_reason": finish, "truncated": finish == "length",
                        "elapsed_ms": run.duration_ms, "adopted": True,
                        "index_event": indexed})
        session.out(f"{_tag(cell, session.manifest)} … adopted {run.id}"
                    + ("" if indexed else " (no index.jsonl event)"))
        claimed.add(run.id)
        adopted += 1
        session.copy_run(cell, run.id)
    return adopted


def resume_matrix(matrix_run_id: str, allow_changed_inputs: bool = False,
                  root: str | None = None,
                  call: Callable[..., dict[str, Any]] = service.generate_run,
                  run: Runner = run_command,
                  listening: Callable[[str], bool | None] = server_listening,
                  out: Callable[[str], None] = print) -> int:
    """`resume`: adopt interrupted cells, then run every unrecorded cell."""
    try:
        folder = matrix_dir(matrix_run_id, root)
        manifest = read_manifest(folder)
    except MatrixError as exc:
        out(f"Cannot resume: {exc.problems[0]}")
        return EXIT_INVALID
    states = cell_states(read_results(folder))
    pending = [c for c in manifest["cells"]
               if states.get(c["index"], {}).get("event") != "recorded"]
    if not pending:
        out(f"Nothing to resume: every cell of {matrix_run_id} is recorded.")
        return EXIT_OK

    problems = _resume_problems(manifest, pending, allow_changed_inputs, root)
    if problems:
        out(f"Resume refused ({len(problems)} problem(s)):")
        for problem in problems:
            out(f"  - {problem}")
        return EXIT_INVALID

    session = Session(folder, manifest, root, call, run, out)

    def work() -> tuple[str, str]:
        # Inside the session, so a Ctrl-C while adopting or copying still
        # ends in a summary and a notification.
        reconcile(session, pending, states)
        now_states = cell_states(read_results(folder))
        remaining = [c for c in pending
                     if now_states.get(c["index"], {}).get("event") != "recorded"]
        if not remaining:
            return "finished", ""
        shelf = read_store(root)
        models = [registration(shelf, m)
                  for m in dict.fromkeys(c["model_id"] for c in remaining)]
        problems, warnings = preflight(models, root, manifest["copy_folder"], run,
                                       listening)
        _print_findings(out, problems, warnings)
        if problems:
            return "refused", problems[0]
        out(f"Resuming {matrix_run_id}: {len(remaining)} cell(s) to run.")
        return execute(session, remaining)

    return run_session(session, work, "resume")


def _resume_problems(manifest: dict[str, Any], pending: list[dict[str, Any]],
                     allow_changed_inputs: bool, root: str | None) -> list[str]:
    """Inputs that changed or vanished, and models that can no longer run.

    Only the inputs and models of cells still to run are checked: a
    recorded cell froze its own texts when it was recorded.
    """
    shelf = read_store(root)
    problems = []
    for input_id in dict.fromkeys(c["input_id"] for c in pending):
        try:
            current = text_sha256(shelf.get_input(input_id).text)
        except NotFound:
            problems.append(f"input '{input_id}' no longer exists")
            continue
        if current != manifest["inputs"][input_id]["sha256"] and not allow_changed_inputs:
            problems.append(f"input '{input_id}' changed since the matrix was "
                            "planned (use --allow-changed-inputs to run it as it is now)")
    for model_id in dict.fromkeys(c["model_id"] for c in pending):
        try:
            refusal = refuse_model(registration(shelf, model_id))
        except NotFound:
            refusal = "is no longer registered"
        if refusal:
            problems.append(f"model '{model_id}' {refusal}")
    return problems


def status_lines(matrix_run_id: str | None = None, root: str | None = None) -> list[str]:
    if matrix_run_id:
        folder = matrix_dir(matrix_run_id, root)
        manifest = read_manifest(folder)
        return summary_text(manifest, read_results(folder), "status",
                            folder, root).rstrip().split("\n")
    base = matrices_dir(root)
    names = sorted(n for n in os.listdir(base) if _is_safe_id(n)) \
        if os.path.isdir(base) else []
    if not names:
        return ["No matrix runs yet."]
    lines = []
    for name in names:
        folder = os.path.join(base, name)
        try:
            manifest = read_manifest(folder)
        except MatrixError:
            continue
        results = read_results(folder)
        total = tally(manifest, results)["total"]
        ends = [r for r in results if r.get("event") == "session_end"]
        last = ends[-1]["outcome"] if ends else "running or interrupted"
        lines.append(f"{name}  {total['recorded']}/{total['cells']} recorded  "
                     f"{total['failed']} failed  {total['skipped']} skipped  ({last})")
    return lines


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:          # type: ignore[override]
        # argparse exits 2 on a usage error, which here means "stopped early".
        self.print_usage(sys.stderr)
        self.exit(EXIT_INVALID, f"{self.prog}: error: {message}\n")


def _model_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="run_matrix.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", metavar="command", parser_class=_Parser)
    sub.required = True
    check_cmd = sub.add_parser("check", help="server, folders, memory, disk, notification")
    check_cmd.add_argument("--models", type=_model_list, default=list(LINEUP),
                           help="registration ids to check (default: the lineup)")
    plan_cmd = sub.add_parser("plan", help="validate a matrix file and list its cells")
    plan_cmd.add_argument("matrix", help="path to the matrix JSON file")
    plan_cmd.add_argument("--models", type=_model_list, default=None,
                          help="only these of the matrix's models")
    run_cmd = sub.add_parser("run", help="run every cell of a matrix file")
    run_cmd.add_argument("matrix", help="path to the matrix JSON file")
    run_cmd.add_argument("--models", type=_model_list, default=None,
                         help="only these of the matrix's models")
    run_cmd.add_argument("--yes", action="store_true", help="start without asking")
    resume_cmd = sub.add_parser("resume", help="finish an interrupted matrix run")
    resume_cmd.add_argument("matrix_run_id")
    resume_cmd.add_argument("--allow-changed-inputs", action="store_true",
                            help="run cells whose input changed since planning")
    status_cmd = sub.add_parser("status", help="list matrix runs, or show one")
    status_cmd.add_argument("matrix_run_id", nargs="?")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "check":
        report = check(args.models)
        print("\n".join(report.lines))
        return EXIT_INVALID if report.problems else EXIT_OK
    if args.command == "run":
        return start_matrix(args.matrix, args.models, args.yes)
    if args.command == "resume":
        return resume_matrix(args.matrix_run_id, args.allow_changed_inputs)
    if args.command == "status":
        try:
            print("\n".join(status_lines(args.matrix_run_id)))
        except MatrixError as exc:
            print(exc.problems[0], file=sys.stderr)
            return EXIT_INVALID
        return EXIT_OK

    try:
        plan = build_plan(load_matrix(args.matrix), args.models)
    except MatrixError as exc:
        print(f"Matrix refused ({len(exc.problems)} problem(s)):", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_INVALID
    estimates = {e["id"]: estimate_cell_seconds(e) for e in plan.models}
    print(format_plan(plan, estimates))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
