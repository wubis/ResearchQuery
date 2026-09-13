"""Provider-neutral publication source contract."""

from typing import Protocol, runtime_checkable

from research_query.data.models import (
    AuthorCandidateBatch,
    FacultyForResolution,
    PublicationBatch,
)


@runtime_checkable
class PublicationSource(Protocol):
    source_name: str

    def find_authors(self, faculty: FacultyForResolution) -> AuthorCandidateBatch: ...

    def get_publications(self, author_id: str, *, candidate_limit: int, start_year: int | None) -> PublicationBatch: ...
