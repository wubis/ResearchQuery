#!/usr/bin/env python3
"""Read-only, paced audit of Whiting directory membership and pagination."""

from __future__ import annotations

import argparse
import json
from urllib.parse import urlsplit

from research_query.config import Settings
from research_query.ingestion.faculty.whiting import parse_directory_page
from research_query.ingestion.http import RetryingHttpTransport
from research_query.ingestion.normalize import canonicalize_url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--expected-pages", type=int)
    args = parser.parse_args()
    if args.max_pages < 1 or (args.expected_pages is not None and args.expected_pages < 1):
        parser.error("page counts must be positive")

    settings = Settings.from_env()
    transport = RetryingHttpTransport(
        user_agent=settings.http_user_agent,
        timeout_seconds=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
        min_interval_seconds=max(5.0, settings.whiting_min_interval_seconds),
    )
    page_url: str | None = canonicalize_url(settings.whiting_directory_url)
    host = urlsplit(page_url).hostname
    visited: set[str] = set()
    all_keys: set[str] = set()
    listed = linked = unlinked = duplicates = 0
    errors: list[str] = []
    while page_url is not None and len(visited) < args.max_pages:
        if page_url in visited:
            errors.append(f"pagination cycle at {page_url}")
            break
        if urlsplit(page_url).hostname != host:
            errors.append(f"pagination left the Whiting host: {page_url}")
            break
        visited.add(page_url)
        response = transport.get(page_url)
        records, next_url, page_errors = parse_directory_page(response.text(), page_url)
        listed += len(records)
        for record in records:
            if record.profile_url is None:
                unlinked += 1
                print(json.dumps({"page": len(visited), "unlinked_member": record.name}), flush=True)
            else:
                linked += 1
                if record.key in all_keys:
                    duplicates += 1
                if record.key is not None:
                    all_keys.add(record.key)
        errors.extend(f"page {len(visited)}: {error}" for error in page_errors)
        print(
            json.dumps(
                {"page": len(visited), "cards": len(records), "next": next_url is not None, "errors": page_errors}
            ),
            flush=True,
        )
        page_url = next_url

    if page_url is not None:
        errors.append(f"directory audit stopped before terminal page (max_pages={args.max_pages})")
    if args.expected_pages is not None and len(visited) != args.expected_pages:
        errors.append(f"expected {args.expected_pages} pages, observed {len(visited)}")
    print(
        json.dumps(
            {
                "pages": len(visited),
                "listed": listed,
                "linked": linked,
                "unlinked": unlinked,
                "duplicate_profile_keys": duplicates,
                "complete": not errors,
                "errors": errors,
            }
        ),
        flush=True,
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
