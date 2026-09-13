BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE faculty (
    faculty_id uuid PRIMARY KEY,
    name text NOT NULL,
    normalized_name text NOT NULL,
    title text,
    profile_url text NOT NULL,
    lab_url text,
    research_summary text,
    is_active boolean NOT NULL DEFAULT true,
    eligibility_status text NOT NULL CHECK (eligibility_status IN ('eligible', 'review', 'excluded')),
    eligibility_reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ingestion_runs (
    run_id uuid PRIMARY KEY,
    source_name text NOT NULL,
    snapshot_id text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    membership_complete boolean,
    status text NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    records_seen integer NOT NULL DEFAULT 0,
    records_created integer NOT NULL DEFAULT 0,
    records_updated integer NOT NULL DEFAULT 0,
    records_deactivated integer NOT NULL DEFAULT 0,
    documents_created integer NOT NULL DEFAULT 0,
    documents_updated integer NOT NULL DEFAULT 0,
    configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_summary jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE UNIQUE INDEX ingestion_runs_one_running_source
    ON ingestion_runs (source_name) WHERE status = 'running';

CREATE TABLE faculty_source_records (
    source_record_id uuid PRIMARY KEY,
    faculty_id uuid NOT NULL REFERENCES faculty(faculty_id),
    source_name text NOT NULL,
    source_faculty_key text NOT NULL,
    source_url text NOT NULL,
    profile_url text,
    raw_payload jsonb NOT NULL,
    content_hash text NOT NULL,
    last_seen_at timestamptz NOT NULL,
    missing_run_count integer NOT NULL DEFAULT 0 CHECK (missing_run_count >= 0),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_name, source_faculty_key)
);

CREATE INDEX faculty_source_records_faculty_idx ON faculty_source_records (faculty_id);
CREATE INDEX faculty_source_records_profile_idx
    ON faculty_source_records ((lower(regexp_replace(COALESCE(profile_url, source_url), '/+$', ''))));

CREATE TABLE faculty_affiliations (
    affiliation_id uuid PRIMARY KEY,
    faculty_id uuid NOT NULL REFERENCES faculty(faculty_id),
    school text NOT NULL,
    department text,
    title text,
    is_primary boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    source_record_id uuid NOT NULL REFERENCES faculty_source_records(source_record_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX faculty_affiliations_active_unit_uniq
    ON faculty_affiliations (faculty_id, school, COALESCE(department, '')) WHERE is_active;
CREATE UNIQUE INDEX faculty_affiliations_one_primary_uniq
    ON faculty_affiliations (faculty_id) WHERE is_active AND is_primary;
CREATE INDEX faculty_affiliations_source_idx ON faculty_affiliations (source_record_id);

CREATE TABLE scholarly_author_links (
    author_link_id uuid PRIMARY KEY,
    faculty_id uuid NOT NULL REFERENCES faculty(faculty_id),
    source_name text NOT NULL,
    external_author_id text,
    resolution_status text NOT NULL
        CHECK (resolution_status IN ('unresolved', 'ambiguous', 'resolved')),
    confidence double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    resolver_version text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    validated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (faculty_id, source_name),
    CHECK (resolution_status <> 'resolved' OR external_author_id IS NOT NULL)
);

CREATE UNIQUE INDEX scholarly_author_links_resolved_identity_uniq
    ON scholarly_author_links (source_name, external_author_id)
    WHERE resolution_status = 'resolved';
CREATE INDEX scholarly_author_links_faculty_idx ON scholarly_author_links (faculty_id);

CREATE TABLE faculty_redirects (
    from_faculty_id uuid PRIMARY KEY REFERENCES faculty(faculty_id),
    to_faculty_id uuid NOT NULL REFERENCES faculty(faculty_id),
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (from_faculty_id <> to_faculty_id)
);

CREATE OR REPLACE FUNCTION reject_faculty_redirect_cycle() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    cursor_id uuid;
BEGIN
    cursor_id := NEW.to_faculty_id;
    WHILE cursor_id IS NOT NULL LOOP
        IF cursor_id = NEW.from_faculty_id THEN
            RAISE EXCEPTION 'faculty redirect cycle';
        END IF;
        SELECT to_faculty_id INTO cursor_id
        FROM faculty_redirects WHERE from_faculty_id = cursor_id;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER faculty_redirect_no_cycle
BEFORE INSERT OR UPDATE ON faculty_redirects
FOR EACH ROW EXECUTE FUNCTION reject_faculty_redirect_cycle();

CREATE TABLE research_documents (
    document_id uuid PRIMARY KEY,
    faculty_id uuid NOT NULL REFERENCES faculty(faculty_id),
    document_type text NOT NULL CHECK (
        document_type IN ('faculty_research', 'faculty_bio', 'lab_description', 'publication')
    ),
    title text NOT NULL,
    text text NOT NULL,
    publication_year smallint,
    source_url text NOT NULL CHECK (source_url <> ''),
    source_name text NOT NULL,
    external_id text NOT NULL,
    chunk_key text NOT NULL,
    content_hash text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_vector tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(text, '')), 'B')
    ) STORED,
    is_active boolean NOT NULL DEFAULT true,
    missing_run_count integer NOT NULL DEFAULT 0 CHECK (missing_run_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX research_documents_active_identity_uniq
    ON research_documents (faculty_id, document_type, external_id, chunk_key) WHERE is_active;
CREATE INDEX research_documents_faculty_idx ON research_documents (faculty_id);
CREATE INDEX research_documents_search_idx ON research_documents USING gin (search_vector);
CREATE INDEX research_documents_aliases_idx
    ON research_documents USING gin ((metadata -> 'identity_aliases'));

CREATE TABLE document_embeddings (
    document_id uuid NOT NULL REFERENCES research_documents(document_id),
    embedding_version text NOT NULL,
    embedding vector(768) NOT NULL,
    document_content_hash text NOT NULL,
    embedding_input_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, embedding_version)
);

CREATE TABLE corpus_snapshots (
    corpus_snapshot_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    source_runs jsonb NOT NULL,
    description text
);

CREATE TABLE search_index_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    embedding_version text NOT NULL,
    activated_at timestamptz NOT NULL,
    corpus_snapshot_id uuid NOT NULL REFERENCES corpus_snapshots(corpus_snapshot_id)
);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER faculty_touch BEFORE UPDATE ON faculty
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER faculty_source_records_touch BEFORE UPDATE ON faculty_source_records
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER faculty_affiliations_touch BEFORE UPDATE ON faculty_affiliations
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER scholarly_author_links_touch BEFORE UPDATE ON scholarly_author_links
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER research_documents_touch BEFORE UPDATE ON research_documents
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMIT;
