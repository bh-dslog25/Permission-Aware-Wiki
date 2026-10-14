"""Embedding adapters for online retrieval.

The production path uses BGE-M3 when the optional ML dependency is installed.
For MVP/demo environments without the model, the hash embedder keeps the API
usable with data ingested via ``python -m app.ingest.ingest run --fake``.
"""
from __future__ import annotations

import hashlib
import math
import os
import random
from typing import Sequence

from app.config import get_settings


class HashEmbedder:
    name = "fake-hash-embedder"

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or get_settings().embedding_dim

    def embed_query(self, text: str) -> Sequence[float]:
        rng = random.Random(int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big"))
        values = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
        norm = math.sqrt(sum(x * x for x in values)) or 1.0
        return [x / norm for x in values]


class BGEM3Embedder:
    """BGE-M3 query embedder with a lazy model load."""

    def __init__(self, model_name: str | None = None, dim: int | None = None, device: str | None = None) -> None:
        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self.dim = dim or settings.embedding_dim
        self.device = device if device is not None else settings.embedding_device
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(self.model_name, device=self.device)
            model.max_seq_length = 1024
            actual = model.get_sentence_embedding_dimension()
            if actual != self.dim:
                raise RuntimeError(f"Embedding model returned {actual} dimensions, expected {self.dim}")
            self._model = model
        return self._model

    def embed_query(self, text: str) -> Sequence[float]:
        vec = self._load().encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
        return vec.tolist()


def build_embedder() -> HashEmbedder | BGEM3Embedder:
    settings = get_settings()
    if os.environ.get("EMBEDDING_BACKEND", "").lower() == "fake":
        return HashEmbedder(settings.embedding_dim)
    return BGEM3Embedder(settings.embedding_model, settings.embedding_dim, settings.embedding_device)
