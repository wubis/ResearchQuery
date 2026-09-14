from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from research_query.documents.builder import build_hopkins_documents
from research_query.ingestion.faculty.base import FacultySource
from research_query.ingestion.faculty.whiting import WhitingFacultySource, parse_directory_page, parse_profile_page

FIXTURES = Path(__file__).parent / "fixtures" / "whiting"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_whiting_satisfies_contract_and_parses_unicode_multi_affiliation(fixture_transport: type) -> None:
    base = "https://engineering.jhu.edu/faculty"
    transport = fixture_transport(
        {
            base: _text("directory-1.html"),
            f"{base}?page=2": _text("directory-2.html"),
            "https://engineering.jhu.edu/faculty/jose-alvarez": _text("profile-jose.html"),
            "https://engineering.jhu.edu/faculty/alex-kim": _text("profile-alex.html"),
            "https://engineering.jhu.edu/faculty/sam-postdoc": _text("profile-postdoc.html"),
        }
    )
    source = WhitingFacultySource(base, transport, now=lambda: datetime(2026, 9, 13, tzinfo=UTC))
    assert isinstance(source, FacultySource)
    snapshot = source.fetch_faculty()
    assert snapshot.source_name == "whiting"
    assert snapshot.membership_complete is False
    # The malformed Pat Lee membership card is retained as a completeness error, never hidden.
    assert any("missing profile link" in error for error in snapshot.errors)
    assert len(snapshot.records) == 3
    jose = snapshot.records[0]
    assert jose.name == "José Álvarez"
    assert jose.source_faculty_key == "wse-001"
    assert len(jose.affiliations) == 2
    assert jose.lab_url == "https://labs.jhu.edu/alvarez"
    assert jose.source_payload["lab_description"] == ("The Alvarez Lab designs data-driven molecular simulations.")
    documents = build_hopkins_documents(UUID(int=1), UUID(int=2), jose)
    assert {document.document_type.value for document in documents} == {
        "faculty_research",
        "faculty_bio",
        "lab_description",
    }
    assert jose.source_payload["profile_parse_version"] == "whiting-html-v2"


def test_live_directory_markup_parses_cards_and_reports_unlinked_member() -> None:
    base = "https://engineering.jhu.edu/faculty/"
    records, next_url, errors = parse_directory_page(_text("live-directory-1.html"), base)
    assert len(records) == 9  # Ten observed cards; one lacks a profile link.
    soumyadipta = next(record for record in records if record.name == "Soumyadipta Acharya")
    assert soumyadipta.title == "Assistant Professor and Director of Bioengineering Innovation & Design"
    assert soumyadipta.profile_url == "https://engineering.jhu.edu/faculty/soumyadipta-acharya"
    assert next_url == "https://engineering.jhu.edu/faculty?current_page=2"
    assert errors == ("membership card missing profile link or name: Amesh Adalja",)


def test_live_terminal_page_and_profile_markup() -> None:
    base = "https://engineering.jhu.edu/faculty/?current_page=2"
    records, next_url, errors = parse_directory_page(_text("live-directory-last.html"), base)
    assert len(records) == 2
    assert next_url is None
    assert errors == ()

    directory, _, _ = parse_directory_page(_text("live-directory-1.html"), base)
    soumyadipta = next(record for record in directory if record.name == "Soumyadipta Acharya")
    profile = parse_profile_page(_text("live-profile.html"), soumyadipta)
    assert profile.name == "Soumyadipta Acharya"
    assert profile.title == "Assistant Professor and Director of Bioengineering Innovation & Design"
    assert profile.affiliations[0].department == "Department of Biomedical Engineering"
    assert profile.biography == "He leads biomedical design research at Hopkins."


def test_directory_parse_error_for_zero_cards_marks_completeness_unknown() -> None:
    records, next_url, errors = parse_directory_page("<html><body>changed</body></html>", "https://example.edu/faculty")
    assert records == ()
    assert next_url is None
    assert errors == ("no faculty membership cards found",)


def test_optional_profile_failure_keeps_membership_record(fixture_transport: type) -> None:
    base = "https://engineering.jhu.edu/faculty"
    directory = """
    <main data-membership-complete='true'>
    <article class='faculty-card' data-faculty-id='1' data-role='Faculty'>
      <a href='/faculty/a'><span class='faculty-name'>A Person</span></a>
      <span class='title'>Assistant Professor</span>
    </article>
    </main>
    """
    source = WhitingFacultySource(
        base,
        fixture_transport({base: directory, "https://engineering.jhu.edu/faculty/a": OSError("timeout")}),
    )
    snapshot = source.fetch_faculty()
    assert snapshot.membership_complete is True
    assert len(snapshot.records) == 1
    assert snapshot.records[0].name == "A Person"
    assert snapshot.errors and "profile enrichment failed" in snapshot.errors[0]


def test_duplicate_source_key_merges_directory_affiliations(fixture_transport: type) -> None:
    base = "https://engineering.jhu.edu/faculty"
    directory = """
    <main data-membership-complete='true'>
    <article class='faculty-card' data-faculty-id='same' data-role='Faculty'>
      <a href='/faculty/a'><span class='faculty-name'>A Person</span></a>
      <span class='department'>Computer Science</span>
    </article>
    <article class='faculty-card' data-faculty-id='same' data-role='Faculty'>
      <a href='/faculty/a'><span class='faculty-name'>A Person</span></a>
      <span class='department'>Applied Mathematics</span>
    </article>
    </main>
    """
    profile = "<h1>A Person</h1><p class='research'>Optimization.</p>"
    source = WhitingFacultySource(
        base,
        fixture_transport({base: directory, "https://engineering.jhu.edu/faculty/a": profile}),
    )
    snapshot = source.fetch_faculty()
    assert snapshot.membership_complete is True
    assert len(snapshot.records) == 1
    assert {item.department for item in snapshot.records[0].affiliations} == {"Computer Science", "Applied Mathematics"}
    assert snapshot.records[0].source_payload["duplicate_listing_count"] == 2
