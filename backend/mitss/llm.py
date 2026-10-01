"""Harness for plugging in a custom language model.

The core rule: MITSS never depends on a model being reachable. The default
provider is `manual` — the harness hands you a packet, you run it through your
model however you like, and you paste the reply back. Everything downstream
(validation, constraint checking, rendering, diffing, logging) is identical
whether the reply arrived by paste or over HTTP.

Adding your own model means either configuring the built-in `http` provider
with environment variables, or subclassing LLMProvider and registering it.

Credentials: read from the environment at call time, sent once in the request
header, and never logged, echoed in an API response, or written into a run
folder. `describe()` reports only whether a key is present, never its value.

Environment variables:

    MITSS_LLM_PROVIDER   manual (default) | http
    MITSS_LLM_URL        endpoint for the http provider
    MITSS_LLM_FORMAT     openai (default) | raw
    MITSS_LLM_MODEL      default model name sent in the request body
    MITSS_LLM_MODELS     comma-separated list offered in the interface
    MITSS_LLM_API_KEY    optional; sent as "Authorization: Bearer <key>"
    MITSS_LLM_TIMEOUT    idle seconds, default 120; must be a positive finite
                         number. This is the longest gap allowed between two
                         chunks of a streamed reply, not the total time a
                         generation may take - a model still producing tokens
                         is never cut off by it.
    MITSS_LLM_PREFLIGHT_TIMEOUT
                         seconds the one-token probe sent before each run may
                         take, default 90; 0 disables the probe
    MITSS_MAX_TOKENS     max_tokens sent with every generation request,
                         default 1024; a model's own max_tokens setting wins
    MITSS_MODELS_DIR     folder holding local model folders, default
                         ~/Desktop/models. For an endpoint on this machine
                         (127.0.0.1 / localhost) a bare model name is sent as
                         the absolute path of its folder here, and the folder
                         is checked before any request is made.

Transport: openai bodies are streamed (`stream: true`) and the deltas are
accumulated into the same Completion a non-streamed reply produces. A
non-streamed request makes mlx_lm.server write nothing - not even headers -
until the whole completion exists, so a long generation looked like a dead
socket and was cut off mid-way. Connecting has its own 10-second limit.

Retries: a connection failure or an idle timeout is retried once. Anything
the server actually answered (an HTTP error status) is not retried - a 404
from mlx_lm.server means the model could not be loaded and asking again
four seconds later cannot change that.

Generation settings: a provider can be given per-model overrides for the
sampling fields below. Whatever was actually sent comes back with the
completion so a run can record it — a comparison between two outputs is
meaningless unless it shows whether one of them had thinking on.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import socket
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .stream_guard import DEFAULT_LOOP_REPEATS, StreamWatch

DEFAULT_TIMEOUT = 120.0
DEFAULT_PREFLIGHT_TIMEOUT = 90.0
DEFAULT_TEMPERATURE = 0
DEFAULT_MAX_TOKENS = 1024
CONNECT_TIMEOUT = 10.0
DEFAULT_MODELS_DIR = os.path.join("~", "Desktop", "models")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class LLMError(RuntimeError):
    """Any failure reaching or reading from a model."""


class ProviderUnavailable(LLMError):
    """The provider cannot run automatically; use the manual paste path."""


class LLMConfigError(LLMError):
    """The provider is misconfigured (bad timeout, bad setting) — fix it, then retry."""


class LLMTimeout(LLMError):
    """The model server accepted the request but never answered in time."""


class LLMUnreachable(LLMError):
    """No server answered at the endpoint at all (refused, no route, DNS)."""


class LLMStuck(LLMError):
    """The server accepts requests but its generation thread is dead.

    mlx_lm.server keeps answering HTTP after its generator crashes, so a run
    would otherwise hang until the full timeout. Detected by the preflight.
    """


class LLMServerError(LLMError):
    """The model server answered with an HTTP error status."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class LLMModelUnavailable(LLMError):
    """A local model folder is missing or unreadable, so nothing was sent.

    Found by the file preflight before any request; asking the server would
    only come back as a 404 after it tried and failed to load the model.
    """


