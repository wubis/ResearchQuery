"""Shared canonicalization and Whiting eligibility policy."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from research_query.data.models import (
    EligibilityStatus,
    NormalizedFaculty,
    RawFacultyRecord,
)

_SPACE = re.compile(r"\s+")
_NAME_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    return cleaned or None


def normalize_name(value: str) -> str:
    cleaned = clean_text(value) or ""
    decomposed = unicodedata.normalize("NFKD", cleaned.casefold())
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _SPACE.sub(" ", _NAME_PUNCTUATION.sub(" ", unaccented)).strip()


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"unsupported source URL: {value!r}")
    if parts.username or parts.password:
        raise ValueError("source URLs may not contain credentials")
    port = f":{parts.port}" if parts.port else ""
    host = f"{parts.hostname.lower()}{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMETERS
        )
    )
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def normalize_unit(value: str | None) -> str | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    aliases = {
        "whiting school of engineering": "Whiting School of Engineering",
        "johns hopkins whiting school of engineering": "Whiting School of Engineering",
        "wse": "Whiting School of Engineering",
    }
    return aliases.get(cleaned.casefold(), cleaned)


def classify_whiting_eligibility(record: RawFacultyRecord) -> tuple[EligibilityStatus, str]:
    policy = "whiting-v1"
    role_text = " ".join(
        part
        for part in (
            record.title,
            record.source_role_category,
            *record.eligibility_evidence,
        )
        if part
    ).casefold()
    excluded = ("student", "postdoctoral", "postdoc", "staff", "administrator", "alumni")
    if any(term in role_text for term in excluded) and "professor" not in role_text:
        return EligibilityStatus.EXCLUDED, f"{policy}: non-faculty role evidence ({role_text})"
    if any(term in role_text for term in ("emeritus", "emerita", "affiliate", "affiliated")):
        return EligibilityStatus.REVIEW, f"{policy}: current research appointment needs review"
    faculty_signals = ("faculty", "professor", "lecturer", "instructor")
    if any(term in role_text for term in faculty_signals):
        return EligibilityStatus.ELIGIBLE, f"{policy}: authoritative source identifies faculty role"
    return EligibilityStatus.REVIEW, f"{policy}: authoritative listing lacks explicit faculty role"


def stable_payload_hash(record: RawFacultyRecord) -> str:
    payload = json.dumps(asdict(record), sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def normalize_faculty(record: RawFacultyRecord) -> NormalizedFaculty:
    name = clean_text(record.name)
    if not name:
        raise ValueError("faculty name is required")
    profile_url = canonicalize_url(record.profile_url or record.source_url)
    status, reason = classify_whiting_eligibility(record)
    return NormalizedFaculty(
        source=record,
        normalized_name=normalize_name(name),
        profile_url=profile_url,
        eligibility_status=status,
        eligibility_reason=reason,
        content_hash=stable_payload_hash(record),
    )


def normalized_terms(value: str) -> set[str]:
    return {term for term in normalize_name(value).split() if len(term) > 1}
