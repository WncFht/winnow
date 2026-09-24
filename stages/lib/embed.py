#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "numpy",
#   "onnxruntime>=1.17",
#   "tokenizers>=0.19",
# ]
# ///
"""Qwen3-Embedding-0.6B int8 ONNX embedder (last-token pooling, L2-normalized).

Backend: onnx-community/Qwen3-Embedding-0.6B-ONNX int8 on CPU
(~12 texts/s per PLAN.md §3.4; not an LLM, exempt from D1).

Model dir resolution (first hit wins):
    1. $EMBED_MODEL_DIR
    2. ~/.cache/embed/            (canonical cache per PLAN.md §3.4)
Each dir must contain model_int8.onnx + tokenizer.json.

API:
    from lib import embed
    v = embed.embed(["标题1", "标题2"], mode="doc")     # (N,1024) float32, unit norm
    q = embed.embed(["某事件有新进展吗"], mode="query") # instruct prefix applied

mode="query" prepends the Qwen3-Embedding instruction prefix (asymmetric
retrieval convention); mode="doc" embeds the raw text. Rows are always
unit-norm so dot product == cosine similarity.

Smoke:  uv run stages/lib/embed.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

REPO_ROOT = Path(__file__).resolve().parents[2]

# Qwen3-Embedding convention: instruction-tuned query side, bare doc side.
# Task wording calibrated in experiments/dedup-history (same-event retrieval).
INSTRUCT = (
    "Instruct: Given a news headline, retrieve previously reported stories "
    "about the same event\nQuery:"
)
MAX_LEN = 512  # tokens; news titles+summaries fit comfortably
DIM = 1024
_BATCH = 32
_MODEL_NAME = "model_int8.onnx"
_TOK_NAME = "tokenizer.json"

_MODE_ALIASES = {
    "query": "query",
    "instruct": "query",
    "doc": "doc",
    "document": "doc",
    "passage": "doc",
}


def _candidate_dirs() -> list[Path]:
    dirs = []
    env = os.environ.get("EMBED_MODEL_DIR")
    if env:
        dirs.append(Path(env).expanduser())
    dirs.append(Path.home() / ".cache" / "embed")
    return dirs


def resolve_model_dir() -> Path:
    """Return the first dir containing model_int8.onnx + tokenizer.json."""
    for d in _candidate_dirs():
        if (d / _MODEL_NAME).is_file() and (d / _TOK_NAME).is_file():
            return d
    tried = "\n".join(f"  - {d}" for d in _candidate_dirs())
    raise FileNotFoundError(
        "Qwen3-Embedding-0.6B ONNX files not found; tried:\n" + tried
    )


class Embedder:
    """Lazy ONNX session; reuse one instance per process (model is ~600MB)."""

    def __init__(self, model_dir: Path | None = None, max_len: int = MAX_LEN,
                 threads: int | None = None):
        self.model_dir = Path(model_dir) if model_dir else resolve_model_dir()
        self.tok = Tokenizer.from_file(str(self.model_dir / _TOK_NAME))
        self.tok.enable_truncation(max_len)

        opts = ort.SessionOptions()
        n = threads or int(os.environ.get("EMBED_THREADS", "4"))
        opts.intra_op_num_threads = max(1, n)
        opts.inter_op_num_threads = 1
        prov = os.environ.get("EMBED_PROVIDER", "cpu").lower()
        if prov in ("cuda", "gpu"):
            # 需 onnxruntime-gpu（与 onnxruntime 同包名互斥，走独立 venv 提供）。
            # int8 量化模型在 CUDA EP 上部分算子仍回退 CPU，提速以实测为准。
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        elif prov == "auto":
            providers = [p for p in ("CUDAExecutionProvider",
                                     "CPUExecutionProvider")
                         if p in ort.get_available_providers()]
        else:
            providers = ["CPUExecutionProvider"]
        self.sess = ort.InferenceSession(
            str(self.model_dir / _MODEL_NAME),
            sess_options=opts,
            providers=providers,
        )
        self.provider = self.sess.get_providers()[0]
        self.need_mask = any(i.name == "attention_mask" for i in self.sess.get_inputs())

    def _encode(self, texts: list[str]):
        encs = self.tok.encode_batch(texts)
        L = max(len(e.ids) for e in encs)
        ids = np.zeros((len(encs), L), dtype=np.int64)
        mask = np.zeros((len(encs), L), dtype=np.int64)
        for i, e in enumerate(encs):
            ids[i, : len(e.ids)] = e.ids
            mask[i, : len(e.ids)] = e.attention_mask
        return ids, mask

    def _empty_past(self, batch_size: int) -> dict:
        # 28 layers, KV heads=8, head_dim=128; empty cache len=0.
        return {
            i.name: np.zeros((batch_size, 8, 0, 128), dtype=np.float32)
            for i in self.sess.get_inputs()
            if i.name.startswith("past_key_values")
        }

    def embed(self, texts: list[str], mode: str = "doc") -> np.ndarray:
        """texts -> float32 (N,1024), unit-norm rows. mode: 'query'|'doc'."""
        m = _MODE_ALIASES.get(mode)
        if m is None:
            raise ValueError(f"mode must be one of {sorted(_MODE_ALIASES)}, got {mode!r}")
        if isinstance(texts, str):
            texts = [texts]
        if len(texts) == 0:
            return np.zeros((0, DIM), dtype=np.float32)
        if m == "query":
            texts = [f"{INSTRUCT} {t}" for t in texts]

        out = []
        for i in range(0, len(texts), _BATCH):
            ids, mask = self._encode(texts[i : i + _BATCH])
            pos = np.tile(np.arange(ids.shape[1], dtype=np.int64), (len(ids), 1))
            feed = {"input_ids": ids, "position_ids": pos}
            feed.update(self._empty_past(len(ids)))
            if self.need_mask:
                feed["attention_mask"] = mask
            hs = self.sess.run(None, feed)[0]  # (B, T, H) last_hidden_state
            idx = mask.sum(1) - 1  # last non-pad token per row
            vec = hs[np.arange(len(ids)), idx]
            vec = vec / np.linalg.norm(vec, axis=1, keepdims=True)
            out.append(vec.astype(np.float32))
        return np.vstack(out)


_default: Embedder | None = None


def _default_embedder() -> Embedder:
    global _default
    if _default is None:
        _default = Embedder()
    return _default


def embed(texts, mode: str = "doc") -> np.ndarray:
    """Convenience wrapper on a process-wide singleton.

    embed(["a","b"])             -> docs, (2,1024)
    embed(["a"], mode="query")   -> instructed query vector
    """
    return _default_embedder().embed(list(texts), mode=mode)


if __name__ == "__main__":
    import time

    t0 = time.time()
    q = embed(["OpenAI 发布 GPT-6"], mode="query")
    d = embed(
        ["OpenAI launches GPT-6", "小米发布 NaviX Ultra 手机"],
        mode="doc",
    )
    print("model_dir:", resolve_model_dir())
    print("shape:", q.shape, d.shape, "dtype:", q.dtype)
    print("row norms:", np.linalg.norm(q, axis=1), np.linalg.norm(d, axis=1))
    print("cos same-story cross-lingual:", float((q @ d[0])[0]))
    print("cos unrelated:", float((q @ d[1])[0]))
    print(f"elapsed {time.time() - t0:.1f}s")
