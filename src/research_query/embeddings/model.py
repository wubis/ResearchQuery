"""Pinned local BGE model and incremental candidate-index builder."""

from __future__ import annotations

import hashlib
import importlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from research_query.data.models import EmbeddingInput, ResearchDocument

QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


class EmbeddingModel(Protocol):
    model_id: str
    revision: str
    dimension: int

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, documents: list[str]) -> list[list[float]]: ...

    def count_tokens(self, text: str) -> int: ...


class EmbeddingStore(Protocol):
    def missing_embedding_inputs(self, embedding_version: str) -> Sequence[EmbeddingInput]: ...

    def upsert_embedding(
        self,
        document_id: UUID,
        embedding_version: str,
        vector: Sequence[float],
        document_content_hash: str,
        embedding_input_hash: str,
    ) -> None: ...

    def activate_embedding_version(self, embedding_version: str, corpus_snapshot_id: UUID) -> None: ...

    def mark_embedding_truncated(self, document_id: UUID, truncated: bool) -> None: ...


def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("embedding vector must have a finite non-zero norm")
    return [float(value / norm) for value in vector]


def format_document(document: ResearchDocument) -> str:
    return f"Title: {document.title}\nText: {document.text}"


def embedding_input_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class LocalEmbeddingModel:
    dimension = 768

    def __init__(
        self,
        model_id: str,
        revision: str,
        *,
        device: str = "cpu",
        model: object | None = None,
    ) -> None:
        if len(revision) < 12 or revision in {"main", "latest"}:
            raise ValueError("an immutable model revision is required")
        self.model_id = model_id
        self.revision = revision
        if model is None:
            try:
                module = importlib.import_module("sentence_transformers")
            except ModuleNotFoundError as exc:  # pragma: no cover - environment-specific
                raise RuntimeError("install sentence-transformers to load local embeddings") from exc
            SentenceTransformer = module.SentenceTransformer
            model = SentenceTransformer(model_id, revision=revision, device=device)
        self._model = cast(Any, model)
        embedding_dimension = self._model.get_sentence_embedding_dimension()
        if embedding_dimension != self.dimension:
            raise ValueError(f"expected 768-dimensional model, got {embedding_dimension}")
        self._model.max_seq_length = 512

    @property
    def version(self) -> str:
        return f"{self.model_id}@{self.revision}:formatter=v1:normalize=l2:maxseq=512"

    def count_tokens(self, text: str) -> int:
        tokenizer = self._model.tokenizer
        result = tokenizer.encode(text, add_special_tokens=True, truncation=False)
        return len(cast(list[int], result))

    def _encode(self, texts: list[str]) -> list[list[float]]:
        result = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        rows = result.tolist()
        vectors = [l2_normalize(row) for row in rows]
        if any(len(vector) != self.dimension for vector in vectors):
            raise ValueError("embedding model returned an unexpected dimension")
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._encode([f"{QUERY_INSTRUCTION}{text}"])[0]

    def embed_documents(self, documents: list[str]) -> list[list[float]]:
        return self._encode(documents)


class PinnedTokenizerCounter:
    """Exact pinned tokenizer boundary for document construction without loading model weights."""

    def __init__(self, model_id: str, revision: str, *, tokenizer: object | None = None) -> None:
        if len(revision) < 12 or revision in {"main", "latest"}:
            raise ValueError("an immutable tokenizer revision is required")
        if tokenizer is None:
            module = importlib.import_module("transformers")
            tokenizer = module.AutoTokenizer.from_pretrained(model_id, revision=revision)
        self._tokenizer = cast(Any, tokenizer)

    def count_tokens(self, text: str) -> int:
        return len(self._tokenizer.encode(text, add_special_tokens=True, truncation=False))


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def fit_embedding_input(text: str, model: EmbeddingModel, *, maximum_tokens: int = 512) -> tuple[str, bool]:
    """Fit at a sentence boundary, falling back to tokenizer-safe word bisection."""
    if model.count_tokens(text) <= maximum_tokens:
        return text, False
    sentences = _SENTENCE_BOUNDARY.split(text)
    while len(sentences) > 1 and model.count_tokens(" ".join(sentences)) > maximum_tokens:
        sentences.pop()
    candidate = " ".join(sentences).strip()
    if model.count_tokens(candidate) <= maximum_tokens:
        return candidate, True
    words = candidate.split()
    low, high = 0, len(words)
    while low < high:
        middle = (low + high + 1) // 2
        if model.count_tokens(" ".join(words[:middle])) <= maximum_tokens:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        raise ValueError("embedding title/markers alone exceed the model token limit")
    return " ".join(words[:low]), True


@dataclass(frozen=True, slots=True)
class EmbeddingBuildResult:
    embedding_version: str
    embedded_count: int
    activated: bool


class EmbeddingIndexer:
    def __init__(self, store: EmbeddingStore, model: EmbeddingModel, *, batch_size: int = 16) -> None:
        self.store = store
        self.model = model
        self.batch_size = batch_size

    def build_and_activate(self, embedding_version: str, corpus_snapshot_id: UUID) -> EmbeddingBuildResult:
        pending = list(self.store.missing_embedding_inputs(embedding_version))
        embedded = 0
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start : start + self.batch_size]
            fitted = [fit_embedding_input(item.text, self.model) for item in batch]
            vectors = self.model.embed_documents([text for text, _truncated in fitted])
            if len(vectors) != len(batch):
                raise ValueError("embedding model returned the wrong batch length")
            for item, vector, (formatted, truncated) in zip(batch, vectors, fitted, strict=True):
                if len(vector) != self.model.dimension:
                    raise ValueError("embedding dimension mismatch")
                self.store.upsert_embedding(
                    item.document_id,
                    embedding_version,
                    l2_normalize(vector),
                    item.content_hash,
                    embedding_input_hash(formatted),
                )
                self.store.mark_embedding_truncated(item.document_id, truncated)
                embedded += 1
        # The store rechecks completeness and content hashes in the activation transaction.
        self.store.activate_embedding_version(embedding_version, corpus_snapshot_id)
        return EmbeddingBuildResult(embedding_version, embedded, True)
