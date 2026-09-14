# ResearchQuery

ResearchQuery is an evidence-first Johns Hopkins faculty research corpus. The repository is
currently limited to Phase 1: authoritative Whiting faculty ingestion, conservative Semantic
Scholar enrichment, independently searchable research documents, and versioned local BGE
embeddings in PostgreSQL with pgvector. Whiting ingestion and local embedding activation have
been verified; live publication enrichment remains pending a usable Semantic Scholar quota.

The governing design is [`ELEPHANT.md`](ELEPHANT.md). Phase 2 retrieval, evaluation, and UI code
are deliberately absent.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

Default tests use minimized Whiting HTML and Semantic Scholar JSON fixtures and never call the
network. PostgreSQL integration tests run only when `TEST_DATABASE_URL` points to a disposable
database with pgvector available:

```bash
TEST_DATABASE_URL=postgresql://... .venv/bin/pytest -m postgres
```

Apply the checksum-protected committed migrations, then ingest, embed, and inspect. Whiting uses
the public, paged Hopkins WordPress people feed to reconcile directory members, including cards
without profile links. Requests on that host honor its five-second robots crawl delay. Use a
descriptive contact `HTTP_USER_AGENT`; do not commit personal contact details or API keys.

```bash
python scripts/migrate.py
python scripts/audit_whiting.py --expected-pages 50  # optional read-only audit
python scripts/ingest.py --faculty-only --summary      # Hopkins source only
python scripts/embed.py
python scripts/inspect_corpus.py
```

For publication enrichment, request a key from the
[official Semantic Scholar API page](https://www.semanticscholar.org/product/api) if needed,
then configure `SEMANTIC_SCHOLAR_API_KEY` in the runtime environment
and run `python scripts/ingest.py --summary`, then rerun `python scripts/embed.py`. The
`--faculty-only` option skips scholarly requests while still producing a complete Whiting
membership snapshot. The default ingest output includes per-faculty traces; `--summary` shows
aggregate counts. Semantic Scholar requests are paced at least 1.1 seconds apart, consistent
with its introductory keyed limit of one request per second.

Configuration is loaded and validated by `research_query.config.Settings.from_env()`. Replace the
placeholder contact in `HTTP_USER_AGENT` before making live source requests. Ingestion uses the
exact pinned BGE tokenizer for document chunk boundaries; embedding loads the same pinned model and
activates a version only after the database rechecks every active eligible document.

## Verified local status (2026-09-14)

- The complete live Whiting directory spans 50 pages and 492 members (442 linked cards and 50
  unlinked cards). A full paced ingest reconciled all 492 against the Hopkins-owned public people
  feed with zero errors. The corrected refresh created one missing source record and changed no
  existing documents; partial snapshots do not advance absence-based deactivation.
- The local PostgreSQL 16 + pgvector corpus has 492 active source records: 473 eligible and 19
  review (all emeritus/emerita titles). It has 562 active biography chunks. Of eligible people,
  423 have source documents and 50 have none; research-summary and lab document counts are zero.
  These source-content gaps are explicit, not filled with inferred text.
- The exact pinned BGE model was downloaded and loaded on CPU. A warm 16-document batch ran at
  about 13 documents/second, with about 762 MB peak process RSS in the local measurement. The
  active index has 536 eligible-document embeddings and zero missing or stale embeddings.
- All 43 tests pass against disposable PostgreSQL + pgvector; Ruff and mypy pass. Without
  `TEST_DATABASE_URL`, the one database integration test is intentionally skipped.
- A representative unauthenticated Semantic Scholar author request returned HTTP 429. There are
  currently no scholarly author resolutions or publication documents in the live corpus. An API
  key or approved sufficient quota is needed before live publication coverage can be validated.
