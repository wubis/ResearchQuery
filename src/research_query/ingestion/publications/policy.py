"""Recent-publication deduplication and deterministic selection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import date

from research_query.data.models import PublicationRecord
from research_query.ingestion.normalize import normalize_name


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    result = value.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if result.startswith(prefix):
            result = result[len(prefix) :]
    return result.rstrip(" .") or None


def publication_aliases(record: PublicationRecord) -> tuple[str, ...]:
    aliases: list[str] = []
    if doi := normalize_doi(record.doi):
        aliases.append(f"doi:{doi}")
    if record.external_id:
        aliases.append(f"{record.source_name}:{record.external_id.casefold()}")
    if record.title and record.year is not None:
        aliases.append(f"title-year:{normalize_name(record.title)}|{record.year}")
    return tuple(aliases)


def _record_precedence(record: PublicationRecord) -> tuple[int, int, str, str]:
    return (
        int(normalize_doi(record.doi) is not None),
        int(bool(record.abstract)),
        record.publication_date or f"{record.year or 0:04d}",
        f"{record.source_name}:{record.external_id}",
    )


def deduplicate_publications(records: Iterable[PublicationRecord]) -> tuple[PublicationRecord, ...]:
    groups: list[list[PublicationRecord]] = []
    alias_to_group: dict[str, int] = {}
    for record in records:
        aliases = publication_aliases(record)
        matches = sorted({alias_to_group[alias] for alias in aliases if alias in alias_to_group})
        if not matches:
            index = len(groups)
            groups.append([record])
        else:
            index = matches[0]
            groups[index].append(record)
            for other in reversed(matches[1:]):
                groups[index].extend(groups[other])
                groups[other] = []
                for alias, old_index in list(alias_to_group.items()):
                    if old_index == other:
                        alias_to_group[alias] = index
        for alias in aliases:
            alias_to_group[alias] = index
    deduped: list[PublicationRecord] = []
    for group in groups:
        if not group:
            continue
        selected = max(group, key=_record_precedence)
        merged_aliases = sorted({alias for member in group for alias in publication_aliases(member)})
        provider_ids: dict[str, list[str]] = {}
        for member in group:
            provider_ids.setdefault(member.source_name, []).append(member.external_id)
        metadata = dict(selected.metadata)
        metadata["identity_aliases"] = merged_aliases
        metadata["provider_ids"] = {provider: sorted(set(ids)) for provider, ids in sorted(provider_ids.items())}
        deduped.append(replace(selected, metadata=metadata))
    return tuple(deduped)


def select_recent_publications(
    records: Iterable[PublicationRecord],
    *,
    cutoff_date: date,
    lookback_years: int,
    maximum: int,
) -> tuple[PublicationRecord, ...]:
    earliest = cutoff_date.year - lookback_years + 1
    candidates = [
        record
        for record in deduplicate_publications(records)
        if record.year is not None and earliest <= record.year <= cutoff_date.year
    ]

    def newest(record: PublicationRecord) -> tuple[str, str]:
        return (
            record.publication_date or f"{record.year or 0:04d}-00-00",
            f"{record.source_name}:{record.external_id}",
        )

    with_abstract = sorted((record for record in candidates if record.abstract), key=newest, reverse=True)
    title_only = sorted(
        (record for record in candidates if not record.abstract and len(normalize_name(record.title)) >= 12),
        key=newest,
        reverse=True,
    )
    return tuple((with_abstract + title_only)[:maximum])
