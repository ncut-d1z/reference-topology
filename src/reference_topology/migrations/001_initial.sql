-- Bibliographic identity, file content, observations, and decisions are distinct.
CREATE TABLE papers (
    id TEXT PRIMARY KEY NOT NULL,
    title TEXT, title_normalized TEXT, publication_type TEXT, venue TEXT,
    volume TEXT, issue TEXT, pages TEXT, article_number TEXT, date_text TEXT,
    abstract TEXT, language TEXT, year INTEGER,
    record_status TEXT NOT NULL DEFAULT 'stub' CHECK(record_status IN ('stub','identified','reviewed')),
    canonical_paper_id TEXT REFERENCES papers(id) ON DELETE RESTRICT,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT,
    CHECK(canonical_paper_id IS NULL OR canonical_paper_id <> id)
);
CREATE TABLE metadata_claims (
    id TEXT PRIMARY KEY NOT NULL,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    field_name TEXT NOT NULL, value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    source_kind TEXT NOT NULL,
    run_id TEXT REFERENCES extraction_runs(id) ON DELETE RESTRICT,
    reference_entry_id TEXT REFERENCES reference_entries(id) ON DELETE RESTRICT,
    source_key TEXT, source_url TEXT, observed_at TEXT, score REAL,
    is_selected INTEGER NOT NULL DEFAULT 0 CHECK(is_selected IN (0,1)),
    is_manual INTEGER NOT NULL DEFAULT 0 CHECK(is_manual IN (0,1)),
    supersedes_id TEXT REFERENCES metadata_claims(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);
CREATE TABLE paper_identifiers (
    id TEXT PRIMARY KEY NOT NULL,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    scheme TEXT NOT NULL CHECK(scheme IN ('doi','arxiv')),
    value_raw TEXT NOT NULL, value_normalized TEXT NOT NULL,
    metadata_claim_id TEXT NOT NULL REFERENCES metadata_claims(id) ON DELETE RESTRICT,
    UNIQUE(scheme,value_normalized)
);
CREATE TABLE paper_authors (
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK(position >= 1),
    display_name TEXT NOT NULL, given_name TEXT, family_name TEXT, orcid TEXT,
    PRIMARY KEY(paper_id,position)
);
CREATE TABLE pdf_assets (
    id TEXT PRIMARY KEY NOT NULL,
    sha256 TEXT NOT NULL UNIQUE CHECK(length(sha256)=64),
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    page_count INTEGER CHECK(page_count IS NULL OR page_count >= 0),
    mime_type TEXT NOT NULL DEFAULT 'application/pdf',
    derived_from_asset_id TEXT REFERENCES pdf_assets(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);
CREATE TABLE file_locations (
    id TEXT PRIMARY KEY NOT NULL,
    asset_id TEXT NOT NULL REFERENCES pdf_assets(id) ON DELETE RESTRICT,
    host_id TEXT NOT NULL, canonical_uri TEXT NOT NULL, original_uri TEXT,
    display_name TEXT NOT NULL, root_key TEXT NOT NULL,
    location_kind TEXT NOT NULL CHECK(location_kind IN ('linked','managed')),
    status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','missing','inaccessible','changed')),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),
    observed_size INTEGER, observed_mtime_ns INTEGER,
    created_at TEXT NOT NULL, last_checked_at TEXT NOT NULL
);
CREATE TABLE paper_files (
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    asset_id TEXT NOT NULL REFERENCES pdf_assets(id) ON DELETE RESTRICT,
    role TEXT NOT NULL DEFAULT 'primary' CHECK(role IN ('primary','alternate','supplement')),
    is_preferred INTEGER NOT NULL DEFAULT 0 CHECK(is_preferred IN (0,1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(paper_id,asset_id)
);
CREATE TABLE extraction_runs (
    id TEXT PRIMARY KEY NOT NULL,
    asset_id TEXT NOT NULL REFERENCES pdf_assets(id) ON DELETE RESTRICT,
    source_paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    parser_name TEXT NOT NULL, parser_version TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(config_json)),
    config_hash TEXT NOT NULL, input_sha256 TEXT NOT NULL, cache_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','partial','needs_ocr','failed','cancelled')),
    is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
    base_run_id TEXT REFERENCES extraction_runs(id) ON DELETE RESTRICT,
    output_uri TEXT, output_sha256 TEXT, warning_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(warning_json)),
    error_code TEXT, started_at TEXT NOT NULL, finished_at TEXT,
    UNIQUE(id,source_paper_id),
    CHECK(is_active=0 OR status IN ('succeeded','partial'))
);
CREATE TABLE reference_entries (
    id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL, source_paper_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1), label TEXT,
    raw_text TEXT NOT NULL CHECK(length(trim(raw_text)) > 0),
    parsed_json TEXT NOT NULL CHECK(json_valid(parsed_json)),
    locations_json TEXT CHECK(locations_json IS NULL OR json_valid(locations_json)),
    text_fingerprint TEXT NOT NULL,
    aligned_from_entry_id TEXT REFERENCES reference_entries(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id,ordinal), UNIQUE(id,source_paper_id),
    FOREIGN KEY(run_id,source_paper_id) REFERENCES extraction_runs(id,source_paper_id) ON DELETE RESTRICT
);
CREATE TABLE citation_assertions (
    id TEXT PRIMARY KEY NOT NULL,
    citing_paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    cited_paper_id TEXT REFERENCES papers(id) ON DELETE RESTRICT,
    reference_entry_id TEXT,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('pdf','manual','external')),
    provider TEXT, provider_record_id TEXT, source_url TEXT, observed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('candidate','accepted','rejected','retracted')),
    review_state TEXT NOT NULL DEFAULT 'current' CHECK(review_state IN ('current','needs_review')),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),
    is_pinned INTEGER NOT NULL DEFAULT 0 CHECK(is_pinned IN (0,1)),
    method TEXT NOT NULL, matcher_version TEXT NOT NULL DEFAULT '1',
    evidence_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(evidence_json)), score REAL,
    actor TEXT NOT NULL CHECK(actor IN ('system','user')),
    supersedes_id TEXT REFERENCES citation_assertions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(reference_entry_id,citing_paper_id) REFERENCES reference_entries(id,source_paper_id) ON DELETE RESTRICT,
    CHECK(status <> 'accepted' OR cited_paper_id IS NOT NULL),
    CHECK(source_kind <> 'pdf' OR reference_entry_id IS NOT NULL),
    CHECK(source_kind <> 'external' OR (provider IS NOT NULL AND provider_record_id IS NOT NULL AND observed_at IS NOT NULL))
);
CREATE TABLE paper_merge_events (
    id TEXT PRIMARY KEY NOT NULL,
    from_paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    to_paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL, actor TEXT NOT NULL,
    before_json TEXT NOT NULL CHECK(json_valid(before_json)),
    after_json TEXT NOT NULL CHECK(json_valid(after_json)),
    touched_versions_json TEXT NOT NULL CHECK(json_valid(touched_versions_json)),
    created_at TEXT NOT NULL, reverted_at TEXT
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY NOT NULL, kind TEXT NOT NULL,
    asset_id TEXT REFERENCES pdf_assets(id) ON DELETE RESTRICT,
    paper_id TEXT REFERENCES papers(id) ON DELETE RESTRICT,
    request_key TEXT NOT NULL UNIQUE, work_key TEXT NOT NULL,
    request_aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(request_aliases_json)),
    input_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(input_json)),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    attempt_history_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(attempt_history_json)),
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','partial','failed','cancelled','interrupted')),
    progress_current INTEGER NOT NULL DEFAULT 0 CHECK(progress_current >= 0),
    progress_total INTEGER NOT NULL DEFAULT 0 CHECK(progress_total >= 0),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
    lease_token TEXT, lease_until TEXT, heartbeat_at TEXT,
    error_code TEXT, error_message TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY NOT NULL,
    name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_current_file_location ON file_locations(host_id,canonical_uri) WHERE is_current=1;
