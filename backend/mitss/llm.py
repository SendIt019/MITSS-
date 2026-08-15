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
    MITSS_LLM_MODELS     optional; comma-separated list offered in the UI's
                         model dropdown. Falls back to MITSS_LLM_MODEL.
    MITSS_LLM_API_KEY    optional; sent as "Authorization: Bearer <key>"
    MITSS_LLM_TIMEOUT    seconds, default 120
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


def configured_models(default: str = "") -> list:
    """Models offered in the UI dropdown.

    Reads MITSS_LLM_MODELS (comma-separated) and falls back to a single default
    (MITSS_LLM_MODEL) so a one-model setup still populates the dropdown.
    """
    raw = os.environ.get("MITSS_LLM_MODELS", "")
    models = [m.strip() for m in raw.split(",") if m.strip()]
    if models:
        return models
    return [default] if default else []


class LLMError(RuntimeError):
    """Any failure reaching or reading from a model."""


class ProviderUnavailable(LLMError):
    """The provider cannot run automatically; use the manual paste path."""


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

        `model` is the operator's dropdown choice; when None the provider uses
        its configured default.
        """

    def describe(self) -> Dict[str, Any]:
        """Non-secret description for the API and the interface."""
        return {"provider": self.name, "available": self.available}


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
            "mode": "paste",
            "note": "set MITSS_LLM_PROVIDER=http and MITSS_LLM_URL to call a model directly",
        }


class HttpProvider(LLMProvider):
    """Calls any HTTP endpoint. Standard library only — no vendor SDK.

    Two body shapes are supported. `openai` sends the chat-completions shape
    that llama.cpp, vLLM, Ollama and LM Studio all accept. `raw` sends
    {"prompt": ...} and accepts a completion under any of several common keys.
    """

    name = "http"

    def __init__(self, url: Optional[str] = None, fmt: Optional[str] = None,
                 model: Optional[str] = None, timeout: Optional[float] = None):
        self.url = url or os.environ.get("MITSS_LLM_URL", "")
        self.format = (fmt or os.environ.get("MITSS_LLM_FORMAT", "openai")).lower()
        self.model = model or os.environ.get("MITSS_LLM_MODEL", "local-model")
        try:
            self.timeout = float(timeout or os.environ.get("MITSS_LLM_TIMEOUT", "120"))
        except ValueError:
            self.timeout = 120.0

    @property
    def available(self) -> bool:
        return bool(self.url)

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "available": self.available,
            "url": self.url or None,
            "format": self.format,
            "model": self.model,
            "models": configured_models(self.model),
            # Presence only. The value is never exposed.
            "api_key_set": bool(os.environ.get("MITSS_LLM_API_KEY")),
        }

    def complete(self, prompt: str, model: Optional[str] = None) -> str:
        if not self.url:
            raise ProviderUnavailable(
                "MITSS_LLM_URL is not set; cannot call a model automatically"
            )

        chosen = model or self.model
        if self.format == "openai":
            body = {
                "model": chosen,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            }
        else:
            body = {"model": chosen, "prompt": prompt}

        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Deliberately does not echo the request, which carries the key.
            raise LLMError(f"model endpoint returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise LLMError(f"could not reach the model endpoint: {exc.reason}") from None
        except OSError as exc:
            raise LLMError(f"could not reach the model endpoint: {exc}") from None

        return self._extract(payload)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("MITSS_LLM_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    @staticmethod
    def _extract(payload: str) -> str:
        """Pull the completion text out of a variety of response shapes."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # Some endpoints just return the text.
            return payload

        if isinstance(data, str):
            return data

        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    message = first.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        return message["content"]
                    if isinstance(first.get("text"), str):
                        return first["text"]
            for key in ("completion", "response", "output", "text", "content"):
                if isinstance(data.get(key), str):
                    return data[key]

        raise LLMError(
            "could not find completion text in the model response; expected an "
            "openai-style 'choices' array or a completion/response/output/text key"
        )


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
    """Return the configured provider. Unknown names fall back to manual."""
    key = (name or os.environ.get("MITSS_LLM_PROVIDER", "manual")).lower()
    provider_class = _REGISTRY.get(key)
    if provider_class is None:
        return ManualProvider()
    return provider_class()