def parse_max_tokens(value: Any) -> int:
    """A positive integer - MITSS_MAX_TOKENS is refused otherwise."""
    try:
        tokens = int(str(value).strip())
    except (TypeError, ValueError):
        raise LLMConfigError(
            f"MITSS_MAX_TOKENS must be a positive integer, got {value!r}"
        ) from None
    if tokens < 1:
        raise LLMConfigError(
            f"MITSS_MAX_TOKENS must be a positive integer, got {value!r}"
        )
    return tokens


def models_dir() -> str:
    """MITSS_MODELS_DIR, or ~/Desktop/models, expanded at call time."""
    raw = os.environ.get("MITSS_MODELS_DIR", "").strip() or DEFAULT_MODELS_DIR
    return os.path.abspath(os.path.expanduser(raw))


def is_local_url(url: str) -> bool:
    """True for an endpoint on this machine, whose model folders we can see."""
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return False
    return (host or "").lower() in LOCAL_HOSTS


def resolve_model_path(name: str) -> str:
    """The folder a local model name refers to.

    mlx_lm.server loads whatever path the request names, but a bare folder
    name is resolved against its own working directory and comes back 404.
    An absolute path (or ~) is taken as given; anything else is a folder
    under MITSS_MODELS_DIR.
    """
    expanded = os.path.expanduser(name)
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(models_dir(), name)


def check_model_folder(path: str) -> None:
    """Refuse a model folder the server could not load: missing, or no
    readable config.json."""
    if not os.path.isdir(path):
        raise LLMModelUnavailable(f"model folder not found: {path}")
    config = os.path.join(path, "config.json")
    if not os.path.isfile(config):
        raise LLMModelUnavailable(f"model folder has no config.json: {path}")
    try:
        with open(config, encoding="utf-8") as handle:
            json.load(handle)
    except (OSError, ValueError) as exc:
        raise LLMModelUnavailable(
            f"config.json in {path} is not readable: {exc}"
        ) from None


# --------------------------------------------------------------------------
# generation settings
# --------------------------------------------------------------------------

# Fields the openai-style request body may carry, and how each is checked.
# Mirrors what mlx_lm.server validates on its side, so a bad value is refused
# here with a readable message instead of coming back as a bare HTTP 400.
# (min_p and presence_penalty are here because Qwen's published thinking-mode
# settings use them and the server honours them; repetition_penalty and
# frequency_penalty because mlx_lm.server accepts them too.)
#   name: (accepted types, minimum, maximum)
_NUMERIC_SETTINGS: dict[str, tuple] = {
    "temperature": ((int, float), 0, None),
    "top_p": ((int, float), 0, 1),
    "top_k": ((int,), 0, None),
    "min_p": ((int, float), 0, 1),
    "presence_penalty": ((int, float), None, None),
    "repetition_penalty": ((int, float), 1, None),
    "frequency_penalty": ((int, float), None, None),
    "max_tokens": ((int,), 1, None),
    "seed": ((int,), 0, None),
    # MITSS-side, never sent to the server (see mitss.stream_guard). 0 is off.
    "thinking_budget": ((int,), 0, None),
    "loop_repeats": ((int,), 0, None),
}
# Settings that act in MITSS rather than in the request body.
CLIENT_SETTINGS = ("timeout", "thinking_budget", "loop_repeats")
SETTING_NAMES = tuple(_NUMERIC_SETTINGS) + ("chat_template_kwargs", "timeout")


def parse_timeout(value: Any, source: str = "timeout") -> float:
    """A positive, finite number of seconds — anything else is refused.

    urllib treats 0 and negatives as "no wait", and float('inf') / NaN
    slip through arithmetic into confusing socket errors. Say so up front.
    """
    if isinstance(value, bool) or value is None:
        raise LLMConfigError(f"{source} must be a positive finite number of seconds, got {value!r}")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise LLMConfigError(
            f"{source} must be a positive finite number of seconds, got {value!r}"
        ) from None
    if math.isnan(seconds) or math.isinf(seconds) or seconds <= 0:
        raise LLMConfigError(
            f"{source} must be a positive finite number of seconds, got {value!r}"
        )
    return seconds


