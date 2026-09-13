from __future__ import annotations

from datetime import date

from research_query.data.models import PublicationRecord
from research_query.ingestion.publications.policy import (
    deduplicate_publications,
    normalize_doi,
    select_recent_publications,
)


def paper(
    provider: str,
    external_id: str,
    title: str,
    year: int | None,
    *,
    doi: str | None = None,
    abstract: str | None = "abstract",
    publication_date: str | None = None,
) -> PublicationRecord:
    return PublicationRecord(
        provider,
        external_id,
        title,
        abstract,
        year,
        publication_date,
        None,
        doi,
        ("A. Author",),
        {},
    )


def test_deduplicates_doi_provider_and_title_year_preserving_aliases() -> None:
    records = [
        paper("semantic_scholar", "a", "A Study", 2025, doi="https://doi.org/10.1/X"),
        paper("openalex", "b", "A Study", 2025, doi="doi:10.1/x", abstract=None),
        paper("semantic_scholar", "a", "A Study revised", 2025),
    ]
    deduped = deduplicate_publications(records)
    assert len(deduped) == 1
    assert normalize_doi(deduped[0].doi) == "10.1/x"
    assert deduped[0].metadata["provider_ids"] == {
        "openalex": ["b"],
        "semantic_scholar": ["a"],
    }
    assert "doi:10.1/x" in deduped[0].metadata["identity_aliases"]


def test_selection_uses_seven_calendar_years_abstract_first_and_stable_order() -> None:
    records = [
        paper("semantic_scholar", "old", "Old Paper", 2019),
        paper("semantic_scholar", "null", "No Year Paper", None),
        paper(
            "semantic_scholar", "new-title", "Substantive New Title", 2026, abstract=None, publication_date="2026-09-01"
        ),
        paper("semantic_scholar", "abs-a", "Abstract A", 2024, publication_date="2024-02-01"),
        paper("semantic_scholar", "abs-b", "Abstract B", 2025, publication_date="2025-02-01"),
    ]
    selected = select_recent_publications(records, cutoff_date=date(2026, 9, 13), lookback_years=7, maximum=3)
    assert [item.external_id for item in selected] == ["abs-b", "abs-a", "new-title"]
