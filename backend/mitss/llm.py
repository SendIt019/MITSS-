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
    MITSS_LLM_TIMEOUT    seconds, default 120; must be a positive finite number
    MITSS_LLM_PREFLIGHT_TIMEOUT
                         seconds the one-token probe sent before each run may
                         take, default 90; 0 disables the probe

Generation settings: a provider can be given per-model overrides for the
sampling fields below. Whatever was actually sent comes back with the
completion so a run can record it — a comparison between two outputs is
meaningless unless it shows whether one of them had thinking on.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT = 120.0
DEFAULT_PREFLIGHT_TIMEOUT = 90.0
DEFAULT_TEMPERATURE = 0


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


# --------------------------------------------------------------------------
# generation settings
# --------------------------------------------------------------------------

# Fields the openai-style request body may carry, and how each is checked.
# Mirrors what mlx_lm.server validates on its side, so a bad value is refused
# here with a readable message instead of coming back as a bare HTTP 400.
# (min_p and presence_penalty are here because Qwen's published thinking-mode
# settings use them and the server honours them.)
#   name: (accepted types, minimum, maximum)
_NUMERIC_SETTINGS: Dict[str, tuple] = {
    "temperature": ((int, float), 0, None),
    "top_p": ((int, float), 0, 1),
    "top_k": ((int,), 0, None),
    "min_p": ((int, float), 0, 1),
    "presence_penalty": ((int, float), None, None),
    "max_tokens": ((int,), 1, None),
    "seed": ((int,), 0, None),
}
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


