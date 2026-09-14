# ResearchQuery

ResearchQuery is an evidence-first Johns Hopkins faculty research corpus. The repository is
currently limited to Phase 1: authoritative Whiting faculty ingestion, conservative Semantic
Scholar enrichment, independently searchable research documents, and versioned local BGE
embeddings in PostgreSQL with pgvector.

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

Apply the checksum-protected committed migrations, then ingest, embed, and inspect:

```bash
python scripts/migrate.py
python scripts/ingest.py
python scripts/embed.py
python scripts/inspect_corpus.py
```

Configuration is loaded and validated by `research_query.config.Settings.from_env()`. Replace the
placeholder contact in `HTTP_USER_AGENT` before making live source requests. Ingestion uses the
exact pinned BGE tokenizer for document chunk boundaries; embedding loads the same pinned model and
activates a version only after the database rechecks every active eligible document.

## Current verification limits

- Whiting parsing is covered by minimized directory/profile fixtures, including current live card,
  profile, and pagination markup. On 2026-09-14, campus-network requests returned HTTP 200: the
  directory declared 50 pages, with 10 cards on page 1 and 2 on page 50. One page-1 card had no
  profile link, so the adapter reports that membership page as incomplete rather than treating its
  department or email link as a profile. Full-directory counts and eligibility still need review
  before a production crawl.
- The PostgreSQL integration test is present and opt-in. It was not run in the initial environment
  because no `TEST_DATABASE_URL` was configured and the Docker daemon was unavailable.
- The BGE model/revision boundary is unit-tested with an injected local backend. Download and CPU
  inference for the real pinned artifact have not yet been measured in the initial environment.
