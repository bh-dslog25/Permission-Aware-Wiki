-- =============================================================================
-- Permission-Aware RAG Wiki: PostgreSQL Schema Reference (MVP)
-- NOTE: app.db (backend/app/db.py) là NGUỒN CHUẨN DUY NHẤT (Single Source of Truth)
-- cho schema và khởi tạo database (build_schema_sql).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- ---------------------------------------------------------------------------
-- 1. documents
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    document_id         TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    doc_type            TEXT NOT NULL DEFAULT 'policy',
    owner_unit          TEXT NOT NULL,
    scope               TEXT NOT NULL DEFAULT 'unit',
    classification      SMALLINT NOT NULL DEFAULT 3 CHECK (classification BETWEEN 0 AND 3),
    version             TEXT NOT NULL,
    valid_from          DATE,
    valid_to            DATE,
    status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'superseded', 'expired')),
    mandatory           BOOLEAN NOT NULL DEFAULT FALSE,
    approved            BOOLEAN NOT NULL DEFAULT FALSE,
    topic               TEXT,
    parent_conformance  TEXT NOT NULL DEFAULT 'unverified'
                        CHECK (parent_conformance IN ('confirmed', 'unverified', 'conflicting')),
    source_path         TEXT,
    content_hash        TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);
CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents (owner_unit);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents (status);
CREATE INDEX IF NOT EXISTS idx_documents_topic ON documents (topic);

-- ---------------------------------------------------------------------------
-- 2. document_relations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_relations (
    source_document_id  TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    target_document_id  TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    relation_type       TEXT NOT NULL CHECK (relation_type IN ('supersedes', 'amends')),
    PRIMARY KEY (source_document_id, target_document_id, relation_type),
    CHECK (source_document_id <> target_document_id)
);
CREATE INDEX IF NOT EXISTS idx_relations_target ON document_relations (target_document_id);

-- ---------------------------------------------------------------------------
-- 3. chunks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    section_path    TEXT,
    content         TEXT NOT NULL,
    embedding       vector(1024),
    content_tsv     tsvector,
    owner_unit      TEXT NOT NULL,
    classification  SMALLINT NOT NULL CHECK (classification BETWEEN 0 AND 3),
    valid_from      DATE,
    valid_to        DATE,
    status          TEXT NOT NULL CHECK (status IN ('draft', 'active', 'superseded', 'expired')),
    mandatory       BOOLEAN NOT NULL DEFAULT FALSE,
    approved        BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_filter
    ON chunks (owner_unit, classification, status, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (content_tsv);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- Triggers
CREATE OR REPLACE FUNCTION chunks_tsv_update() RETURNS trigger AS $$
BEGIN
    NEW.content_tsv := to_tsvector(
        'simple', unaccent(coalesce(NEW.section_path, '') || ' ' || NEW.content)
    );
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_chunks_tsv ON chunks;
CREATE TRIGGER trg_chunks_tsv BEFORE INSERT OR UPDATE OF content, section_path
    ON chunks FOR EACH ROW EXECUTE FUNCTION chunks_tsv_update();

CREATE OR REPLACE FUNCTION documents_sync_chunks() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    UPDATE chunks SET
        owner_unit = NEW.owner_unit,
        classification = NEW.classification,
        valid_from = NEW.valid_from,
        valid_to = NEW.valid_to,
        status = NEW.status,
        mandatory = NEW.mandatory,
        approved = NEW.approved
    WHERE document_id = NEW.document_id;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_documents_sync ON documents;
CREATE TRIGGER trg_documents_sync BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION documents_sync_chunks();

-- ---------------------------------------------------------------------------
-- 4. audit_log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id             TEXT NOT NULL,
    context_unit        TEXT,
    question_hash       TEXT,
    as_of_date          DATE,
    structured_query    JSONB,
    allowed_units       TEXT[],
    max_classification  SMALLINT,
    authz_decision      JSONB,
    retrieved_chunk_ids TEXT[],
    source_hashes       TEXT[],
    outcome             TEXT NOT NULL CHECK (outcome IN ('answered', 'abstained', 'denied', 'error')),
    abstain_reason      TEXT,
    latency_ms          INTEGER,
    resolution          JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log (user_id, ts DESC);

CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log là append-only: không được sửa hoặc xóa';
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_immutable ON audit_log;
CREATE TRIGGER trg_audit_immutable BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- ---------------------------------------------------------------------------
-- 5. conversations (conversation history scoped to a persona)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    title           TEXT NOT NULL,
    context_id      TEXT NOT NULL,
    messages        JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
    ON conversations (user_id, updated_at DESC);
