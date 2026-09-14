"""Fixture-tested Whiting directory and person-profile adapter."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import ClassVar
from urllib.error import HTTPError
from urllib.parse import urljoin

from research_query.data.models import FacultySourceSnapshot, RawAffiliation, RawFacultyRecord
from research_query.ingestion.http import HttpTransport
from research_query.ingestion.normalize import canonicalize_url, clean_text


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None
    children: list[_Node] = field(default_factory=list)
    content: list[str | _Node] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        pieces = [part if isinstance(part, str) else part.text() for part in self.content]
        return " ".join(piece for piece in pieces if piece)

    def descendants(self) -> list[_Node]:
        result: list[_Node] = []
        for child in self.children:
            result.append(child)
            result.extend(child.descendants())
        return result


class _TreeParser(HTMLParser):
    _VOID: ClassVar[set[str]] = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {}, None)
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs}, self.current)
        self.current.children.append(node)
        self.current.content.append(node)
        if tag not in self._VOID:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        cursor: _Node | None = self.current
        while cursor and cursor is not self.root:
            if cursor.tag == tag:
                self.current = cursor.parent or self.root
                return
            cursor = cursor.parent

    def handle_data(self, data: str) -> None:
        value = clean_text(data)
        if value:
            self.current.content.append(value)


def _parse(html: str) -> _Node:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return parser.root


def _first(
    node: _Node,
    *,
    classes: Collection[str] = frozenset(),
    tags: Collection[str] = frozenset(),
) -> _Node | None:
    for candidate in node.descendants():
        if classes and not candidate.classes.intersection(classes):
            continue
        if tags and candidate.tag not in tags:
            continue
        return candidate
    return None


@dataclass(frozen=True, slots=True)
class _DirectoryRecord:
    key: str
    source_url: str
    profile_url: str
    name: str
    title: str | None
    department: str | None
    role: str | None


def parse_directory_page(html: str, page_url: str) -> tuple[tuple[_DirectoryRecord, ...], str | None, tuple[str, ...]]:
    root = _parse(html)
    cards = [
        node
        for node in root.descendants()
        if node.attrs.get("data-faculty-id") or node.classes.intersection({"faculty-card", "person-card", "entity"})
    ]
    records: list[_DirectoryRecord] = []
    errors: list[str] = []
    seen_nodes: set[int] = set()
    for card in cards:
        if id(card) in seen_nodes:
            continue
        seen_nodes.add(id(card))
        name_node = _first(card, classes={"entity_name", "faculty-name", "person-name"}) or _first(
            card, tags={"h2", "h3", "h4"}
        )
        link = _first(card, classes={"entity_name_link", "faculty-name", "profile-link"}, tags={"a"})
        if link is None and name_node is not None:
            link = name_node.parent if name_node.parent is not None and name_node.parent.tag == "a" else None
            link = link or _first(name_node, tags={"a"})
        href = link.attrs.get("href") if link else None
        name = clean_text(name_node.text() if name_node else (link.text() if link else None))
        if not href or not name:
            errors.append(f"membership card missing profile link or name: {name or '<unknown>'}")
            continue
        profile_url = canonicalize_url(urljoin(page_url, href))
        title_node = _first(card, classes={"entity_title", "faculty-title", "person-title", "title"})
        department_node = _first(card, classes={"entity_department", "faculty-department", "department"})
        department_label = (
            _first(department_node, classes={"entity_detail_info_label"}) if department_node is not None else None
        )
        role = clean_text(card.attrs.get("data-role"))
        key = clean_text(card.attrs.get("data-faculty-id")) or profile_url
        records.append(
            _DirectoryRecord(
                key=key,
                source_url=canonicalize_url(page_url),
                profile_url=profile_url,
                name=name,
                title=clean_text(title_node.text()) if title_node else None,
                department=clean_text((department_label or department_node).text().replace("\ufeff", ""))
                if department_node
                else None,
                role=role,
            )
        )
    next_url: str | None = None
    terminal_page_confirmed = any(
        node.attrs.get("data-membership-complete", "false").casefold() == "true" for node in root.descendants()
    )
    for candidate in root.descendants():
        rel = set(candidate.attrs.get("rel", "").split())
        if candidate.tag == "a" and (
            "next" in rel or "pagination-next" in candidate.classes or "pagination_arrow_right" in candidate.classes
        ):
            href = candidate.attrs.get("href")
            disabled = (
                candidate.attrs.get("aria-disabled", "false").casefold() == "true"
                or "pagination_arrow_disabled" in candidate.classes
            )
            if disabled:
                terminal_page_confirmed = True
            elif href:
                next_url = canonicalize_url(urljoin(page_url, href))
                break
    if not cards:
        errors.append("no faculty membership cards found")
    elif next_url is None and not terminal_page_confirmed:
        errors.append("pagination end was not explicitly confirmed")
    return tuple(records), next_url, tuple(errors)


def parse_profile_page(html: str, directory: _DirectoryRecord) -> RawFacultyRecord:
    root = _parse(html)
    name_node = _first(root, classes={"faculty-name", "person-name", "page_title"}) or _first(root, tags={"h1"})
    title_node = _first(root, classes={"faculty-title", "person-title", "title", "page_description"})
    research_node = _first(root, classes={"research-interests", "research-summary", "research"})
    biography_node = _first(root, classes={"biography", "bio"}) or _first(root, classes={"page_content"})
    lab_description_node = _first(root, classes={"lab-description", "laboratory-description"})
    department_node = _first(root, classes={"page_header_detail_nav_item_link_label"})
    area_nodes = [node for node in root.descendants() if node.classes.intersection({"research-area", "expertise"})]
    publication_nodes = [
        node for node in root.descendants() if node.classes.intersection({"known-publication", "publication-title"})
    ]
    external_identifiers: dict[str, str] = {}
    for node in root.descendants():
        if semantic_id := clean_text(node.attrs.get("data-semantic-scholar-id")):
            external_identifiers["semantic_scholar"] = semantic_id
        if orcid := clean_text(node.attrs.get("data-orcid")):
            external_identifiers["orcid"] = orcid
    affiliations: list[RawAffiliation] = []
    affiliation_nodes = [node for node in root.descendants() if "affiliation" in node.classes]
    for node in affiliation_nodes:
        school = clean_text(node.attrs.get("data-school")) or "Whiting School of Engineering"
        department = clean_text(node.attrs.get("data-department")) or clean_text(node.text())
        affiliations.append(
            RawAffiliation(
                school=school,
                department=department,
                title=directory.title,
                source_url=directory.profile_url,
            )
        )
    if not affiliations:
        affiliations.append(
            RawAffiliation(
                school="Whiting School of Engineering",
                department=directory.department
                or (clean_text(department_node.text().replace("\ufeff", "")) if department_node else None),
                title=directory.title,
                source_url=directory.profile_url,
            )
        )
    lab_url: str | None = None
    for node in root.descendants():
        if node.tag == "a" and ({"lab-link", "laboratory"} & node.classes or node.attrs.get("rel") == "lab"):
            href = node.attrs.get("href")
            if href:
                lab_url = canonicalize_url(urljoin(directory.profile_url, href))
                break
    name = clean_text(name_node.text() if name_node else None) or directory.name
    title = clean_text(title_node.text()) if title_node else directory.title
    evidence = tuple(filter(None, (directory.role, title, "authoritative Whiting faculty directory")))
    return RawFacultyRecord(
        source_name="whiting",
        source_faculty_key=directory.key,
        source_url=directory.source_url,
        name=name,
        title=title,
        affiliations=tuple(affiliations),
        profile_url=directory.profile_url,
        lab_url=lab_url,
        research_areas=tuple(filter(None, (clean_text(node.text()) for node in area_nodes))),
        research_summary=clean_text(research_node.text()) if research_node else None,
        biography=clean_text(biography_node.text()) if biography_node else None,
        source_role_category=directory.role,
        eligibility_evidence=evidence,
        external_identifiers=external_identifiers,
        source_payload={
            "directory_name": directory.name,
            "directory_title": directory.title,
            "directory_department": directory.department,
            "lab_description": (clean_text(lab_description_node.text()) if lab_description_node else None),
            "lab_title": f"{name} — Lab",
            "known_publication_titles": [title for node in publication_nodes if (title := clean_text(node.text()))],
            "profile_parse_version": "whiting-html-v2",
        },
    )


def _merge_duplicate_records(records: list[RawFacultyRecord]) -> tuple[RawFacultyRecord, ...]:
    merged: dict[str, RawFacultyRecord] = {}
    for record in records:
        prior = merged.get(record.source_faculty_key)
        if prior is None:
            merged[record.source_faculty_key] = record
            continue
        affiliations = tuple(dict.fromkeys((*prior.affiliations, *record.affiliations)))
        payload = dict(prior.source_payload)
        for key in ("lab_description", "lab_title", "known_publication_titles"):
            payload[key] = prior.source_payload.get(key) or record.source_payload.get(key)
        if "profile_error" not in prior.source_payload or "profile_error" not in record.source_payload:
            payload.pop("profile_error", None)
        departments = {
            str(item)
            for item in (
                prior.source_payload.get("directory_department"),
                record.source_payload.get("directory_department"),
            )
            if item
        }
        payload["directory_departments"] = sorted(departments)
        prior_count = prior.source_payload.get("duplicate_listing_count", 1)
        payload["duplicate_listing_count"] = (prior_count if isinstance(prior_count, int) else 1) + 1
        merged[record.source_faculty_key] = replace(
            prior,
            title=prior.title or record.title,
            affiliations=affiliations,
            profile_url=prior.profile_url or record.profile_url,
            lab_url=prior.lab_url or record.lab_url,
            research_areas=tuple(dict.fromkeys((*prior.research_areas, *record.research_areas))),
            research_summary=prior.research_summary or record.research_summary,
            biography=prior.biography or record.biography,
            eligibility_evidence=tuple(dict.fromkeys((*prior.eligibility_evidence, *record.eligibility_evidence))),
            external_identifiers={**record.external_identifiers, **prior.external_identifiers},
            source_payload=payload,
        )
    return tuple(merged.values())


class WhitingFacultySource:
    source_name = "whiting"

    def __init__(
        self,
        directory_url: str,
        transport: HttpTransport,
        *,
        now: Callable[[], datetime] | None = None,
        max_pages: int = 100,
    ) -> None:
        self.directory_url = canonicalize_url(directory_url)
        self.transport = transport
        self.now = now or (lambda: datetime.now(UTC))
        self.max_pages = max_pages

    def fetch_faculty(self) -> FacultySourceSnapshot:
        fetched_at = self.now()
        page_url: str | None = self.directory_url
        visited: set[str] = set()
        records: list[RawFacultyRecord] = []
        errors: list[str] = []
        membership_complete = True
        source_hash = hashlib.sha256()
        while page_url and len(visited) < self.max_pages:
            if page_url in visited:
                errors.append(f"pagination cycle at {page_url}")
                membership_complete = False
                break
            visited.add(page_url)
            try:
                response = self.transport.get(page_url)
                body = response.text()
                source_hash.update(body.encode())
                discovered, next_url, page_errors = parse_directory_page(body, page_url)
                if page_errors:
                    membership_complete = False
                    errors.extend(f"{page_url}: {error}" for error in page_errors)
                for item in discovered:
                    try:
                        profile_response = self.transport.get(item.profile_url)
                        source_hash.update(profile_response.body)
                        records.append(parse_profile_page(profile_response.text(), item))
                    except Exception as exc:  # optional profile enrichment failure
                        errors.append(f"{item.profile_url}: profile enrichment failed: {exc}")
                        records.append(
                            RawFacultyRecord(
                                source_name=self.source_name,
                                source_faculty_key=item.key,
                                source_url=item.source_url,
                                name=item.name,
                                title=item.title,
                                affiliations=(
                                    RawAffiliation(
                                        school="Whiting School of Engineering",
                                        department=item.department,
                                        title=item.title,
                                        source_url=item.profile_url,
                                    ),
                                ),
                                profile_url=item.profile_url,
                                source_role_category=item.role,
                                eligibility_evidence=tuple(filter(None, (item.role, item.title, "directory listing"))),
                                source_payload={"profile_error": str(exc), "profile_parse_version": "whiting-html-v2"},
                            )
                        )
                page_url = next_url
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise
                membership_complete = False
                errors.append(f"{page_url}: membership fetch failed: {exc}")
                break
            except Exception as exc:
                membership_complete = False
                errors.append(f"{page_url}: membership fetch failed: {exc}")
                break
        if page_url is not None and len(visited) >= self.max_pages:
            membership_complete = False
            errors.append(f"pagination exceeded max_pages={self.max_pages}")
        snapshot_id = f"whiting:{fetched_at.isoformat()}:{source_hash.hexdigest()[:16]}"
        return FacultySourceSnapshot(
            source_name=self.source_name,
            snapshot_id=snapshot_id,
            fetched_at=fetched_at,
            records=_merge_duplicate_records(records),
            membership_complete=membership_complete,
            errors=tuple(errors),
        )
