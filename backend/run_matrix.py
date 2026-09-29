"""Run one prompt across versions x inputs x models, without anyone watching.

Built in steps (docs/runner/RUNNER_SPEC.md). This file has `check` and `plan`;
`run`, `resume` and `status` come in Step 2.

Start it through the wrapper, which loads backend/.env the way dev.sh does.
This file never opens .env itself:

    scripts/run_matrix.sh check [--models ID[,ID...]]
    scripts/run_matrix.sh plan MATRIX.json [--models ID[,ID...]]

Exit codes: 0 all good - 1 finished with failed or skipped cells - 2 stopped
early - 3 validation or preflight error.

Standard library only, and it must run on Python 3.9 (CI runs the core there).
Runs are recorded only through app.service; nothing here writes to data/.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import shutil
import socket
import statistics
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from app import service
from mitss.llm import (
    DEFAULT_MAX_TOKENS, DEFAULT_PREFLIGHT_TIMEOUT, DEFAULT_TIMEOUT,
    LLMModelUnavailable, check_model_folder, describe_settings, is_local_url,
    models_dir, resolve_model_path,
)
# The store's own id alphabet, so a name that passes here is safe as a
# directory name for the same reason a run id is.
from pipeline.store import _SAFE_ID, text_sha256

EXIT_OK = 0
EXIT_FAILED_CELLS = 1
EXIT_STOPPED = 2
EXIT_INVALID = 3

# The models `check` looks at when not told otherwise (DECISIONS.md,
# 2026-09-29 "Lineup").
LINEUP = ("llama-3-1-8b", "qwen3-8-27b", "gemma-4-26b-a4b")

MAX_REPEATS = 10
MATRIX_KEYS = ("name", "prompt_id", "versions", "inputs", "models", "repeats")
# A matrix run id is "<YYYYMMDD-HHMMSS>-<name>" and must fit the store's
# 128-character id limit.
MAX_NAME = 128 - len("20260929-141500-")

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

    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# --------------------------------------------------------------------------
# matrix file
# --------------------------------------------------------------------------

@dataclass
class Matrix:
    name: str
    prompt_id: str
    versions: List[int]
    inputs: List[str]
    models: List[str]
    repeats: int = 1


def _is_safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_ID.fullmatch(value))


def _id_list(raw: Dict[str, Any], key: str, problems: List[str]) -> List[str]:
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
    problems: List[str] = []
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

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "model_id": self.model_id,
                "model": self.model, "version": self.version,
                "input_id": self.input_id, "repeat": self.repeat}


@dataclass
class Plan:
    matrix: Matrix
    models: List[Dict[str, Any]]          # registrations, in run order
    inputs: Dict[str, Dict[str, str]]     # id -> name and sha256 at plan time
    cells: List[Cell] = field(default_factory=list)


def _selected_models(matrix: Matrix, only: Optional[Sequence[str]],
                     problems: List[str]) -> List[str]:
    if not only:
        return list(matrix.models)
    for model_id in only:
        if model_id not in matrix.models:
            problems.append(f"--models: '{model_id}' is not in this matrix "
                            f"({', '.join(matrix.models)})")
    # The matrix file's order wins: it is the order the models load in.
    return [m for m in matrix.models if m in only]


def build_plan(matrix: Matrix, only: Optional[Sequence[str]] = None,
               root: Optional[str] = None) -> Plan:
    """Check the matrix against the store and lay out every cell.

    Every problem is collected before refusing, so one pass fixes the file.
    A quarantined or paste-only model is refused here rather than skipped
    later: a matrix that silently lost a model would compare less than it says.
    """
    problems: List[str] = []
    model_ids = _selected_models(matrix, only, problems)

    try:
        prompt = service.prompt_detail(matrix.prompt_id, root=root)
        known = {v["version"] for v in prompt["versions"]}
        for version in matrix.versions:
            if version not in known:
                problems.append(f"prompt '{matrix.prompt_id}' has no version {version}")
    except service.ServiceError as exc:
        problems.append(f"prompt: {exc.message}")

    inputs: Dict[str, Dict[str, str]] = {}
    for input_id in matrix.inputs:
        try:
            found = service.get_input(input_id, root=root)
        except service.ServiceError as exc:
            problems.append(f"input: {exc.message}")
            continue
        # The same function the store fingerprints runs with, so a plan-time
        # hash compares directly with a run's input_sha256.
        inputs[input_id] = {"name": found["name"],
                            "sha256": text_sha256(found["text"])}

    models: List[Dict[str, Any]] = []
    for model_id in model_ids:
        try:
            entry = service.get_model(model_id, root=root)
        except service.ServiceError as exc:
            problems.append(f"model: {exc.message}")
            continue
        if entry["quarantine"]:
            problems.append(f"model '{model_id}' is quarantined: {entry['quarantine']}")
        elif not entry["callable"]:
            problems.append(f"model '{model_id}' is paste-only (no url)")
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


def _max_tokens(entry: Dict[str, Any]) -> int:
    configured = (entry.get("settings") or {}).get("max_tokens")
    if isinstance(configured, int):
        return configured
    try:
        return int(os.environ.get("MITSS_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    except ValueError:
        return DEFAULT_MAX_TOKENS


def estimate_cell_seconds(entry: Dict[str, Any],
                          root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Seconds per cell from this model's recent runs, or None without data.

    Only runs where the server reported a real token count carry
    tokens_per_second, so the estimate is never built on a guessed rate.
    Output length is the median of those runs, capped at the model's
    max_tokens. Model loading is not included.
    """
    samples = []
    for run in service.list_runs(model=entry["name"], root=root):   # newest first
        usage = run.get("usage") or {}
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


