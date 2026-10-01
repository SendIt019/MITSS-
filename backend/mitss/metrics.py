"""Timing and compute metrics for one generation request.

Two kinds of number end up in a run's `usage` from here:

- timings taken from the streamed reply: when the first text arrived, when
  the answer itself started, and the decode speed with prompt processing
  taken out;
- an estimate of the floating-point operations the request cost, from the
  model folder's config.json.

The estimate is the standard forward-pass count (Kaplan et al., 2020,
"Scaling Laws for Neural Language Models"). For every token processed:

    2 x active_params  +  2 x n_layers x context_length x attention_width

summed in closed form over the prompt and completion tokens together.
`active_params` counts the weights one token actually passes through: for a
mixture-of-experts model the shared weights plus the routed experts, not
every expert. Embeddings are counted, so the figure matches published model
sizes. Per layer, `context_length` is capped at a sliding window where the
config gives one, and a linear-attention layer (Qwen 3.5's Gated DeltaNet)
adds no context term at all - it keeps a fixed-size state instead of
attending to every earlier token. Quantisation does not change the count:
a 4-bit weight is still one multiply-add.

Only architectures whose parameter count has been checked against the
published size are recognised. Anything else - or a config missing a field
the count needs - gives null with the reason, never a guess.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

# Changes whenever the formula does, so old runs show which one made them.
FLOPS_METHOD = ("kaplan2020-v1: 2*active_params + 2*layers*context*attn_width "
                "per token; MoE routed experts only; sliding windows capped; "
                "linear attention adds no context term; embeddings counted; "
                "4-bit quantisation not counted")

DENSE = ("llama", "mistral", "qwen2", "qwen3")
KNOWN = DENSE + ("qwen3_moe", "gemma4_text", "qwen3_5_text")


class Unsupported(ValueError):
    """config.json cannot be counted; the message says why."""


@dataclass
class Layer:
    """What one decoder layer costs: its weights and its attention reach."""

    params: int            # weights a token passes through in this layer
    total_params: int      # every weight in the layer, every expert included
    attention_width: int   # query heads x head dim; 0 for linear attention
    window: int | None  # sliding window, or None for full context


@dataclass
class Shape:
    active_params: int
    total_params: int
    layers: list[Layer]


# --------------------------------------------------------------------------
# reading config.json
# --------------------------------------------------------------------------

def read_config(folder: str) -> dict[str, Any]:
    path = os.path.join(folder, "config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise Unsupported(f"config.json not readable: {type(exc).__name__}") from None
    if not isinstance(config, dict):
        raise Unsupported("config.json is not an object")
    return config


def _count(cfg: dict[str, Any], name: str) -> int:
    """A positive integer field, or Unsupported naming it."""
    value = cfg.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Unsupported(f"config.json has no usable {name}")
    return value


def _optional_count(cfg: dict[str, Any], name: str, fallback: int) -> int:
    return fallback if cfg.get(name) is None else _count(cfg, name)


def _flag(cfg: dict[str, Any], name: str, default: bool = False) -> bool:
    value = cfg.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise Unsupported(f"config.json has a non-boolean {name}")
    return value


def _tied(config: dict[str, Any], text: dict[str, Any]) -> bool:
    for source in (text, config):
        value = source.get("tie_word_embeddings")
        if isinstance(value, bool):
            return value
    raise Unsupported("config.json has no tie_word_embeddings")


def _head_dim(cfg: dict[str, Any], hidden: int, heads: int) -> int:
    if cfg.get("head_dim") is not None:
        return _count(cfg, "head_dim")
    if hidden % heads:
        raise Unsupported("config.json has no head_dim and hidden_size is not "
                          "a multiple of num_attention_heads")
    return hidden // heads


def _layer_types(cfg: dict[str, Any], layers: int, allowed: tuple[str, ...]) -> list[str]:
    kinds = cfg.get("layer_types")
    if not isinstance(kinds, list) or len(kinds) != layers:
        raise Unsupported("config.json has no layer_types for every layer")
    for kind in kinds:
        if kind not in allowed:
            raise Unsupported(f"unrecognised layer type {kind!r}")
    return kinds


# --------------------------------------------------------------------------
# parameter counts per block
# --------------------------------------------------------------------------

def _attention(hidden: int, heads: int, kv_heads: int, head_dim: int,
               gated: bool = False, k_eq_v: bool = False) -> int:
    """q, k, v and output projections. A gated output doubles q's width;
    keys equal to values share one projection."""
    q = heads * head_dim * (2 if gated else 1)
    kv = kv_heads * head_dim * (1 if k_eq_v else 2)
    return hidden * (q + kv) + heads * head_dim * hidden


def _mlp(hidden: int, intermediate: int) -> int:
    """Gated MLP: gate, up and down projections."""
    return 3 * hidden * intermediate


def _gated_deltanet(cfg: dict[str, Any], hidden: int) -> int:
    """Qwen 3.5's linear-attention block: fused qkvz and b/a projections,
    a short causal convolution, per-head decay terms, a norm and the output
    projection."""
    key_dim = _count(cfg, "linear_num_key_heads") * _count(cfg, "linear_key_head_dim")
    value_heads = _count(cfg, "linear_num_value_heads")
    value_head_dim = _count(cfg, "linear_value_head_dim")
    value_dim = value_heads * value_head_dim
    kernel = _count(cfg, "linear_conv_kernel_dim")
    return (hidden * (2 * key_dim + 2 * value_dim)     # in_proj_qkvz
            + hidden * 2 * value_heads                  # in_proj_ba
            + (2 * key_dim + value_dim) * kernel        # conv1d
            + 2 * value_heads + value_head_dim          # A_log, dt_bias, norm
            + value_dim * hidden)                       # out_proj


def _experts(hidden: int, experts: int, chosen: int, size: int) -> tuple[int, int]:
    """(active, total) for a routed expert block, router included."""
    if chosen > experts:
        raise Unsupported("more experts per token than experts")
    router = hidden * experts
    return router + chosen * _mlp(hidden, size), router + experts * _mlp(hidden, size)


# --------------------------------------------------------------------------
# architectures
# --------------------------------------------------------------------------

def _dense_layers(cfg: dict[str, Any], hidden: int, layers: int) -> list[Layer]:
    """Llama, Mistral, Qwen 2 / 3: full attention and a gated MLP everywhere."""
    heads = _count(cfg, "num_attention_heads")
    kv = _optional_count(cfg, "num_key_value_heads", heads)
    head_dim = _head_dim(cfg, hidden, heads)
    if cfg.get("layer_types") is not None or (
            cfg.get("sliding_window") is not None
            and cfg.get("use_sliding_window") is not False):
        raise Unsupported("sliding-window layout not recognised for "
                          f"{cfg.get('model_type')}")
    attention = _attention(hidden, heads, kv, head_dim)
    if cfg.get("model_type") != "qwen3_moe":
        block = attention + _mlp(hidden, _count(cfg, "intermediate_size"))
        return [Layer(block, block, heads * head_dim, None)] * layers

    step = _optional_count(cfg, "decoder_sparse_step", 1)
    dense_only = cfg.get("mlp_only_layers") or []
    if not isinstance(dense_only, list):
        raise Unsupported("config.json has an unusable mlp_only_layers")
    active, total = _experts(hidden, _count(cfg, "num_experts"),
                             _count(cfg, "num_experts_per_tok"),
                             _count(cfg, "moe_intermediate_size"))
    out = []
    for index in range(layers):
        if index not in dense_only and (index + 1) % step == 0:
            out.append(Layer(attention + active, attention + total,
                             heads * head_dim, None))
        else:
            block = attention + _mlp(hidden, _count(cfg, "intermediate_size"))
            out.append(Layer(block, block, heads * head_dim, None))
    return out


def _gemma4_layers(cfg: dict[str, Any], hidden: int, layers: int) -> list[Layer]:
    """Gemma 4: sliding and full attention layers with their own head shapes,
    a dense MLP in every layer, and a routed expert block beside it when
    enable_moe_block is set."""
    if cfg.get("hidden_size_per_layer_input") or cfg.get("num_kv_shared_layers"):
        raise Unsupported("per-layer inputs or shared KV layers are not counted")
    if _flag(cfg, "use_double_wide_mlp"):
        raise Unsupported("double-wide MLP is not counted")
    kinds = _layer_types(cfg, layers, ("sliding_attention", "full_attention"))
    heads = _count(cfg, "num_attention_heads")
    kv = _optional_count(cfg, "num_key_value_heads", heads)
    head_dim = _head_dim(cfg, hidden, heads)
    global_dim = _optional_count(cfg, "global_head_dim", head_dim)
    global_kv = _optional_count(cfg, "num_global_key_value_heads", kv)
    window = _count(cfg, "sliding_window")
    k_eq_v = _flag(cfg, "attention_k_eq_v")
    mlp = _mlp(hidden, _count(cfg, "intermediate_size"))
    active_moe = total_moe = 0
    if _flag(cfg, "enable_moe_block"):
        active_moe, total_moe = _experts(hidden, _count(cfg, "num_experts"),
                                         _count(cfg, "top_k_experts"),
                                         _count(cfg, "moe_intermediate_size"))
    out = []
    for kind in kinds:
        if kind == "full_attention":
            attention = _attention(hidden, heads, global_kv, global_dim, k_eq_v=k_eq_v)
            width, reach = heads * global_dim, None
        else:
            attention = _attention(hidden, heads, kv, head_dim)
            width, reach = heads * head_dim, window
        out.append(Layer(attention + mlp + active_moe, attention + mlp + total_moe,
                         width, reach))
    return out


def _qwen3_5_layers(cfg: dict[str, Any], hidden: int, layers: int) -> list[Layer]:
    """Qwen 3.5 dense: linear-attention layers with a full-attention layer
    every few, output-gated attention, a gated MLP everywhere."""
    if cfg.get("num_experts"):
        raise Unsupported("Qwen 3.5 mixture-of-experts layout is not counted")
    kinds = _layer_types(cfg, layers, ("linear_attention", "full_attention"))
    heads = _count(cfg, "num_attention_heads")
    kv = _optional_count(cfg, "num_key_value_heads", heads)
    head_dim = _head_dim(cfg, hidden, heads)
    mlp = _mlp(hidden, _count(cfg, "intermediate_size"))
    full = _attention(hidden, heads, kv, head_dim,
                      gated=_flag(cfg, "attn_output_gate"))
    linear = _gated_deltanet(cfg, hidden)
    return [Layer(full + mlp, full + mlp, heads * head_dim, None)
            if kind == "full_attention" else Layer(linear + mlp, linear + mlp, 0, None)
            for kind in kinds]


def model_shape(config: dict[str, Any]) -> Shape:
    """Parameter counts and attention layout, or Unsupported with the reason.

    Multimodal folders keep the language model under text_config; only it is
    counted, because only it runs for a text prompt.
    """
    text = config.get("text_config")
    cfg = text if isinstance(text, dict) else config
    kind = cfg.get("model_type")
    if kind not in KNOWN:
        raise Unsupported(f"architecture {kind!r} not recognised")
    hidden = _count(cfg, "hidden_size")
    layers = _count(cfg, "num_hidden_layers")
    if kind == "gemma4_text":
        blocks = _gemma4_layers(cfg, hidden, layers)
    elif kind == "qwen3_5_text":
        blocks = _qwen3_5_layers(cfg, hidden, layers)
    else:
        blocks = _dense_layers(cfg, hidden, layers)
    embeddings = _count(cfg, "vocab_size") * hidden * (1 if _tied(config, cfg) else 2)
    return Shape(active_params=embeddings + sum(b.params for b in blocks),
                 total_params=embeddings + sum(b.total_params for b in blocks),
                 layers=blocks)


# --------------------------------------------------------------------------
# the estimate
# --------------------------------------------------------------------------

def context_sum(tokens: int, window: int | None = None) -> int:
    """Sum over positions 1..tokens of how many tokens each attends to:
    its own position, capped at the window."""
    if window is None or tokens <= window:
        return tokens * (tokens + 1) // 2
    return window * (window + 1) // 2 + (tokens - window) * window


def flops(shape: Shape, prompt_tokens: int, completion_tokens: int) -> int:
    tokens = prompt_tokens + completion_tokens
    attention = sum(layer.attention_width * context_sum(tokens, layer.window)
                    for layer in shape.layers if layer.attention_width)
    return 2 * shape.active_params * tokens + 2 * attention


def _method(reason: str = "") -> str:
    return f"{FLOPS_METHOD}; not estimated: {reason}" if reason else FLOPS_METHOD


def compute_metrics(folder: str | None, prompt_tokens: Any,
                    completion_tokens: Any) -> dict[str, Any]:
    """active_params, flops_estimate and flops_method for one request.

    Nulls carry their reason in flops_method; nothing is guessed.
    """
    out: dict[str, Any] = {"active_params": None, "flops_estimate": None}
    if not folder:
        out["flops_method"] = _method("no local model folder")
        return out
    try:
        shape = model_shape(read_config(folder))
    except Unsupported as exc:
        out["flops_method"] = _method(str(exc))
        return out
    out["active_params"] = shape.active_params
    if not (_is_count(prompt_tokens) and _is_count(completion_tokens)):
        out["flops_method"] = _method("the server did not report both token counts")
        return out
    out["flops_estimate"] = flops(shape, prompt_tokens, completion_tokens)
    out["flops_method"] = FLOPS_METHOD
    return out


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# --------------------------------------------------------------------------
# timings
# --------------------------------------------------------------------------

def timing_metrics(timing: dict[str, float] | None, prompt_tokens: Any,
                   completion_tokens: Any) -> dict[str, Any]:
    """Timing keys from the stream marks, in seconds since the request was sent.

    `first_token`: first delta carrying any text (answer or thinking).
    `first_answer`: first delta carrying answer text.
    `last_token`: last delta carrying any text.
    A mark that was never seen, or a rate with nothing to divide by, is null.
    """
    marks = timing or {}
    first = marks.get("first_token")
    answer = marks.get("first_answer")
    last = marks.get("last_token")
    out: dict[str, Any] = {
        "time_to_first_token_ms": _ms(first),
        "time_to_first_answer_ms": _ms(answer),
        "prompt_tokens_per_second": None,
        "decode_tokens_per_second": None,
    }
    if _is_count(prompt_tokens) and first is not None and first > 0:
        out["prompt_tokens_per_second"] = round(prompt_tokens / first, 1)
    if (_is_count(completion_tokens) and completion_tokens > 1
            and first is not None and last is not None and last > first):
        out["decode_tokens_per_second"] = round((completion_tokens - 1) / (last - first), 1)
    return out


def _ms(seconds: float | None) -> int | None:
    return None if seconds is None else round(seconds * 1000)


def run_metrics(usage: dict[str, Any] | None, timing: dict[str, float] | None,
                folder: str | None) -> dict[str, Any]:
    """Every new usage key for one run, timings first."""
    usage = usage or {}
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    out = timing_metrics(timing, prompt, completion)
    out.update(compute_metrics(folder, prompt, completion))
    return out
