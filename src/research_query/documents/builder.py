"""Deterministic, provenance-preserving ResearchDocument construction."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from research_query.data.models import DocumentType, PublicationRecord, RawFacultyRecord, ResearchDocument
from research_query.ingestion.normalize import canonicalize_url, clean_text, normalize_name
from research_query.ingestion.publications.policy import normalize_doi, publication_aliases

DOCUMENT_NAMESPACE = uuid5(NAMESPACE_URL, "researchquery-document-v1")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_BOILERPLATE = re.compile(
    r"^(skip to content|menu|search|contact|cookie policy|privacy policy|all rights reserved)$",
    re.IGNORECASE,
)


class TokenCounter(Protocol):
    def count_tokens(self, text: str) -> int: ...


class WhitespaceTokenCounter:
    """Deterministic fallback for construction; production embedding uses its exact tokenizer."""

    def count_tokens(self, text: str) -> int:
        return len(text.split())


@dataclass(frozen=True, slots=True)
class LabEvidence:
    url: str
    title: str
    text: str
    source_name: str
    source_content_id: str | None = None


def canonical_content_hash(title: str, text: str) -> str:
    canonical = f"{clean_text(title) or ''}\n{clean_text(text) or ''}"
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_document_id(faculty_id: UUID, document_type: DocumentType, external_id: str, chunk_key: str) -> UUID:
    return uuid5(DOCUMENT_NAMESPACE, f"{faculty_id}|{document_type.value}|{external_id}|{chunk_key}")


def _clean_body(text: str) -> str:
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", text):
        lines = [clean_text(line) for line in paragraph.splitlines()]
        useful = [line for line in lines if line and not _BOILERPLATE.match(line)]
        if useful:
            paragraphs.append(" ".join(useful))
    return "\n\n".join(paragraphs)


def _hard_split(text: str, counter: TokenCounter, maximum: int) -> list[str]:
    def split_words(value: str) -> list[str]:
        words = value.split()
        pieces: list[str] = []
        while words:
            low, high = 0, len(words)
            while low < high:
                middle = (low + high + 1) // 2
                if counter.count_tokens(" ".join(words[:middle])) <= maximum:
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                raise ValueError("a single source token exceeds the hard embedding limit")
            pieces.append(" ".join(words[:low]))
            words = words[low:]
        return pieces

    sentences = _SENTENCE.split(text)
    chunks: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        if counter.count_tokens(sentence) > maximum:
            if current:
                chunks.append(" ".join(current))
                current = []
            chunks.extend(split_words(sentence))
        elif counter.count_tokens(" ".join((*current, sentence))) <= maximum:
            current.append(sentence)
        else:
            chunks.append(" ".join(current))
            current = [sentence]
    if current:
        chunks.append(" ".join(current))
    return chunks


def chunk_body(
    title: str,
    body: str,
    *,
    counter: TokenCounter,
    hard_formatted_limit: int = 448,
    target_body_limit: int = 400,
    overlap_tokens: int = 50,
) -> tuple[tuple[str, str], ...]:
    cleaned = _clean_body(body)
    formatted = f"Title: {title}\nText: {cleaned}"
    if counter.count_tokens(formatted) <= hard_formatted_limit:
        return (("whole", cleaned),)
    title_cost = counter.count_tokens(f"Title: {title}\nText:")
    body_hard_limit = max(1, hard_formatted_limit - title_cost)
    target = min(target_body_limit, body_hard_limit)
    paragraphs = [part for part in cleaned.split("\n\n") if part]
    atoms: list[str] = []
    for paragraph in paragraphs:
        atoms.extend(_hard_split(paragraph, counter, body_hard_limit))
    chunks: list[str] = []
    current: list[str] = []
    for atom in atoms:
        proposed = "\n\n".join((*current, atom))
        if current and counter.count_tokens(proposed) > target:
            completed = "\n\n".join(current)
            chunks.append(completed)
            overlap = completed.split()[-overlap_tokens:] if overlap_tokens else []
            while overlap and counter.count_tokens(" ".join(overlap)) > overlap_tokens:
                overlap.pop(0)
            current = [" ".join(overlap), atom] if overlap else [atom]
            if counter.count_tokens("\n\n".join(current)) > body_hard_limit:
                current = [atom]
        else:
            current.append(atom)
    if current:
        chunks.append("\n\n".join(current))
    return tuple((f"section-{index:03d}", chunk) for index, chunk in enumerate(chunks, start=1))


def _hopkins_external_id(record: RawFacultyRecord, semantic_section: str) -> str:
    source_content_id = str(record.source_payload.get("source_content_id") or record.profile_url or record.source_url)
    canonical_id = (
        canonicalize_url(source_content_id)
        if source_content_id.startswith(("http://", "https://"))
        else source_content_id
    )
    return f"hopkins:{record.source_name}:{canonical_id}:{semantic_section}"


def build_hopkins_documents(
    faculty_id: UUID,
    source_record_id: UUID,
    record: RawFacultyRecord,
    *,
    token_counter: TokenCounter | None = None,
    lab_evidence: LabEvidence | None = None,
) -> tuple[ResearchDocument, ...]:
    counter = token_counter or WhitespaceTokenCounter()
    profile_url = canonicalize_url(record.profile_url or record.source_url)
    embedded_lab_description = record.source_payload.get("lab_description")
    if (
        lab_evidence is None
        and record.lab_url
        and isinstance(embedded_lab_description, str)
        and embedded_lab_description.strip()
    ):
        lab_title = record.source_payload.get("lab_title")
        lab_evidence = LabEvidence(
            url=record.lab_url,
            title=str(lab_title or f"{record.name} — Lab"),
            text=embedded_lab_description,
            source_name=record.source_name,
        )
    specs: list[tuple[DocumentType, str, str, str, str, Mapping[str, object]]] = []
    if record.research_summary or record.research_areas:
        research_text = record.research_summary or "; ".join(record.research_areas)
        specs.append(
            (
                DocumentType.FACULTY_RESEARCH,
                f"{record.name} — Research Interests",
                research_text,
                profile_url,
                _hopkins_external_id(record, "research"),
                {"research_areas": list(record.research_areas)},
            )
        )
    if record.biography:
        specs.append(
            (
                DocumentType.FACULTY_BIO,
                f"{record.name} — Biography",
                record.biography,
                profile_url,
                _hopkins_external_id(record, "biography"),
                {},
            )
        )
    if lab_evidence:
        lab_url = canonicalize_url(lab_evidence.url)
        content_id = lab_evidence.source_content_id or lab_url
        specs.append(
            (
                DocumentType.LAB_DESCRIPTION,
                lab_evidence.title,
                lab_evidence.text,
                lab_url,
                f"hopkins:{record.source_name}:{content_id}:lab-description",
                {"evidence_authority": "linked_external_lab"},
            )
        )
    documents: list[ResearchDocument] = []
    for document_type, title, text, source_url, external_id, extras in specs:
        document_source_name = record.source_name
        if document_type == DocumentType.LAB_DESCRIPTION:
            if lab_evidence is None:  # defensive: lab specs are only created with evidence
                raise ValueError("lab evidence disappeared during document construction")
            document_source_name = lab_evidence.source_name
        for chunk_key, chunk in chunk_body(title, text, counter=counter):
            metadata = {
                **extras,
                "source_record_id": str(source_record_id),
                "extractor_version": "phase1-v1",
                "formatter_version": "v1",
            }
            documents.append(
                ResearchDocument(
                    document_id=make_document_id(faculty_id, document_type, external_id, chunk_key),
                    faculty_id=faculty_id,
                    document_type=document_type,
                    title=title,
                    text=chunk,
                    publication_year=None,
                    source_url=source_url,
                    source_name=document_source_name,
                    external_id=external_id,
                    chunk_key=chunk_key,
                    content_hash=canonical_content_hash(title, chunk),
                    metadata=metadata,
                )
            )
    return tuple(documents)


def publication_external_id(record: PublicationRecord) -> str:
    if doi := normalize_doi(record.doi):
        return f"doi:{doi}"
    if record.external_id:
        return f"{record.source_name}:{record.external_id}"
    digest = hashlib.sha256(f"{normalize_name(record.title)}|{record.year}".encode()).hexdigest()
    return f"title-year:{digest}"


def build_publication_document(faculty_id: UUID, record: PublicationRecord) -> ResearchDocument:
    external_id = publication_external_id(record)
    raw_aliases = record.metadata.get("identity_aliases", [])
    metadata_aliases = [str(item) for item in raw_aliases] if isinstance(raw_aliases, list | tuple) else []
    aliases = sorted(set((*publication_aliases(record), *metadata_aliases)))
    source_url = canonicalize_url(record.url or f"https://www.semanticscholar.org/paper/{record.external_id}")
    metadata = {
        **record.metadata,
        "doi": normalize_doi(record.doi),
        "authors": list(record.authors),
        "identity_aliases": aliases,
        "provider_ids": record.metadata.get("provider_ids", {record.source_name: [record.external_id]}),
        "formatter_version": "v1",
        "embedding_truncated": False,
    }
    text = record.abstract or ""
    return ResearchDocument(
        document_id=make_document_id(faculty_id, DocumentType.PUBLICATION, external_id, "whole"),
        faculty_id=faculty_id,
        document_type=DocumentType.PUBLICATION,
        title=clean_text(record.title) or record.title,
        text=clean_text(text) or "",
        publication_year=record.year,
        source_url=source_url,
        source_name=record.source_name,
        external_id=external_id,
        chunk_key="whole",
        content_hash=canonical_content_hash(record.title, text),
        metadata=metadata,
    )
