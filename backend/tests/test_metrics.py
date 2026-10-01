"""Run metrics: the FLOPs estimate from config.json, and the timing keys a
run records through the service.

Configs here are small fakes with numbers small enough to check by hand,
plus the three lineup models rebuilt from the fields the count reads, so
the published sizes are checked without the model folders being present.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_llm import DONE, SCRIPT, SEEN, StreamServer, _delta, _sse

from app import service
from mitss.metrics import (
    FLOPS_METHOD,
    Unsupported,
    compute_metrics,
    context_sum,
    flops,
    model_shape,
    run_metrics,
    timing_metrics,
)
from pipeline.transcript import read as read_transcript

# hidden 8, 2 layers, 2 heads of 4, 1 KV head, MLP 16, vocab 10, untied.
# Per layer: attention 8*(8 + 2*4) + 8*8 = 192, MLP 3*8*16 = 384.
# Embeddings 10*8*2 = 160. Total 2*576 + 160 = 1312.
TINY_LLAMA = {"model_type": "llama", "hidden_size": 8, "num_hidden_layers": 2,
              "num_attention_heads": 2, "num_key_value_heads": 1,
              "intermediate_size": 16, "vocab_size": 10,
              "tie_word_embeddings": False, "quantization": {"bits": 4}}


def write_config(folder, config):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    return folder


class FlopsFormula(unittest.TestCase):
    def test_context_sum_matches_counting_token_by_token(self):
        for tokens in (0, 1, 2, 7, 50):
            for window in (None, 1, 3, 7, 100):
                expected = sum(min(i, window) if window else i
                               for i in range(1, tokens + 1))
                self.assertEqual(context_sum(tokens, window), expected, (tokens, window))

    def test_dense_model_by_hand(self):
        shape = model_shape(TINY_LLAMA)
        self.assertEqual(shape.active_params, 1312)
        self.assertEqual(shape.total_params, 1312)
        # 3 prompt + 2 completion tokens: 2*1312*5 for the weights, plus
        # 2 layers * width 8 * (1+2+3+4+5) for attention, doubled.
        self.assertEqual(flops(shape, 3, 2), 2 * 1312 * 5 + 2 * (2 * 8 * 15))

    def test_prompt_and_completion_tokens_both_count(self):
        shape = model_shape(TINY_LLAMA)
        self.assertEqual(flops(shape, 3, 2), flops(shape, 5, 0))
        self.assertGreater(flops(shape, 3, 2), flops(shape, 3, 0))

    def test_mixture_of_experts_counts_only_the_routed_experts(self):
        config = {"model_type": "qwen3_moe", "hidden_size": 8, "num_hidden_layers": 1,
                  "num_attention_heads": 2, "num_key_value_heads": 2, "head_dim": 4,
                  "intermediate_size": 16, "num_experts": 4, "num_experts_per_tok": 2,
                  "moe_intermediate_size": 4, "decoder_sparse_step": 1,
                  "mlp_only_layers": [], "vocab_size": 10, "tie_word_embeddings": True}
        shape = model_shape(config)
        attention, router, expert, embeddings = 8 * (8 + 16) + 64, 8 * 4, 3 * 8 * 4, 80
        self.assertEqual(shape.active_params, attention + router + 2 * expert + embeddings)
        self.assertEqual(shape.total_params, attention + router + 4 * expert + embeddings)

    def test_sliding_window_layers_are_capped(self):
        # Gemma 4 layout: layer 0 slides over 3 tokens with 2 heads of 4;
        # layer 1 is full attention with 2 heads of 8 (global_head_dim).
        config = {"model_type": "gemma4", "text_config": {
            "model_type": "gemma4_text", "hidden_size": 8, "num_hidden_layers": 2,
            "layer_types": ["sliding_attention", "full_attention"],
            "num_attention_heads": 2, "num_key_value_heads": 1, "head_dim": 4,
            "global_head_dim": 8, "num_global_key_value_heads": 1,
            "attention_k_eq_v": True, "sliding_window": 3, "intermediate_size": 16,
            "enable_moe_block": False, "vocab_size": 10, "tie_word_embeddings": True}}
        shape = model_shape(config)
        sliding, full = shape.layers
        self.assertEqual((sliding.attention_width, sliding.window), (8, 3))
        self.assertEqual((full.attention_width, full.window), (16, None))
        # 7 tokens: the sliding layer sees 1, 2, 3, 3, 3, 3, 3.
        attention = 8 * (1 + 2 + 3 + 3 + 3 + 3 + 3) + 16 * (1 + 2 + 3 + 4 + 5 + 6 + 7)
        self.assertEqual(flops(shape, 4, 3), 2 * shape.active_params * 7 + 2 * attention)

    def test_gemma_moe_block_sits_beside_the_dense_mlp(self):
        base = {"model_type": "gemma4_text", "hidden_size": 8, "num_hidden_layers": 1,
                "layer_types": ["sliding_attention"], "num_attention_heads": 2,
                "num_key_value_heads": 1, "head_dim": 4, "sliding_window": 3,
                "intermediate_size": 16, "vocab_size": 10, "tie_word_embeddings": True}
        dense = model_shape(dict(base, enable_moe_block=False)).active_params
        moe = model_shape(dict(base, enable_moe_block=True, num_experts=4,
                               top_k_experts=2, moe_intermediate_size=4))
        self.assertEqual(moe.active_params, dense + 8 * 4 + 2 * 3 * 8 * 4)
        self.assertEqual(moe.total_params, dense + 8 * 4 + 4 * 3 * 8 * 4)

    def test_linear_attention_layers_add_no_context_term(self):
        config = {"model_type": "qwen3_5_text", "hidden_size": 8, "num_hidden_layers": 2,
                  "layer_types": ["linear_attention", "full_attention"],
                  "num_attention_heads": 2, "num_key_value_heads": 1, "head_dim": 4,
                  "attn_output_gate": True, "intermediate_size": 16,
                  "linear_num_key_heads": 1, "linear_key_head_dim": 4,
                  "linear_num_value_heads": 2, "linear_value_head_dim": 4,
                  "linear_conv_kernel_dim": 4, "vocab_size": 10,
                  "tie_word_embeddings": False}
        shape = model_shape(config)
        self.assertEqual(shape.layers[0].attention_width, 0)
        self.assertEqual(flops(shape, 10, 0) - 2 * shape.active_params * 10,
                         2 * 8 * context_sum(10))

    def test_the_three_lineup_models_land_on_their_published_sizes(self):
        for name, config, published in LINEUP:
            active = model_shape(config).active_params
            self.assertLess(abs(active - published) / published, 0.05,
                            f"{name}: {active:,} vs published {published:,}")


class FlopsNulls(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def test_a_missing_field_is_null_with_the_reason(self):
        config = dict(TINY_LLAMA)
        del config["intermediate_size"]
        out = compute_metrics(write_config(os.path.join(self.dir, "m"), config), 10, 5)
        self.assertIsNone(out["active_params"])
        self.assertIsNone(out["flops_estimate"])
        self.assertIn("not estimated: config.json has no usable intermediate_size",
                      out["flops_method"])

    def test_an_unrecognised_architecture_is_null(self):
        folder = write_config(os.path.join(self.dir, "m"), {"model_type": "gpt_oss"})
        out = compute_metrics(folder, 10, 5)
        self.assertIsNone(out["flops_estimate"])
        self.assertIn("architecture 'gpt_oss' not recognised", out["flops_method"])

    def test_no_local_folder_is_null(self):
        out = compute_metrics(None, 10, 5)
        self.assertIsNone(out["active_params"])
        self.assertIn("no local model folder", out["flops_method"])

    def test_missing_token_counts_keep_the_params_but_not_the_estimate(self):
        folder = write_config(os.path.join(self.dir, "m"), TINY_LLAMA)
        out = compute_metrics(folder, None, 5)
        self.assertEqual(out["active_params"], 1312)
        self.assertIsNone(out["flops_estimate"])
        self.assertIn("did not report both token counts", out["flops_method"])

    def test_a_sliding_window_without_layer_types_is_not_guessed(self):
        with self.assertRaises(Unsupported):
            model_shape(dict(TINY_LLAMA, sliding_window=4096))
        # Qwen 2 lists a window but switches it off; that is fine.
        model_shape(dict(TINY_LLAMA, sliding_window=4096, use_sliding_window=False))

    def test_a_good_estimate_names_the_method_without_a_reason(self):
        folder = write_config(os.path.join(self.dir, "m"), TINY_LLAMA)
        out = compute_metrics(folder, 3, 2)
        self.assertEqual(out["flops_method"], FLOPS_METHOD)
        self.assertIn("4-bit quantisation not counted", FLOPS_METHOD)


class Timings(unittest.TestCase):
    def test_rates_come_from_the_marks(self):
        out = timing_metrics({"first_token": 2.0, "first_answer": 3.0,
                              "last_token": 12.0}, 4000, 81)
        self.assertEqual(out, {"time_to_first_token_ms": 2000,
                               "time_to_first_answer_ms": 3000,
                               "prompt_tokens_per_second": 2000.0,
                               "decode_tokens_per_second": 8.0})

    def test_nothing_measured_is_all_null(self):
        out = run_metrics({"prompt_tokens": 5, "completion_tokens": 1}, {}, None)
        for key in ("time_to_first_token_ms", "time_to_first_answer_ms",
                    "prompt_tokens_per_second", "decode_tokens_per_second",
                    "active_params", "flops_estimate"):
            self.assertIsNone(out[key], key)

    def test_one_token_has_no_decode_rate(self):
        out = timing_metrics({"first_token": 1.0, "last_token": 1.0}, 10, 1)
        self.assertIsNone(out["decode_tokens_per_second"])
        self.assertIsNone(out["time_to_first_answer_ms"])


class RecordedThroughTheService(unittest.TestCase):
    """A real run against the streaming stub, with pauses placed so the
    timings can be checked within a tolerance."""

    TOLERANCE = 0.15   # seconds of scheduling slack on a busy machine

    def setUp(self):
        for name in ("root", "models"):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            setattr(self, name, tmp.name)
        for key, value in (("MITSS_LLM_PREFLIGHT_TIMEOUT", "0"),
                           ("MITSS_MODELS_DIR", self.models)):
            previous = os.environ.get(key)
            os.environ[key] = value
            self.addCleanup(lambda k=key, p=previous: os.environ.pop(k, None)
                            if p is None else os.environ.__setitem__(k, p))
        SCRIPT["attempts"], SCRIPT["status"] = [], 200
        SEEN["count"], SEEN["bodies"] = 0, []
        self.folder = write_config(os.path.join(self.models, "tiny"), TINY_LLAMA)
        self.prompt = service.create_prompt("P", "Say: {input}", root=self.root)["id"]

    def record(self, url):
        model_id = service.register_model("tiny", url=url, model=self.folder,
                                          root=self.root)["id"]
        return service.generate_run(self.prompt, model_id=model_id, root=self.root)

    def test_timings_flops_and_the_old_rate_are_recorded(self):
        # 0.4 s of "prompt processing", thinking for 0.3 s, then the answer.
        SCRIPT["attempts"] = [[
            (0, b": keepalive 1/3\n\n"),
            (0.4, _delta(reasoning="Hmm ")),
            (0.3, _delta(content="The ")),
            (0.2, _delta(content="answer.")),
            (0.3, _delta(content="", finish="stop")),   # empty: not text
            (0, _sse({"choices": [], "usage": {"prompt_tokens": 40,
                                               "completion_tokens": 11,
                                               "total_tokens": 51}})),
            (0, DONE),
        ]]
        with StreamServer() as url:
            run = self.record(url)
        usage = run["usage"]
        self.assertAlmostEqual(usage["time_to_first_token_ms"] / 1000, 0.4,
                               delta=self.TOLERANCE)
        self.assertAlmostEqual(usage["time_to_first_answer_ms"] / 1000, 0.7,
                               delta=self.TOLERANCE)
        # 10 tokens after the first, over the 0.5 s between first and last
        # text; the trailing empty delta is not text.
        self.assertAlmostEqual(usage["decode_tokens_per_second"], 10 / 0.5,
                               delta=6)
        self.assertAlmostEqual(usage["prompt_tokens_per_second"],
                               40 / (usage["time_to_first_token_ms"] / 1000), delta=1)
        self.assertEqual(usage["active_params"], 1312)
        self.assertEqual(usage["flops_estimate"], 2 * 1312 * 51 + 2 * 2 * 8 * context_sum(51))
        self.assertEqual(usage["flops_method"], FLOPS_METHOD)
        # tokens_per_second keeps its meaning: completion tokens over the
        # whole request, about 1.2 s here.
        self.assertLess(usage["tokens_per_second"], usage["decode_tokens_per_second"])
        self.assertAlmostEqual(usage["tokens_per_second"], 11 / 1.2, delta=2)
        transcript = read_transcript(os.path.join(self.root, "data"))
        # A tiny model's estimate is below a megaFLOP, so it reads in FLOPs.
        self.assertRegex(transcript, r"\|  first token 0\.\d s  \|  decode [\d.]+ tok/s"
                                     r"  \|  ~176256 FLOPs  \|  stopped: stop")

    def test_a_remote_endpoint_records_null_flops_with_the_reason(self):
        # The stub is on 127.0.0.1; treating it as remote stands in for a
        # teammate's endpoint, whose model folder this machine cannot see.
        SCRIPT["attempts"] = [[(0, _delta(content="hi")),
                               (0, _delta(content="", finish="stop")),
                               (0, _sse({"choices": [], "usage": {
                                   "prompt_tokens": 3, "completion_tokens": 1}})),
                               (0, DONE)]]
        with StreamServer() as url:
            model_id = service.register_model(
                "remote", url=url, model="anything", root=self.root)["id"]
            with mock.patch("mitss.llm.is_local_url", return_value=False):
                run = service.generate_run(self.prompt, model_id=model_id, root=self.root)
        self.assertIsNone(run["usage"]["flops_estimate"])
        self.assertIn("no local model folder", run["usage"]["flops_method"])
        self.assertIsNotNone(run["usage"]["time_to_first_token_ms"])


# The three lineup models, rebuilt from the config.json fields the count
# reads (~/Desktop/models, 2026-10-01). Published sizes: Llama 3.1 8B 8.0 B;
# Gemma 4 26B-A4B 3.8 B active; Qwen 3.8 27B 27 B dense.
LINEUP = (
    ("llama-3.1-8b", {
        "model_type": "llama", "hidden_size": 4096, "num_hidden_layers": 32,
        "num_attention_heads": 32, "num_key_value_heads": 8,
        "intermediate_size": 14336, "vocab_size": 128256,
        "tie_word_embeddings": False}, 8_000_000_000),
    ("gemma-4-26b-a4b", {"model_type": "gemma4", "tie_word_embeddings": True,
                         "text_config": {
        "model_type": "gemma4_text", "hidden_size": 2816, "num_hidden_layers": 30,
        "layer_types": (["sliding_attention"] * 5 + ["full_attention"]) * 5,
        "num_attention_heads": 16, "num_key_value_heads": 8, "head_dim": 256,
        "global_head_dim": 512, "num_global_key_value_heads": 2,
        "attention_k_eq_v": True, "sliding_window": 1024, "intermediate_size": 2112,
        "enable_moe_block": True, "num_experts": 128, "top_k_experts": 8,
        "moe_intermediate_size": 704, "hidden_size_per_layer_input": 0,
        "num_kv_shared_layers": 0, "use_double_wide_mlp": False,
        "vocab_size": 262144, "tie_word_embeddings": True}}, 3_800_000_000),
    ("qwen3.8-27b", {"model_type": "qwen3_5", "tie_word_embeddings": False,
                     "text_config": {
        "model_type": "qwen3_5_text", "hidden_size": 5120, "num_hidden_layers": 64,
        "layer_types": (["linear_attention"] * 3 + ["full_attention"]) * 16,
        "num_attention_heads": 24, "num_key_value_heads": 4, "head_dim": 256,
        "attn_output_gate": True, "intermediate_size": 17408,
        "linear_num_key_heads": 16, "linear_key_head_dim": 128,
        "linear_num_value_heads": 48, "linear_value_head_dim": 128,
        "linear_conv_kernel_dim": 4, "vocab_size": 248320,
        "tie_word_embeddings": False}}, 27_000_000_000),
)


if __name__ == "__main__":
    unittest.main()
