from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

from research_query.documents.builder import build_hopkins_documents
from research_query.ingestion.faculty.base import FacultySource
from research_query.ingestion.faculty.whiting import (
    WhitingFacultySource,
    _names_match_with_optional_initial,
    parse_directory_page,
    parse_profile_page,
)
from research_query.ingestion.http import HttpResponse

FIXTURES = Path(__file__).parent / "fixtures" / "whiting"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_unlinked_name_match_allows_only_one_omitted_middle_initial() -> None:
    assert _names_match_with_optional_initial("Balázs Vágvölgyi", "Balázs P. Vágvölgyi")
    assert not _names_match_with_optional_initial("Balázs Vágvölgyi", "Balázs Paul Vágvölgyi")
    assert not _names_match_with_optional_initial("Balázs Vágvölgyi", "Balázs P. Smith")
    assert not _names_match_with_optional_initial("Balázs P. Vágvölgyi", "Balázs Q. Vágvölgyi")


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
    assert any("no profile link" in error for error in snapshot.errors)
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


def test_live_directory_markup_preserves_unlinked_member_for_verified_lookup() -> None:
    base = "https://engineering.jhu.edu/faculty/"
    records, next_url, errors = parse_directory_page(_text("live-directory-1.html"), base)
    assert len(records) == 10
    soumyadipta = next(record for record in records if record.name == "Soumyadipta Acharya")
    assert soumyadipta.title == "Assistant Professor and Director of Bioengineering Innovation & Design"
    assert soumyadipta.profile_url == "https://engineering.jhu.edu/faculty/soumyadipta-acharya"
    amesh = next(record for record in records if record.name == "Amesh Adalja")
    assert amesh.key is None
    assert amesh.profile_url is None  # The department and email links are not person profiles.
    assert amesh.department == "Department of Environmental Health and Engineering"
    assert next_url == "https://engineering.jhu.edu/faculty?current_page=2"
    assert errors == ()


def test_unlinked_member_uses_unique_public_hopkins_people_record(fixture_transport: type) -> None:
    base = "https://engineering.jhu.edu/faculty"
    directory = """
    <main data-membership-complete='true'><div class='entity'>
      <h2 class='entity_name'>Amesh Adalja</h2><div class='entity_title'>Assistant Professor</div>
      <a href='mailto:aadalja1@jhu.edu'>Email</a>
    </div></main>
    """
    query = urlencode({"search": "Amesh Adalja", "per_page": 100, "_fields": "id,link,title"})
    profile_url = f"{base}/amesh-adalja"
    transport = fixture_transport(
        {
            base: directory,
            f"https://engineering.jhu.edu/wp-json/wp/v2/people?{query}": [
                {"id": 10442, "link": f"{profile_url}/", "title": {"rendered": "Amesh Adalja"}}
            ],
            profile_url: "<h1 class='page_title'>Amesh Adalja</h1>",
        }
    )
    snapshot = WhitingFacultySource(base, transport).fetch_faculty()
    assert snapshot.membership_complete is True
    assert len(snapshot.records) == 1
    assert snapshot.records[0].source_faculty_key == profile_url
    assert snapshot.records[0].source_payload["wp_person_id"] == 10442


def test_ambiguous_unlinked_member_keeps_snapshot_incomplete(fixture_transport: type) -> None:
    base = "https://engineering.jhu.edu/faculty"
    directory = (
        "<main data-membership-complete='true'><div class='entity'>"
        "<h2 class='entity_name'>A Person</h2></div></main>"
    )
    query = urlencode({"search": "A Person", "per_page": 100, "_fields": "id,link,title"})
    source = WhitingFacultySource(
        base,
        fixture_transport(
            {
                base: directory,
                f"https://engineering.jhu.edu/wp-json/wp/v2/people?{query}": [
                    {"id": index, "link": f"{base}/a-person-{index}", "title": {"rendered": "A Person"}}
                    for index in (1, 2)
                ],
            }
        ),
    )
    snapshot = source.fetch_faculty()
    assert snapshot.membership_complete is False
    assert snapshot.records == ()
    assert "expected one exact Hopkins people match" in snapshot.errors[0]


def test_public_people_feed_reconciles_linked_and_unlinked_directory_members(fixture_transport: type) -> None:
    base = "https://engineering.jhu.edu/faculty"
    directory = """
    <main data-membership-complete='true'>
      <article class='faculty-card' data-role='Faculty'>
        <a href='/faculty/alex-kim'><span class='faculty-name'>Alex Kim</span></a>
        <span class='title'>Professor</span>
      </article>
      <article class='faculty-card' data-role='Faculty'>
        <span class='faculty-name'>Pat Lee</span><span class='title'>Assistant Professor</span>
      </article>
    </main>
    """
    query = urlencode({"per_page": 100, "page": 1, "_fields": "id,link,title,content"})
    feed_url = f"https://engineering.jhu.edu/wp-json/wp/v2/people?{query}"
    payload = (
        '[{"id":1,"link":"https://engineering.jhu.edu/faculty/alex-kim/",'
        '"title":{"rendered":"Alex Kim"},"content":{"rendered":"<p>Studies robotics.</p>"}},'
        '{"id":2,"link":"https://engineering.jhu.edu/faculty/pat-lee/",'
        '"title":{"rendered":"Pat M. Lee"},"content":{"rendered":"<p>Studies systems.</p>"}}]'
    )
    transport = fixture_transport(
        {
            base: directory,
            feed_url: HttpResponse(200, payload.encode(), {"x-wp-total": "2", "x-wp-totalpages": "1"}, feed_url),
        }
    )
    snapshot = WhitingFacultySource(base, transport, use_people_feed=True).fetch_faculty()
    assert snapshot.membership_complete is True
    assert snapshot.errors == ()
    assert len(snapshot.records) == 2
    assert {record.name for record in snapshot.records} == {"Alex Kim", "Pat Lee"}
    assert {record.source_payload["wp_person_id"] for record in snapshot.records} == {1, 2}
    assert {record.source_payload["profile_parse_version"] for record in snapshot.records} == {"whiting-wp-rest-v1"}
    assert [url for url, _ in transport.requests] == [base, feed_url]


def test_public_people_feed_mismatch_prevents_complete_membership(fixture_transport: type) -> None:
    base = "https://engineering.jhu.edu/faculty"
    directory = (
        "<main data-membership-complete='true'><article class='faculty-card' data-role='Faculty'>"
        "<a href='/faculty/alex-kim'><span class='faculty-name'>Alex Kim</span></a>"
        "</article></main>"
    )
    query = urlencode({"per_page": 100, "page": 1, "_fields": "id,link,title,content"})
    feed_url = f"https://engineering.jhu.edu/wp-json/wp/v2/people?{query}"
    payload = (
        '[{"id":1,"link":"https://engineering.jhu.edu/faculty/pat-lee/",'
        '"title":{"rendered":"Pat Lee"},"content":{"rendered":""}}]'
    )
    source = WhitingFacultySource(
        base,
        fixture_transport(
            {
                base: directory,
                feed_url: HttpResponse(200, payload.encode(), {"x-wp-total": "1", "x-wp-totalpages": "1"}, feed_url),
            }
        ),
        use_people_feed=True,
    )
    snapshot = source.fetch_faculty()
    assert snapshot.membership_complete is False
    assert snapshot.records == ()
    assert any("no unique people-feed profile" in error for error in snapshot.errors)


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
