from __future__ import annotations

from uuid import UUID, uuid4

from research_query.data.models import PublicationRecord, RawAffiliation, RawFacultyRecord
from research_query.documents.builder import (
    WhitespaceTokenCounter,
    build_hopkins_documents,
    build_publication_document,
    chunk_body,
)


def faculty_record(summary: str = "Machine learning for materials.") -> RawFacultyRecord:
    return RawFacultyRecord(
        "whiting",
        "faculty-1",
        "https://engineering.jhu.edu/faculty",
        "Jane Doe",
        "Professor",
        (RawAffiliation("Whiting School of Engineering", "Materials Science"),),
        "https://engineering.jhu.edu/faculty/jane",
        research_summary=summary,
        biography="Jane studies materials.",
    )


def test_hopkins_documents_have_stable_ids_ownership_and_provenance() -> None:
    faculty_id = UUID(int=1)
    source_record_id = UUID(int=2)
    first = build_hopkins_documents(faculty_id, source_record_id, faculty_record())
    second = build_hopkins_documents(faculty_id, source_record_id, faculty_record("Changed copy"))
    assert len(first) == 2
    assert first[0].document_id == second[0].document_id
    assert first[0].content_hash != second[0].content_hash
    assert all(item.faculty_id == faculty_id for item in first)
    assert all(item.source_url.startswith("https://engineering.jhu.edu") for item in first)
    assert first[0].metadata["source_record_id"] == str(source_record_id)


def test_chunker_removes_boilerplate_obeys_hard_limit_and_is_stable() -> None:
    body = "Skip to content\n\n" + "\n\n".join(
        f"Paragraph {index} has several useful scientific words about materials modeling." for index in range(20)
    )
    chunks = chunk_body(
        "Research",
        body,
        counter=WhitespaceTokenCounter(),
        hard_formatted_limit=35,
        target_body_limit=25,
        overlap_tokens=3,
    )
    assert len(chunks) > 1
    assert chunks == chunk_body(
        "Research",
        body,
        counter=WhitespaceTokenCounter(),
        hard_formatted_limit=35,
        target_body_limit=25,
        overlap_tokens=3,
    )
    assert all("Skip to content" not in text for _, text in chunks)
    assert all(len(f"Title: Research\nText: {text}".split()) <= 35 for _, text in chunks)


def test_publication_identity_prefers_doi_and_title_only_is_allowed() -> None:
    document = build_publication_document(
        uuid4(),
        PublicationRecord(
            "semantic_scholar",
            "paper-1",
            "Useful Title Only Paper",
            None,
            2026,
            None,
            None,
            "10.1/ABC",
            ("Jane Doe",),
            {},
        ),
    )
    assert document.external_id == "doi:10.1/abc"
    assert document.text == ""
    assert document.source_url == "https://www.semanticscholar.org/paper/paper-1"
    assert document.metadata["authors"] == ["Jane Doe"]