CREATE UNIQUE INDEX uq_preferred_pdf ON paper_files(paper_id) WHERE is_preferred=1;
CREATE UNIQUE INDEX uq_selected_claim ON metadata_claims(paper_id,field_name) WHERE is_selected=1;
CREATE UNIQUE INDEX uq_active_extraction ON extraction_runs(source_paper_id,asset_id) WHERE is_active=1;
CREATE UNIQUE INDEX uq_accepted_reference_target ON citation_assertions(reference_entry_id)
    WHERE status='accepted' AND is_current=1 AND reference_entry_id IS NOT NULL;
CREATE UNIQUE INDEX uq_active_work ON jobs(work_key) WHERE status IN ('queued','running');
CREATE INDEX idx_citation_out ON citation_assertions(citing_paper_id,status,is_current);
CREATE INDEX idx_citation_in ON citation_assertions(cited_paper_id,status,is_current);
CREATE INDEX idx_citation_reference ON citation_assertions(reference_entry_id,is_current);
CREATE INDEX idx_reference_source ON reference_entries(source_paper_id);
CREATE INDEX idx_reference_fingerprint ON reference_entries(text_fingerprint);
CREATE INDEX idx_run_cache ON extraction_runs(cache_key,status);
CREATE INDEX idx_location_asset ON file_locations(asset_id,status);
CREATE INDEX idx_paper_file_asset ON paper_files(asset_id);
CREATE INDEX idx_paper_title_year ON papers(title_normalized,year);
CREATE INDEX idx_paper_canonical ON papers(canonical_paper_id);
CREATE INDEX idx_claim_paper ON metadata_claims(paper_id,field_name);
CREATE INDEX idx_job_recovery ON jobs(status,lease_until);