def normalize_settings(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
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

    clean: Dict[str, Any] = {}
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


def describe_settings(settings: Optional[Dict[str, Any]]) -> str:
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


def redact(text: str, *secrets: Optional[str]) -> str:
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
    settings: Dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    # What the server reported about the work: token counts, why it stopped,
    # which model it says it used. None when the server reports nothing -
    # never estimated from words or characters.
    usage: Optional[Dict[str, Any]] = None
    # Seconds spent on the generation request alone, with the preflight probe
    # (and any model load it absorbed) excluded. This is what a throughput
    # figure must be measured against, or swapping models would look slow.
    request_seconds: float = 0.0


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
    def complete(self, prompt: str, model: Optional[str] = None) -> str:
        """Send the prompt, return the raw reply text.

        `model` overrides the configured default for this one call, so an
        interface can offer a choice without reconfiguring the provider.
        """

    def generate(self, prompt: str, model: Optional[str] = None) -> Completion:
        """Like complete(), but also reports the settings that were sent.

        Providers that do not expose settings report none; the run is still
        recorded, just without a settings line.
        """
        return Completion(self.complete(prompt, model), {})

    @property
    def models(self) -> List[str]:
        """Model names this provider can be asked for. Empty means unknown."""
        return []

    def describe(self) -> Dict[str, Any]:
        """Non-secret description for the API and the interface."""
        return {"provider": self.name, "available": self.available,
                "models": self.models}


class ManualProvider(LLMProvider):
    """The default. Produces no completion; the operator carries the packet."""

    name = "manual"

    @property
    def available(self) -> bool:
        return False

    def complete(self, prompt: str, model: Optional[str] = None) -> str:
        raise ProviderUnavailable(
            "the manual provider does not call a model - copy the packet, run it "
            "through your model, and paste the reply back"
        )

    def describe(self) -> Dict[str, Any]:
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

    def __init__(self, url: Optional[str] = None, fmt: Optional[str] = None,
                 model: Optional[str] = None, timeout: Optional[float] = None,
                 key_env: Optional[str] = None,
                 settings: Optional[Dict[str, Any]] = None,
                 preflight: Optional[float] = None):
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

    @property
    def available(self) -> bool:
        return bool(self.url)

    @property
    def models(self) -> List[str]:
        """Names offered in the interface.

        MITSS_LLM_MODELS is a comma-separated list; if it is unset the single
        MITSS_LLM_MODEL is the only choice. Order is preserved and duplicates
        are dropped, so the list reads the way it was written.
        """
        raw = os.environ.get("MITSS_LLM_MODELS", "")
        names: List[str] = []
        for candidate in raw.split(","):
            cleaned = candidate.strip()
            if cleaned and cleaned not in names:
                names.append(cleaned)
        if names:
            return names
        return [self.model] if self.model else []

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "available": self.available,
            "url": self.url or None,
            "format": self.format,
            "model": self.model,
            "models": self.models,
            "timeout": self.timeout,
            "preflight_timeout": self.preflight_timeout,
            "settings": self.settings,
            # Presence only. The value is never exposed.
            "api_key_set": bool(os.environ.get(self.key_env)),
        }

    def request_settings(self) -> Dict[str, Any]:
        """The generation fields this provider will put in an openai body.

        The default is unchanged from before settings existed: temperature 0
        and nothing else, so the server's own caps apply.
        """
        sent: Dict[str, Any] = {"temperature": DEFAULT_TEMPERATURE}
        for name, value in self.settings.items():
            if name != "timeout":
                sent[name] = value
        return sent

    def complete(self, prompt: str, model: Optional[str] = None) -> str:
        return self.generate(prompt, model).text

    def generate(self, prompt: str, model: Optional[str] = None) -> Completion:
        if not self.url:
            raise ProviderUnavailable(
                "MITSS_LLM_URL is not set; cannot call a model automatically"
            )

        # The chosen model must reach the request body, not just the run's
        # label. Labelling a run with a model the endpoint never saw would
        # make every later comparison a lie.
        chosen = model or self.model

        if self.format == "openai":
            sent = self.request_settings()
            body: Dict[str, Any] = {
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

        if self.format == "openai" and self.preflight_timeout > 0:
            self._preflight(chosen)

        started = time.monotonic()
        payload = self._post(body, self.timeout)
        request_seconds = time.monotonic() - started
        text, reasoning = self._parse(payload)
        return Completion(text, used, reasoning, _usage(payload), request_seconds)

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
            self._post(probe, self.preflight_timeout)
        except LLMTimeout:
            raise LLMStuck(
                f"model server is stuck, restart mlx_lm.server: {self.url} "
                f"accepted a one-token probe but did not answer it within "
                f"{self.preflight_timeout:g}s (the prompt was not sent)"
            ) from None

    def _post(self, body: Dict[str, Any], timeout: float) -> str:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Deliberately does not echo the request, which carries the key.
            # The server's own error text usually says why - but a server can
            # echo the Authorization header it was sent, so redact first.
            said = redact(_server_said(exc), os.environ.get(self.key_env))
            raise LLMServerError(
                f"model server returned HTTP {exc.code}{said}", exc.code,
            ) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise LLMTimeout(self._timeout_message(timeout)) from None
            if isinstance(reason, ConnectionRefusedError):
                raise LLMUnreachable(
                    f"could not connect to the model server at {self.url} "
                    "(connection refused) - is mlx_lm.server running?"
                ) from None
            raise LLMUnreachable(
                f"could not reach the model server at {self.url}: {reason}"
            ) from None
        except (socket.timeout, TimeoutError):
            # A timeout while reading the body surfaces here, not as URLError.
            raise LLMTimeout(self._timeout_message(timeout)) from None
        except ConnectionResetError:
            raise LLMUnreachable(
                f"the model server at {self.url} dropped the connection "
                "mid-request - it may have crashed; check its log"
            ) from None
        except OSError as exc:
            raise LLMUnreachable(
                f"could not reach the model server at {self.url}: {exc}"
            ) from None

    def _timeout_message(self, timeout: float) -> str:
        return (
            f"the model server at {self.url} did not answer within "
            f"{timeout:g}s - it may still be generating (raise the "
            "model's timeout setting or MITSS_LLM_TIMEOUT), or mlx_lm.server "
            "may be stuck: if the GPU is idle, restart it"
        )

    def _headers(self) -> Dict[str, str]:
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


def _usage(payload: str) -> Optional[Dict[str, Any]]:
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

    out: Dict[str, Any] = {}
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


def _server_said(exc: urllib.error.HTTPError, limit: int = 200) -> str:
    """A short quote of the server's error body, if it sent one.

    Response bodies come from the server, never from our request, so they
    cannot carry the key. Trimmed so a stack trace does not flood the UI.
    """
    try:
        raw = exc.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001 - any failure to read just means no quote
        return ""
    finally:
        exc.close()
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


_REGISTRY: Dict[str, type] = {
    ManualProvider.name: ManualProvider,
    HttpProvider.name: HttpProvider,
}


def register_provider(name: str, provider_class: type) -> None:
    """Make a custom provider selectable via MITSS_LLM_PROVIDER."""
    if not issubclass(provider_class, LLMProvider):
        raise TypeError("provider_class must subclass LLMProvider")
    _REGISTRY[name.lower()] = provider_class


def available_providers() -> Dict[str, str]:
    return {name: cls.__doc__.strip().splitlines()[0] if cls.__doc__ else ""
            for name, cls in _REGISTRY.items()}


def get_provider(name: Optional[str] = None) -> LLMProvider:
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
