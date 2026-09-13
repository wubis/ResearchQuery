from __future__ import annotations

from research_query.data.models import EligibilityStatus, RawFacultyRecord
from research_query.ingestion.normalize import canonicalize_url, normalize_faculty, normalize_name


def record(**changes: object) -> RawFacultyRecord:
    values: dict[str, object] = {
        "source_name": "whiting",
        "source_faculty_key": "1",
        "source_url": "https://engineering.jhu.edu/faculty",
        "profile_url": "https://engineering.jhu.edu/faculty/José/?utm_source=x#bio",
        "name": "  José\u00a0 Álvarez ",
        "title": "Associate Professor",
        "source_role_category": "Faculty",
    }
    values.update(changes)
    return RawFacultyRecord(**values)  # type: ignore[arg-type]


def test_normalization_preserves_display_but_produces_comparison_forms() -> None:
    normalized = normalize_faculty(record())
    assert normalize_name(normalized.source.name) == "jose alvarez"
    assert normalized.profile_url == "https://engineering.jhu.edu/faculty/José"
    assert normalized.eligibility_status is EligibilityStatus.ELIGIBLE
    assert normalized.content_hash == normalize_faculty(record()).content_hash


def test_eligibility_is_separate_from_membership() -> None:
    assert normalize_faculty(record(title="Professor Emeritus")).eligibility_status is EligibilityStatus.REVIEW
    postdoc = normalize_faculty(record(title="Postdoctoral Fellow", source_role_category="Postdoc"))
    assert postdoc.eligibility_status is EligibilityStatus.EXCLUDED


def test_canonical_url_rejects_unsafe_schemes_and_credentials() -> None:
    assert canonicalize_url("HTTPS://Example.EDU/a//b/?utm_medium=x&z=2") == "https://example.edu/a/b?z=2"
    import pytest

    with pytest.raises(ValueError):
        canonicalize_url("javascript:alert(1)")
    with pytest.raises(ValueError):
        canonicalize_url("https://user:secret@example.edu/a")