-- A single evidence view is used by graph, reference counts, and reverse queries.
CREATE VIEW effective_citation_assertions AS
SELECT a.id, COALESCE(src.canonical_paper_id,src.id) AS citing_paper_id,
       COALESCE(dst.canonical_paper_id,dst.id) AS cited_paper_id,
       a.source_kind, a.reference_entry_id, a.is_pinned, a.method, a.created_at
FROM citation_assertions a
JOIN papers src ON src.id=a.citing_paper_id
JOIN papers dst ON dst.id=a.cited_paper_id
LEFT JOIN reference_entries e ON e.id=a.reference_entry_id
LEFT JOIN extraction_runs r ON r.id=e.run_id
JOIN papers cs ON cs.id=COALESCE(src.canonical_paper_id,src.id)
JOIN papers ct ON ct.id=COALESCE(dst.canonical_paper_id,dst.id)
WHERE a.status='accepted' AND a.is_current=1 AND a.review_state='current'
  AND (a.source_kind <> 'pdf' OR r.is_active=1 OR a.is_pinned=1)
  AND cs.archived_at IS NULL AND ct.archived_at IS NULL
  AND cs.id <> ct.id;
CREATE VIEW citation_edges AS
SELECT citing_paper_id,cited_paper_id,COUNT(*) AS evidence_count,
       MAX(CASE WHEN source_kind IN ('pdf','manual') THEN 1 ELSE 0 END) AS has_local_evidence,
       MAX(CASE WHEN source_kind='external' THEN 1 ELSE 0 END) AS has_external_evidence
FROM effective_citation_assertions
GROUP BY citing_paper_id,cited_paper_id;

CREATE TRIGGER reference_entries_immutable BEFORE UPDATE ON reference_entries
BEGIN SELECT RAISE(ABORT,'reference entries are immutable'); END;
CREATE TRIGGER reference_entries_preserve BEFORE DELETE ON reference_entries
BEGIN SELECT RAISE(ABORT,'reference evidence cannot be deleted'); END;
CREATE TRIGGER pdf_content_immutable BEFORE UPDATE OF sha256,byte_size ON pdf_assets
BEGIN SELECT RAISE(ABORT,'PDF content identity is immutable'); END;
CREATE TRIGGER run_input_immutable BEFORE UPDATE OF asset_id,source_paper_id,parser_name,parser_version,
    config_json,config_hash,input_sha256,cache_key,base_run_id,output_uri,output_sha256 ON extraction_runs
BEGIN SELECT RAISE(ABORT,'extraction snapshot is immutable'); END;
CREATE TRIGGER claim_value_immutable BEFORE UPDATE OF paper_id,field_name,value_json,source_kind,run_id,
    reference_entry_id,source_key,source_url,is_manual,supersedes_id ON metadata_claims
BEGIN SELECT RAISE(ABORT,'metadata claim value is immutable'); END;
CREATE TRIGGER assertion_value_immutable BEFORE UPDATE OF citing_paper_id,cited_paper_id,reference_entry_id,
    source_kind,provider,provider_record_id,status,method,evidence_json,actor,supersedes_id ON citation_assertions
BEGIN SELECT RAISE(ABORT,'citation decision is immutable; append a replacement'); END;