def parse_preflight_timeout(value: Any) -> float:
    """Like parse_timeout, but 0 is allowed and means "no preflight"."""
    try:
        if float(value) == 0:
            return 0.0
    except (TypeError, ValueError):
        pass
    return parse_timeout(value, "MITSS_LLM_PREFLIGHT_TIMEOUT")


def normalize_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate per-model generation settings and drop blanks.

    Blank means "use the default": None, "" and missing keys are all removed,
    so a form with empty boxes round-trips to an empty dict. Unknown names are
    refused rather than silently ignored — a misspelt `temprature` would
    otherwise look like it had been applied.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LLMConfigError("settings must be a JSON object")

    clean: dict[str, Any] = {}
    for name, value in raw.items():
        if value is None or value == "":
            continue
        if name not in SETTING_NAMES:
            raise LLMConfigError(
                f"unknown setting '{name}'; allowed: {', '.join(SETTING_NAMES)}"
            )
        if name == "timeout":
            clean[name] = parse_timeout(value, "timeout")
            continue
        if name == "chat_template_kwargs":
            if not isinstance(value, dict):
                raise LLMConfigError("chat_template_kwargs must be a JSON object, "
                                     "e.g. {\"enable_thinking\": false}")
            if value:
                clean[name] = dict(value)
            continue
        types, low, high = _NUMERIC_SETTINGS[name]
        if isinstance(value, bool) or not isinstance(value, types):
            wanted = "an integer" if types == (int,) else "a number"
            raise LLMConfigError(f"{name} must be {wanted}, got {value!r}")
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise LLMConfigError(f"{name} must be a finite number, got {value!r}")
        if low is not None and value < low:
            raise LLMConfigError(f"{name} must be at least {low}, got {value!r}")
        if high is not None and value > high:
            raise LLMConfigError(f"{name} must be at most {high}, got {value!r}")
        clean[name] = value
    return clean


def describe_settings(settings: dict[str, Any] | None) -> str:
    """One readable line, e.g. `temperature=0  timeout=120s  enable_thinking=false`."""
    if not settings:
        return ""
    parts = []
    for name, value in settings.items():
        if name == "chat_template_kwargs" and isinstance(value, dict):
            parts.extend(f"{k}={json.dumps(v)}" for k, v in value.items())
        elif name == "timeout":
            parts.append(f"timeout={value:g}s")
        else:
            parts.append(f"{name}={value}")
    return "  ".join(parts)


def redact(text: str, *secrets: str | None) -> str:
    """Blank out credential values anywhere in text.

    Provider error bodies are quoted back to the operator, and a server or
    proxy may echo the Authorization header it received ("Invalid
    authorization: Bearer sk-..."). Everything derived from a response goes
    through here before it is raised, logged, stored or returned.
    """
    for secret in secrets:
        if secret and len(secret) >= 4:
            # The header form first: replacing the bare value first would
            # leave a dangling "Bearer" behind.
            text = text.replace(f"Bearer {secret}", "[REDACTED]")
            text = text.replace(secret, "[REDACTED]")
    return text


@dataclass
class Completion:
    """What came back, plus exactly which settings the request carried.

    `reasoning` is a thinking model's chain of thought when the server
    returns it separately (mlx_lm.server does, as `message.reasoning`).
    Empty for models that do not think or servers that fold it into the text.
    """

    text: str
    settings: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    # What the server reported about the work: token counts, why it stopped,
    # which model it says it used. None when the server reports nothing -
    # never estimated from words or characters.
    usage: dict[str, Any] | None = None
    # Seconds spent on the generation request alone, with the preflight probe
    # (and any model load it absorbed) excluded. This is what a throughput
    # figure must be measured against, or swapping models would look slow.
    request_seconds: float = 0.0
    # Seconds from sending the generation request to the first delta with
    # any text ("first_token"), the first with answer text ("first_answer")
    # and the last with any text ("last_token"). Only a streamed reply has
    # them; a missing mark was never seen.
    timing: dict[str, float] = field(default_factory=dict)
    # The local model folder the request named, so its config.json can be
    # read. Empty for a remote endpoint, whose folder this machine cannot see.
    model_folder: str = ""


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------

