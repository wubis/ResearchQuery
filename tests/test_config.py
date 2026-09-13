from __future__ import annotations

import pytest

from research_query.config import PINNED_BGE_REVISION, ConfigurationError, Settings


def test_settings_are_typed_and_compute_version() -> None:
    settings = Settings.from_env(
        {
            "ENABLED_FACULTY_SOURCES": "whiting",
            "EMBEDDING_BATCH_SIZE": "8",
            "AUTHOR_RESOLVE_THRESHOLD": "0.9",
            "SEMANTIC_SCHOLAR_API_KEY": "",
        }
    )
    assert settings.enabled_faculty_sources == ("whiting",)
    assert settings.embedding_batch_size == 8
    assert settings.author_resolve_threshold == 0.9
    assert settings.semantic_scholar_api_key is None
    assert PINNED_BGE_REVISION in settings.embedding_version


def test_settings_reject_moving_model_revision_and_bad_thresholds() -> None:
    with pytest.raises(ConfigurationError):
        Settings(embedding_model_revision="main").validate()
    with pytest.raises(ConfigurationError):
        Settings(author_ambiguous_threshold=0.9, author_resolve_threshold=0.8).validate()