def format_plan(plan: Plan, estimates: Dict[str, Optional[Dict[str, Any]]]) -> str:
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

Runner = Callable[[List[str]], Optional[str]]


def run_command(command: List[str]) -> Optional[str]:
    """stdout of a command that succeeded, else None. No shell, ever."""
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def sysctl_command(name: str) -> List[str]:
    return ["sysctl", "-n", name]


def metal_command() -> List[str]:
    python = os.path.join(os.path.expanduser(MODELS_ENV), "bin", "python")
    return [python, "-c", METAL_PROBE]


def notification_command(title: str, body: str, sound: str = "Glass") -> List[str]:
    """osascript with the text passed as arguments, never built into the script."""
    return ["osascript",
            "-e", "on run argv",
            "-e", ("display notification (item 2 of argv) with title "
                   f"(item 1 of argv) sound name \"{sound}\""),
            "-e", "end run",
            title, body]


def _as_int(text: Optional[str]) -> Optional[int]:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


@dataclass
class Memory:
    limit: Optional[int]    # bytes the GPU may wire, or None if unknown
    source: str
    ram: Optional[int]


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


def memory_warning(weights: int, limit: Optional[int],
                   ram: Optional[int] = None) -> Optional[str]:
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


def quantization(config: Dict[str, Any]) -> str:
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


def server_listening(url: str, timeout: float = 2.0) -> Optional[bool]:
    """Whether something accepts connections at a local endpoint.

    A TCP connect only: no request is sent and no model is touched. Remote
    endpoints are not probed (None) - the runner has no network of its own.
    """
    if not is_local_url(url):
        return None
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def free_bytes(path: str) -> Optional[int]:
    """Free space on the volume holding `path`, or its nearest existing parent."""
    path = os.path.abspath(os.path.expanduser(path))
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return shutil.disk_usage(path).free


def environment_lines(env: Optional[Dict[str, str]] = None) -> List[str]:
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
        self.lines: List[str] = []
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
                 root: Optional[str]) -> Optional[Dict[str, Any]]:
    try:
        entry = service.get_model(model_id, root=root)
    except service.ServiceError:
        report.say(f"{model_id}")
        report.problem("not registered (Step 1 printed the registration payload)")
        return None
    settings = entry.get("settings") or {}
    thinking = (settings.get("chat_template_kwargs") or {}).get("enable_thinking")
    report.say(f"{entry['name']} (id {entry['id']})")
    report.say(f"  settings: {describe_settings(settings) or 'harness defaults'}"
               + ("" if thinking is not None else "  [enable_thinking not set]"))
    if entry["quarantine"]:
        report.problem(f"quarantined: {entry['quarantine']}")
    if not entry["callable"]:
        report.problem("paste-only (no url)")
        return entry
    if not is_local_url(entry["url"]):
        report.say(f"  remote endpoint {entry['url']}; folder and memory not checked")
        return entry

    folder = resolve_model_path(entry["model"])
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


def check(model_ids: Sequence[str] = LINEUP, root: Optional[str] = None,
          run: Runner = run_command,
          listening: Callable[[str], Optional[bool]] = server_listening) -> Report:
    """Everything a matrix needs before it starts, without calling a model."""
    report = Report()
    shelf = service.store(root)
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
        entry = _check_model(report, model_id, memory, root)
        if entry and entry["callable"] and entry["url"] not in urls:
            urls.append(entry["url"])

    report.say("")
    for url in urls:
        state = listening(url)
        if state is None:
            report.say(f"Server {url}: remote, not probed")
        elif state:
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
# command line
# --------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:          # type: ignore[override]
        # argparse exits 2 on a usage error, which here means "stopped early".
        self.print_usage(sys.stderr)
        self.exit(EXIT_INVALID, f"{self.prog}: error: {message}\n")


def _model_list(text: str) -> List[str]:
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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "check":
        report = check(args.models)
        print("\n".join(report.lines))
        return EXIT_INVALID if report.problems else EXIT_OK

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