class LLMProvider(ABC):
    """Interface every provider implements.

    To add one: subclass, implement `available` and `complete`, then call
    `register_provider("myname", MyProvider)`. Nothing else in the codebase
    needs to change.
    """

    name = "base"

    @property
    @abstractmethod
    def available(self) -> bool:
        """True if complete() can be called right now."""

    @abstractmethod
    def complete(self, prompt: str, model: str | None = None) -> str:
        """Send the prompt, return the raw reply text.

        `model` overrides the configured default for this one call, so an
        interface can offer a choice without reconfiguring the provider.
        """

    def generate(self, prompt: str, model: str | None = None) -> Completion:
        """Like complete(), but also reports the settings that were sent.

        Providers that do not expose settings report none; the run is still
        recorded, just without a settings line.
        """
        return Completion(self.complete(prompt, model), {})

    @property
    def models(self) -> list[str]:
        """Model names this provider can be asked for. Empty means unknown."""
        return []

    def describe(self) -> dict[str, Any]:
        """Non-secret description for the API and the interface."""
        return {"provider": self.name, "available": self.available,
                "models": self.models}


class ManualProvider(LLMProvider):
    """The default. Produces no completion; the operator carries the packet."""

    name = "manual"

    @property
    def available(self) -> bool:
        return False

    def complete(self, prompt: str, model: str | None = None) -> str:
        raise ProviderUnavailable(
            "the manual provider does not call a model - copy the packet, run it "
            "through your model, and paste the reply back"
        )

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": False,
            "models": [],
            "mode": "paste",
            "note": "set MITSS_LLM_PROVIDER=http and MITSS_LLM_URL to call a model directly",
        }


