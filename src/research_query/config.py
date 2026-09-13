"""Typed, startup-validated application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, Self, cast, get_type_hints

PINNED_BGE_REVISION = "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a"


class ConfigurationError(ValueError):
    """Raised when environment configuration is unsafe or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = "postgresql://researchquery:researchquery@localhost:5432/researchquery"
    whiting_directory_url: str = "https://engineering.jhu.edu/faculty/"
    enabled_faculty_sources: tuple[str, ...] = ("whiting",)
    faculty_eligibility_policy_version: str = "whiting-v1"
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_model_revision: str = PINNED_BGE_REVISION
    document_formatter_version: str = "v1"
    embedding_batch_size: int = 16
    max_publications_per_faculty: int = 15
    publication_candidate_limit: int = 50
    publication_lookback_years: int = 7
    publication_missing_grace_runs: int = 1
    author_resolve_threshold: float = 0.80
    author_ambiguous_threshold: float = 0.60
    author_resolve_margin: float = 0.15
    semantic_scholar_api_key: str | None = None
    http_user_agent: str = "ResearchQuery/0.1 (contact: replace-with-project-email@example.edu)"
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 3

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environ is None else environ
        hints = get_type_hints(cls)
        values: dict[str, object] = {}
        for item in fields(cls):
            key = item.name.upper()
            if key not in source:
                continue
            raw = source[key].strip()
            type_hint = hints[item.name]
            if item.name == "enabled_faculty_sources":
                values[item.name] = tuple(part.strip() for part in raw.split(",") if part.strip())
            elif type_hint is int:
                values[item.name] = int(raw)
            elif type_hint is float:
                values[item.name] = float(raw)
            elif item.name == "semantic_scholar_api_key":
                values[item.name] = raw or None
            else:
                values[item.name] = raw
        result = cls(**cast(Any, values))
        result.validate()
        return result

    @property
    def embedding_version(self) -> str:
        return (
            f"{self.embedding_model}@{self.embedding_model_revision}:"
            f"formatter={self.document_formatter_version}:normalize=l2:maxseq=512"
        )

    def validate(self) -> None:
        positive_ints = {
            "embedding_batch_size": self.embedding_batch_size,
            "max_publications_per_faculty": self.max_publications_per_faculty,
            "publication_candidate_limit": self.publication_candidate_limit,
            "publication_lookback_years": self.publication_lookback_years,
        }
        for name, value in positive_ints.items():
            if value <= 0:
                raise ConfigurationError(f"{name} must be positive")
        if self.publication_missing_grace_runs < 0 or self.http_max_retries < 0:
            raise ConfigurationError("grace runs and retry counts cannot be negative")
        if self.http_timeout_seconds <= 0:
            raise ConfigurationError("http_timeout_seconds must be positive")
        if not self.enabled_faculty_sources:
            raise ConfigurationError("at least one faculty source must be enabled")
        if self.author_ambiguous_threshold > self.author_resolve_threshold:
            raise ConfigurationError("ambiguous threshold cannot exceed resolve threshold")
        if not 0 <= self.author_ambiguous_threshold <= 1 or not 0 <= self.author_resolve_threshold <= 1:
            raise ConfigurationError("author thresholds must be in [0, 1]")
        if not 0 <= self.author_resolve_margin <= 1:
            raise ConfigurationError("author resolve margin must be in [0, 1]")
        if self.max_publications_per_faculty > self.publication_candidate_limit:
            raise ConfigurationError("maximum selected publications cannot exceed candidate limit")
        if not self.embedding_model_revision or self.embedding_model_revision in {"main", "latest"}:
            raise ConfigurationError("embedding model revision must be an immutable revision")
        if not self.whiting_directory_url.startswith("https://"):
            raise ConfigurationError("whiting_directory_url must use HTTPS")
        if "replace-with-project-email" in self.http_user_agent:
            # Safe for fixture tests, but live clients explicitly reject this placeholder.
            return
