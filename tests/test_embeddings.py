from __future__ import annotations

import math
from uuid import UUID

import pytest

from research_query.data.models import EmbeddingInput
from research_query.embeddings.model import (
    QUERY_INSTRUCTION,
    EmbeddingIndexer,
    LocalEmbeddingModel,
    PinnedTokenizerCounter,
    fit_embedding_input,
    l2_normalize,
)


class FakeModel:
    model_id = "fake"
    revision = "0123456789ab"
    dimension = 3

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    def embed_documents(self, documents: list[str]) -> list[list[float]]:
        return [[3.0, 4.0, 0.0] for _ in documents]

    def count_tokens(self, text: str) -> int:
        return len(text.split())


class FakeStore:
    def __init__(self) -> None:
        self.pending = [EmbeddingInput(UUID(int=1), "content", "formatted", "input")]
        self.rows: list[tuple[object, ...]] = []
        self.activated: tuple[str, UUID] | None = None
        self.truncated: dict[UUID, bool] = {}

    def missing_embedding_inputs(self, embedding_version: str):
        return self.pending

    def upsert_embedding(self, *values: object) -> None:
        self.rows.append(values)

    def activate_embedding_version(self, embedding_version: str, corpus_snapshot_id: UUID) -> None:
        self.activated = (embedding_version, corpus_snapshot_id)

    def mark_embedding_truncated(self, document_id: UUID, truncated: bool) -> None:
        self.truncated[document_id] = truncated


def test_incremental_index_normalizes_then_activates_candidate_version() -> None:
    store = FakeStore()
    result = EmbeddingIndexer(store, FakeModel(), batch_size=1).build_and_activate("v1", UUID(int=2))
    assert result.embedded_count == 1 and result.activated
    vector = store.rows[0][2]
    assert vector == pytest.approx([0.6, 0.8, 0.0])
    assert store.activated == ("v1", UUID(int=2))
    assert store.truncated == {UUID(int=1): False}


def test_l2_normalization_rejects_zero_and_has_unit_norm() -> None:
    normalized = l2_normalize([2.0, 2.0])
    assert math.sqrt(sum(item * item for item in normalized)) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        l2_normalize([0.0, 0.0])


def test_embedding_input_truncates_at_last_sentence_boundary() -> None:
    text, truncated = fit_embedding_input(
        "Title: Work. First useful sentence. Second sentence is too long.",
        FakeModel(),
        maximum_tokens=7,
    )
    assert truncated is True
    assert text == "Title: Work. First useful sentence."


class FakeArray:
    def __init__(self, rows: list[list[float]]) -> None:
        self.rows = rows

    def tolist(self) -> list[list[float]]:
        return self.rows


class FakeTokenizer:
    def encode(self, text: str, **kwargs: object) -> list[int]:
        return list(range(len(text.split()) + 2))


class FakeSentenceTransformer:
    def __init__(self) -> None:
        self.tokenizer = FakeTokenizer()
        self.max_seq_length = 0
        self.seen: list[str] = []

    def get_sentence_embedding_dimension(self) -> int:
        return 768

    def encode(self, texts: list[str], **kwargs: object) -> FakeArray:
        self.seen.extend(texts)
        return FakeArray([[1.0, *([0.0] * 767)] for _ in texts])


def test_local_model_boundary_formats_queries_only_and_pins_sequence_length() -> None:
    backend = FakeSentenceTransformer()
    model = LocalEmbeddingModel("BAAI/bge-base-en-v1.5", "0123456789abcdef", model=backend)
    assert model.embed_query("materials") == [1.0, *([0.0] * 767)]
    assert backend.seen[-1] == f"{QUERY_INSTRUCTION}materials"
    model.embed_documents(["Title: Work\nText: body"])
    assert backend.seen[-1] == "Title: Work\nText: body"
    assert backend.max_seq_length == 512
    assert model.count_tokens("one two") == 4


def test_pinned_tokenizer_counter_uses_injected_exact_tokenizer() -> None:
    counter = PinnedTokenizerCounter("model", "0123456789abcdef", tokenizer=FakeTokenizer())
    assert counter.count_tokens("one two three") == 5