class HttpProvider(LLMProvider):
    """Calls any HTTP endpoint. Standard library only — no vendor SDK.

    Two body shapes are supported. `openai` sends the chat-completions shape
    that llama.cpp, vLLM, Ollama, LM Studio and mlx_lm.server all accept, and
    is the only shape generation settings are added to. `raw` sends
    {"prompt": ...} and accepts a completion under any of several common keys.
    """

    name = "http"
    # Pause before the one retry a connection failure or idle timeout gets.
    retry_delay = 1.0

    def __init__(self, url: str | None = None, fmt: str | None = None,
                 model: str | None = None, timeout: float | None = None,
                 key_env: str | None = None,
                 settings: dict[str, Any] | None = None,
                 preflight: float | None = None):
        self.url = url or os.environ.get("MITSS_LLM_URL", "")
        self.format = (fmt or os.environ.get("MITSS_LLM_FORMAT", "openai")).lower()
        self.model = model or os.environ.get("MITSS_LLM_MODEL", "local-model")
        # Which environment variable holds the key — never the key itself, so
        # a provider built from a stored registration still cannot persist one.
        self.key_env = key_env or "MITSS_LLM_API_KEY"
        # Per-model overrides (a registration's settings). Validated again
        # here so a hand-edited model.json cannot smuggle in a bad value.
        self.settings = normalize_settings(settings)
        # Timeout precedence: the model's own setting, then the constructor,
        # then the environment, then the default. Bad values are refused now,
        # not at the moment a run is already in flight.
        if "timeout" in self.settings:
            self.timeout = self.settings["timeout"]
        elif timeout is not None:
            self.timeout = parse_timeout(timeout, "timeout")
        else:
            self.timeout = parse_timeout(
                os.environ.get("MITSS_LLM_TIMEOUT", str(DEFAULT_TIMEOUT)),
                "MITSS_LLM_TIMEOUT",
            )
        # How long the one-token probe before a run may take. It has to
        # cover a model swap (loading a 12B model takes tens of seconds), so
        # it is not tiny; it only needs to be far shorter than the run timeout.
        self.preflight_timeout = parse_preflight_timeout(
            preflight if preflight is not None
            else os.environ.get("MITSS_LLM_PREFLIGHT_TIMEOUT",
                                str(DEFAULT_PREFLIGHT_TIMEOUT))
        )
        # Every generation request carries a cap. The model's own setting
        # wins over the environment, so a thinking model registered with
        # max_tokens 32768 is not cut to the default.
        self.max_tokens = parse_max_tokens(
            os.environ.get("MITSS_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))
        )
        self.connect_timeout = CONNECT_TIMEOUT

    @property
    def available(self) -> bool:
        return bool(self.url)

    @property
    def models(self) -> list[str]:
        """Names offered in the interface.

        MITSS_LLM_MODELS is a comma-separated list; if it is unset the single
        MITSS_LLM_MODEL is the only choice. Order is preserved and duplicates
        are dropped, so the list reads the way it was written.
        """
        raw = os.environ.get("MITSS_LLM_MODELS", "")
        names: list[str] = []
        for candidate in raw.split(","):
            cleaned = candidate.strip()
            if cleaned and cleaned not in names:
                names.append(cleaned)
        if names:
            return names
        return [self.model] if self.model else []

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": self.available,
            "url": self.url or None,
            "format": self.format,
            "model": self.model,
            "models": self.models,
            "timeout": self.timeout,
            "preflight_timeout": self.preflight_timeout,
            "max_tokens": self.max_tokens,
            "settings": self.settings,
            # Presence only. The value is never exposed.
            "api_key_set": bool(os.environ.get(self.key_env)),
        }

    def request_settings(self) -> dict[str, Any]:
        """The generation fields this provider will put in an openai body.

        The default is temperature 0 and MITSS_MAX_TOKENS (1024); a model's
        own settings override either. MITSS-side settings (CLIENT_SETTINGS)
        never reach the body.
        """
        sent: dict[str, Any] = {"temperature": DEFAULT_TEMPERATURE,
                                "max_tokens": self.max_tokens}
        for name, value in self.settings.items():
            if name not in CLIENT_SETTINGS:
                sent[name] = value
        return sent

    def stream_watch(self) -> StreamWatch:
        """Fresh guards for one attempt, from this model's settings."""
        return StreamWatch(self.settings.get("loop_repeats", DEFAULT_LOOP_REPEATS),
                           self.settings.get("thinking_budget", 0))

    def complete(self, prompt: str, model: str | None = None) -> str:
        return self.generate(prompt, model).text

    def generate(self, prompt: str, model: str | None = None) -> Completion:
        if not self.url:
            raise ProviderUnavailable(
                "MITSS_LLM_URL is not set; cannot call a model automatically"
            )

        # The chosen model must reach the request body, not just the run's
        # label. Labelling a run with a model the endpoint never saw would
        # make every later comparison a lie.
        chosen = model or self.model
        folder = ""
        if is_local_url(self.url):
            # A local server loads models from folders we can see, so ask for
            # the folder by absolute path and check it before sending
            # anything. Remote endpoints are left exactly as configured.
            chosen = resolve_model_path(chosen)
            check_model_folder(chosen)
            folder = chosen

        if self.format == "openai":
            sent = self.request_settings()
            body: dict[str, Any] = {
                "model": chosen,
                "messages": [{"role": "user", "content": prompt}],
            }
            body.update(sent)
        else:
            sent = {}
            body = {"model": chosen, "prompt": prompt}

        # The snapshot a run keeps: what went in the body, plus the client
        # timeout that bounded the wait. Never the key.
        used = dict(sent)
        used["timeout"] = self.timeout

        if self.format == "openai":
            # Streamed, so the idle timeout measures the gap between chunks
            # rather than the whole generation. Usage arrives as a last chunk
            # only when asked for.
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            # The guards that can end a stream early are part of what a run
            # was made with, so they go in the snapshot too: the loop limit
            # always (it is on by default), the thinking budget when set.
            watch = self.stream_watch()
            used["loop_repeats"] = watch.answer_loop.repeats
            if watch.thinking_budget:
                used["thinking_budget"] = watch.thinking_budget

        if self.format == "openai" and self.preflight_timeout > 0:
            self._preflight(chosen)

        started = time.monotonic()  # the attempt that answered, below
        watch = self.stream_watch()
        for attempt in (1, 2):
            started = time.monotonic()
            watch = self.stream_watch()
            try:
                payload = self._post(body, self.timeout, watch)
                break
            except (LLMUnreachable, LLMTimeout):
                # Once only. A server that answered with an error status is
                # not retried: that answer will not change.
                if attempt == 2:
                    raise
                time.sleep(self.retry_delay)
        request_seconds = time.monotonic() - started
        text, reasoning = self._parse(payload)
        timing = {name: moment - started for name, moment in watch.marks.items()}
        usage = _usage(payload)
        if watch.stop:
            # A guard ended the stream; the server never sent its usage chunk.
            usage = {**(usage or {}), **watch.stop}
        return Completion(text, used, reasoning, usage, request_seconds,
                          timing, folder)

    def _preflight(self, chosen: str) -> None:
        """Ask for one token before the real run.

        A server whose generation thread has died still answers HTTP, so the
        only way to tell "stuck" from "slow" is to ask for a token and give
        it a deadline. If the model needs loading, that happens here rather
        than in the run, so the deadline covers a swap.
        """
        probe = {
            "model": chosen,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
        }
        try:
            try:
                self._post(probe, self.preflight_timeout)
            except LLMUnreachable:
                # One retry for a connection failure, as for the run itself.
                time.sleep(self.retry_delay)
                self._post(probe, self.preflight_timeout)
        except LLMTimeout:
            raise LLMStuck(
                f"model server is stuck, restart mlx_lm.server: {self.url} "
                f"accepted a one-token probe but did not answer it within "
                f"{self.preflight_timeout:g}s (the prompt was not sent)"
            ) from None

    def _post(self, body: dict[str, Any], timeout: float,
              watch: StreamWatch | None = None) -> str:
        """POST the body and return the reply as one JSON document.

        `timeout` is an idle limit: the longest the socket may sit without
        receiving a byte. Connecting is bounded separately by
        CONNECT_TIMEOUT. A streamed (text/event-stream) reply is accumulated
        into the same shape a non-streamed one has, so parsing is shared.
        A streamed reply is also fed through `watch` (see _read_stream).
        """
        try:
            parts = urllib.parse.urlsplit(self.url)
            # Given explicitly: http.client misreads a bare IPv6 host with no
            # port ("::1") as host "::" port 1.
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as exc:
            raise LLMUnreachable(f"model server url is not usable: {exc}") from None
        connection_class = (http.client.HTTPSConnection if parts.scheme == "https"
                            else http.client.HTTPConnection)
        connection = connection_class(parts.hostname or "", port,
                                      timeout=self.connect_timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            try:
                # socket.create_connection under the connect timeout.
                connection.connect()
            # socket.timeout is only an alias of TimeoutError from 3.10; CI
            # also runs the core on 3.9.
            except (socket.timeout, TimeoutError):
                raise LLMUnreachable(
                    f"the model server at {self.url} did not accept a "
                    f"connection within {self.connect_timeout:g}s"
                ) from None
            except ConnectionRefusedError:
                raise LLMUnreachable(
                    f"could not connect to the model server at {self.url} "
                    "(connection refused) - is mlx_lm.server running?"
                ) from None
            except OSError as exc:
                raise LLMUnreachable(
                    f"could not reach the model server at {self.url}: {exc}"
                ) from None
            # From here on every read waits at most `timeout` for its next
            # byte - an idle gap, not a total.
            connection.sock.settimeout(timeout)
            connection.request("POST", path, body=json.dumps(body).encode("utf-8"),
                               headers=self._headers())
            response = connection.getresponse()
            if response.status >= 400:
                # Deliberately does not echo the request, which carries the
                # key. The server's own error text usually says why - but a
                # server can echo the Authorization header it was sent, so
                # redact first.
                said = redact(_server_said(response.read()),
                              os.environ.get(self.key_env))
                raise LLMServerError(
                    f"model server returned HTTP {response.status}{said}",
                    response.status,
                )
            content_type = (response.getheader("Content-Type") or "").lower()
            if content_type.startswith("text/event-stream"):
                return self._read_stream(response, watch)
            return response.read().decode("utf-8")
        except (socket.timeout, TimeoutError):
            raise LLMTimeout(self._timeout_message(timeout)) from None
        except (ConnectionResetError, BrokenPipeError, http.client.IncompleteRead):
            raise LLMUnreachable(
                f"the model server at {self.url} dropped the connection "
                "mid-request - it may have crashed; check its log"
            ) from None
        except http.client.HTTPException as exc:
            raise LLMUnreachable(
                f"the model server at {self.url} sent an unreadable reply: "
                f"{type(exc).__name__}"
            ) from None
        except OSError as exc:
            raise LLMUnreachable(
                f"could not reach the model server at {self.url}: {exc}"
            ) from None
        finally:
            connection.close()

    def _read_stream(self, response: http.client.HTTPResponse,
                     watch: StreamWatch | None = None) -> str:
        """Accumulate server-sent events into one chat-completion document.

        Content and reasoning deltas are joined in order; finish_reason,
        usage and the model name are taken from whichever chunk carries
        them. `: keepalive` comment lines (mlx_lm.server sends them during
        prompt processing) are skipped - but they still reset the idle timer.

        Every delta's text goes through `watch`, which marks when text
        arrived and may end the reply early (a loop, or a thinking budget
        spent before any answer). Then reading stops with what has arrived,
        under the guard's finish_reason, and closing the connection tells
        the server to stop generating.
        """
        if watch is None:
            watch = StreamWatch()
        content: list[str] = []
        reasoning: list[str] = []
        saw_content = False
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None
        served: str | None = None
        done = False
        while True:
            line = response.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("data:"):
                continue  # blank separators, comments, event names
            data = text[len("data:"):].strip()
            if data == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                raise LLMError("the model server sent an unreadable stream chunk") from None
            if not isinstance(chunk, dict):
                continue
            if "error" in chunk:
                problem = chunk["error"]
                if isinstance(problem, dict):
                    problem = problem.get("message", "")
                said = redact(" ".join(str(problem).split())[:200],
                              os.environ.get(self.key_env))
                raise LLMError(f"the model server reported an error mid-stream: {said}")
            if isinstance(chunk.get("model"), str) and chunk["model"]:
                served = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices")
            if not (isinstance(choices, list) and choices
                    and isinstance(choices[0], dict)):
                continue
            first = choices[0]
            delta = first.get("delta")
            if isinstance(delta, dict):
                piece = delta.get("content")
                if isinstance(piece, str):
                    saw_content = True
                    content.append(piece)
                thought = delta.get("reasoning")
                if not isinstance(thought, str):
                    thought = delta.get("reasoning_content")
                if isinstance(thought, str):
                    reasoning.append(thought)
                if watch.see(piece, thought):
                    finish_reason = watch.stop["finish_reason"]
                    break
            if isinstance(first.get("finish_reason"), str) and first["finish_reason"]:
                finish_reason = first["finish_reason"]

        if not done and finish_reason is None:
            # The socket closed before the server said it was finished.
            raise LLMUnreachable(
                f"the model server at {self.url} closed the stream before the "
                "completion finished - it may have crashed; check its log"
            )

        message: dict[str, Any] = {"role": "assistant"}
        if saw_content:
            message["content"] = "".join(content)
        if reasoning:
            message["reasoning"] = "".join(reasoning)
        choice: dict[str, Any] = {"index": 0, "message": message}
        if finish_reason:
            choice["finish_reason"] = finish_reason
        document: dict[str, Any] = {"choices": [choice]}
        if usage is not None:
            document["usage"] = usage
        if served:
            document["model"] = served
        return json.dumps(document)

    def _timeout_message(self, timeout: float) -> str:
        return (
            f"the model server at {self.url} did not answer within "
            f"{timeout:g}s of its last output (idle timeout; a model that "
            "keeps streaming tokens is never cut off) - mlx_lm.server may be "
            "stuck: if the GPU is idle, restart it; if it is busy, raise the "
            "model's timeout setting or MITSS_LLM_TIMEOUT"
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    @classmethod
    def _extract(cls, payload: str) -> str:
        """The completion text only (see _parse)."""
        return cls._parse(payload)[0]

    @staticmethod
    def _parse(payload: str) -> tuple:
        """(text, reasoning) out of a variety of response shapes.

        mlx_lm.server puts a thinking model's chain of thought in
        `message.reasoning` and omits `content` entirely when the answer
        never came (max_tokens ran out mid-think). That is still a result
        worth keeping - it says the cap was too low - so it comes back as an
        empty text with the reasoning attached rather than as an error.
        """
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # Some endpoints just return the text.
            return payload, ""

        if isinstance(data, str):
            return data, ""

        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    message = first.get("message")
                    if isinstance(message, dict):
                        reasoning = message.get("reasoning")
                        if not isinstance(reasoning, str):
                            reasoning = message.get("reasoning_content")
                        if not isinstance(reasoning, str):
                            reasoning = ""
                        if isinstance(message.get("content"), str):
                            return message["content"], reasoning
                        if reasoning:
                            return "", reasoning
                    if isinstance(first.get("text"), str):
                        return first["text"], ""
            for key in ("completion", "response", "output", "text", "content"):
                if isinstance(data.get(key), str):
                    return data[key], ""

        raise LLMError(
            "could not find completion text in the model response; expected an "
            "openai-style 'choices' array or a completion/response/output/text key"
        )


def _usage(payload: str) -> dict[str, Any] | None:
    """Token counts and stop reason, exactly as the server reported them.

    Returns None when the server says nothing. Counts are never inferred from
    the text: a missing number stays missing, so a comparison is never made
    against a guess.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    out: dict[str, Any] = {}
    reported = data.get("usage")
    if isinstance(reported, dict):
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = reported.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                out[name] = value
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        if isinstance(reason, str) and reason:
            out["finish_reason"] = reason
    # What the server says it ran, which can differ from what we asked for.
    served = data.get("model")
    if isinstance(served, str) and served:
        out["model_reported"] = served
    return out or None


def _server_said(body: bytes, limit: int = 200) -> str:
    """A short quote of the server's error body, if it sent one.

    Response bodies come from the server, never from our request - but the
    caller still redacts, because a server can echo the header it received.
    Trimmed so a stack trace does not flood the UI.
    """
    raw = (body or b"").decode("utf-8", "replace").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            inner = data.get("error")
            if isinstance(inner, dict):
                inner = inner.get("message")
            if isinstance(inner, str):
                raw = inner
    except json.JSONDecodeError:
        pass
    raw = " ".join(raw.split())
    if len(raw) > limit:
        raw = raw[:limit] + "..."
    return f": {raw}"


_REGISTRY: dict[str, type] = {
    ManualProvider.name: ManualProvider,
    HttpProvider.name: HttpProvider,
}


def register_provider(name: str, provider_class: type) -> None:
    """Make a custom provider selectable via MITSS_LLM_PROVIDER."""
    if not issubclass(provider_class, LLMProvider):
        raise TypeError("provider_class must subclass LLMProvider")
    _REGISTRY[name.lower()] = provider_class


def available_providers() -> dict[str, str]:
    return {name: cls.__doc__.strip().splitlines()[0] if cls.__doc__ else ""
            for name, cls in _REGISTRY.items()}


def get_provider(name: str | None = None) -> LLMProvider:
    """Return the configured provider. Unknown names fall back to manual.

    Raises LLMConfigError if the provider's configuration is unusable (for
    example MITSS_LLM_TIMEOUT set to 0); that is a fix-your-environment
    error, not a model failure, and callers report it as such.
    """
    key = (name or os.environ.get("MITSS_LLM_PROVIDER", "manual")).lower()
    provider_class = _REGISTRY.get(key)
    if provider_class is None:
        return ManualProvider()
    return provider_class()
