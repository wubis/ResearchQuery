"""Conservative, versioned, auditable scholarly-author resolution."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from research_query.data.models import (
    AuthorCandidate,
    AuthorCandidateBatch,
    AuthorResolution,
    FacultyForResolution,
    ResolutionStatus,
)
from research_query.ingestion.normalize import normalize_name, normalized_terms

RESOLVER_VERSION = "weighted-signals-v1"


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    resolve_threshold: float = 0.80
    ambiguous_threshold: float = 0.60
    resolve_margin: float = 0.15


def _name_features(expected: str, actual: str) -> tuple[float, bool, bool]:
    left = normalize_name(expected)
    right = normalize_name(actual)
    if left == right and left:
        return 0.30, True, False
    left_parts = left.split()
    right_parts = right.split()
    surname_matches = bool(left_parts and right_parts and left_parts[-1] == right_parts[-1])
    first_matches = bool(left_parts and right_parts and left_parts[0][:1] == right_parts[0][:1])
    middle_contradiction = len(left_parts) > 2 and len(right_parts) > 2 and left_parts[1][:1] != right_parts[1][:1]
    similarity = SequenceMatcher(None, left, right).ratio()
    gate = surname_matches and first_matches and similarity >= 0.72 and not middle_contradiction
    return (round(0.30 * similarity, 6) if gate else 0.0), gate, middle_contradiction


def names_compatible(expected: str, actual: str) -> bool:
    """Public validation gate for a previously persisted provider author ID."""
    _score_value, gate, contradiction = _name_features(expected, actual)
    return gate and not contradiction


def _publication_overlap(faculty: FacultyForResolution, candidate: AuthorCandidate) -> tuple[float, list[str]]:
    expected = {normalize_name(title) for title in faculty.known_publication_titles}
    actual = {normalize_name(paper.title) for paper in candidate.known_papers}
    matches = sorted(expected.intersection(actual) - {""})
    return (0.25 if matches else 0.0), matches


def _score(faculty: FacultyForResolution, candidate: AuthorCandidate) -> tuple[float, dict[str, object]]:
    name_score, name_gate, middle_contradiction = _name_features(faculty.name, candidate.name)
    affiliations = " ".join(candidate.affiliations).casefold()
    hopkins = "johns hopkins" in affiliations or "hopkins university" in affiliations
    affiliation_score = 0.25 if hopkins else 0.0
    publication_score, publication_matches = _publication_overlap(faculty, candidate)
    faculty_terms = normalized_terms(" ".join((faculty.research_text, *faculty.affiliations)))
    topic_terms = normalized_terms(" ".join((*candidate.topics, *(paper.title for paper in candidate.known_papers))))
    overlap = sorted(faculty_terms.intersection(topic_terms))
    denominator = max(1, min(8, len(faculty_terms)))
    topic_score = round(min(0.15, 0.15 * len(overlap) / denominator), 6)
    direct_id = faculty.external_identifiers.get("semantic_scholar") == candidate.author_id
    external_score = 0.05 if direct_id else 0.0
    hard_contradiction = bool(
        candidate.affiliations and not hopkins and not publication_matches and topic_score == 0 and not direct_id
    )
    score = round(name_score + affiliation_score + publication_score + topic_score + external_score, 6)
    if middle_contradiction or hard_contradiction:
        score = 0.0
    evidence: dict[str, object] = {
        "candidate_id": candidate.author_id,
        "candidate_name": candidate.name,
        "name_score": name_score,
        "name_gate": name_gate,
        "middle_name_contradiction": middle_contradiction,
        "hopkins_affiliation": hopkins,
        "affiliation_score": affiliation_score,
        "publication_matches": publication_matches,
        "publication_score": publication_score,
        "topic_matches": overlap,
        "topic_score": topic_score,
        "direct_provider_id": direct_id,
        "external_id_score": external_score,
        "hard_contradiction": hard_contradiction,
        "score": score,
    }
    return score, evidence


def resolve_author(
    faculty: FacultyForResolution,
    batch: AuthorCandidateBatch,
    policy: ResolutionPolicy | None = None,
) -> AuthorResolution | None:
    """Return no decision for incomplete requests so persistence preserves prior state."""
    policy = policy or ResolutionPolicy()
    if not batch.complete_for_request:
        return None
    scored = [(*_score(faculty, candidate), candidate) for candidate in batch.candidates]
    scored.sort(key=lambda item: (-item[0], item[2].author_id))
    candidate_evidence = [item[1] for item in scored]
    if not scored:
        return AuthorResolution(
            ResolutionStatus.UNRESOLVED,
            None,
            None,
            RESOLVER_VERSION,
            {"candidates": [], "decision": "no candidates"},
        )
    best_score, best_evidence, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    margin = round(best_score - second_score, 6)
    direct_matches = [
        item
        for item in scored
        if item[1]["direct_provider_id"] and item[1]["name_gate"] and not item[1]["hard_contradiction"]
    ]
    if len(direct_matches) == 1:
        _direct_score, direct_evidence, direct_candidate = direct_matches[0]
        return AuthorResolution(
            ResolutionStatus.RESOLVED,
            direct_candidate.author_id,
            1.0,
            RESOLVER_VERSION,
            {
                "candidates": candidate_evidence,
                "best_candidate_id": direct_candidate.author_id,
                "decision": "explicit provider ID validated with compatible name",
                "direct_evidence": direct_evidence,
            },
        )
    strong_non_name = bool(
        best_evidence["hopkins_affiliation"]
        or best_evidence["publication_matches"]
        or best_evidence["direct_provider_id"]
    )
    common_evidence = {
        "candidates": candidate_evidence,
        "best_candidate_id": best.author_id,
        "margin": margin,
        "thresholds": {
            "resolve": policy.resolve_threshold,
            "ambiguous": policy.ambiguous_threshold,
            "margin": policy.resolve_margin,
        },
        "strong_non_name_signal": strong_non_name,
    }
    if (
        best_score >= policy.resolve_threshold
        and margin >= policy.resolve_margin
        and bool(best_evidence["name_gate"])
        and strong_non_name
        and not bool(best_evidence["hard_contradiction"])
    ):
        return AuthorResolution(
            ResolutionStatus.RESOLVED,
            best.author_id,
            best_score,
            RESOLVER_VERSION,
            {**common_evidence, "decision": "resolved"},
        )
    if best_score >= policy.ambiguous_threshold:
        return AuthorResolution(
            ResolutionStatus.AMBIGUOUS,
            None,
            best_score,
            RESOLVER_VERSION,
            {**common_evidence, "decision": "ambiguous"},
        )
    return AuthorResolution(
        ResolutionStatus.UNRESOLVED,
        None,
        best_score,
        RESOLVER_VERSION,
        {**common_evidence, "decision": "below plausible threshold"},
    )
