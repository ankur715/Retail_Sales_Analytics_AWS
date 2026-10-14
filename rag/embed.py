"""Embedders. Both produce 384-dimension, L2-normalised vectors, so the index layout is the same;
the index records which embedder built it and refuses queries from a different one.

  hash       feature hashing of word unigrams + bigrams. Deterministic and dependency-free, so
             unit tests and CI exercise the real pgvector path. Matches words, not meaning.
  fastembed  BAAI/bge-small-en-v1.5 via fastembed (ONNX Runtime on CPU, ~130 MB model, no torch).
             Installed in the Codespace and the chatbot image only (requirements-rag.txt): it's
             too heavy for the laptop's venv. Queries use bge's retrieval instruction.
"""
import hashlib
import math
import re
from functools import lru_cache

DIM = 384


class HashEmbedder:
    name = "hash"
    dim = DIM

    @staticmethod
    def _vector(text: str) -> list[float]:
        words = re.findall(r"[a-z0-9]+", text.lower())
        features = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
        v = [0.0] * DIM
        for f in features:
            h = int(hashlib.sha1(f.encode()).hexdigest()[:8], 16)
            v[h % DIM] += 1.0 if (h >> 31) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def query(self, text: str) -> list[float]:
        return self._vector(text)


class FastEmbedder:
    name = "fastembed"
    dim = DIM
    MODEL = "BAAI/bge-small-en-v1.5"

    def __init__(self):
        from fastembed import TextEmbedding   # imported here: optional dependency
        self.model = TextEmbedding(self.MODEL)

    def documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.passage_embed(texts)]

    def query(self, text: str) -> list[float]:
        return next(iter(self.model.query_embed([text]))).tolist()


@lru_cache(maxsize=2)
def get(name: str):
    if name == "hash":
        return HashEmbedder()
    if name == "fastembed":
        return FastEmbedder()
    raise ValueError(f"RAG_EMBEDDER must be hash or fastembed, got {name!r}")
