"""Kết nối PostgreSQL, schema 4 bảng MVP (documents, document_relations, chunks, audit_log) và health check.

Nguồn chuẩn duy nhất (Single Source of Truth) cho schema và khởi tạo database.
Ref: README Section 3.1 & MVP Architecture.

4 Bảng cho MVP:
  1. documents: metadata tài liệu (owner_unit, scope, classification rank 0-3, status, topic, parent_conformance...)
  2. document_relations: quan hệ phiên bản (supersedes, amends)
  3. chunks: chunk văn bản + vector embedding + denormalized metadata để lọc index
  4. audit_log: nhật ký truy vấn append-only

CLI:
    python -m app.db init     # tạo schema (idempotent)
    python -m app.db check    # kiểm tra PostgreSQL + OpenFGA
    python -m app.db reset    # XÓA và tạo lại schema (chỉ dev)
"""
from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator

import httpx
import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.config import get_settings

log = logging.getLogger(__name__)

# Độ mật (0..3): đồng bộ với org.yaml rank.
CLASSIFICATION_LEVELS: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
CLASSIFICATION_NAMES: dict[int, str] = {v: k for k, v in CLASSIFICATION_LEVELS.items()}
DOC_STATUSES = ("draft", "active", "superseded", "expired")
RELATION_TYPES = ("supersedes", "amends")
ALLOWED_DOC_TYPES = ("policy", "process", "guideline", "faq")

EXPECTED_TABLES = {"documents", "document_relations", "chunks", "audit_log"}


# -----------------------------------------------------------------------------
# DDL Schema
# -----------------------------------------------------------------------------
def build_schema_sql(embedding_dim: int) -> str:
    statuses = ", ".join(f"'{s}'" for s in DOC_STATUSES)
    relations = ", ".join(f"'{r}'" for r in RELATION_TYPES)
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- 1. documents
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
    status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ({statuses})),
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

-- 2. document_relations (quan hệ phiên bản: supersedes, amends)
CREATE TABLE IF NOT EXISTS document_relations (
    source_document_id  TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    target_document_id  TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    relation_type       TEXT NOT NULL CHECK (relation_type IN ({relations})),
    PRIMARY KEY (source_document_id, target_document_id, relation_type),
    CHECK (source_document_id <> target_document_id)
);
CREATE INDEX IF NOT EXISTS idx_relations_target ON document_relations (target_document_id);

-- 3. chunks
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    section_path    TEXT,
    content         TEXT NOT NULL,
    embedding       vector({embedding_dim}),
    content_tsv     tsvector,
    -- Metadata sao chép để lọc tại index
    owner_unit      TEXT NOT NULL,
    classification  SMALLINT NOT NULL CHECK (classification BETWEEN 0 AND 3),
    valid_from      DATE,
    valid_to        DATE,
    status          TEXT NOT NULL CHECK (status IN ({statuses})),
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

-- Điền tsvector (tiếng Việt: bỏ dấu + cấu hình 'simple')
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

-- Đồng bộ metadata từ documents xuống chunks khi document cập nhật
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

-- 4. audit_log (append-only)
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

-- Bất biến: audit_log chỉ cho INSERT
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log là append-only: không được sửa hoặc xóa';
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_immutable ON audit_log;
CREATE TRIGGER trg_audit_immutable BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
"""


RESET_SQL = """
DROP TABLE IF EXISTS audit_log CASCADE;
DROP TABLE IF EXISTS chunks CASCADE;
DROP TABLE IF EXISTS document_relations CASCADE;
DROP TABLE IF EXISTS documents CASCADE;
"""


# -----------------------------------------------------------------------------
# Connection Pool (psycopg)
# -----------------------------------------------------------------------------
_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        s = get_settings()
        _pool = ConnectionPool(
            s.database_url,
            min_size=s.db_pool_min,
            max_size=s.db_pool_max,
            configure=_configure,
            open=False,
        )
        _pool.open(wait=True, timeout=15)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn


# -----------------------------------------------------------------------------
# Schema Management
# -----------------------------------------------------------------------------
def init_schema(reset: bool = False) -> None:
    """Tạo schema PostgreSQL (idempotent)."""
    s = get_settings()
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        if reset:
            conn.execute(RESET_SQL)
        conn.execute(build_schema_sql(s.embedding_dim))
    log.info("Schema MVP đã sẵn sàng (embedding_dim=%s)", s.embedding_dim)


# -----------------------------------------------------------------------------
# Write Audit Log (psycopg)
# -----------------------------------------------------------------------------
def write_audit(
    conn: psycopg.Connection,
    user_id: str,
    *,
    context_unit: str | None = None,
    question_hash: str | None = None,
    as_of_date: date | None = None,
    structured_query: dict | None = None,
    allowed_units: list[str] | None = None,
    max_classification: int | None = None,
    authz_decision: dict | None = None,
    retrieved_chunk_ids: list[str] | None = None,
    source_hashes: list[str] | None = None,
    outcome: str = "answered",
    abstain_reason: str | None = None,
    latency_ms: int | None = None,
    resolution: dict | None = None,
) -> None:
    """Ghi nhận bản ghi append-only vào audit_log."""
    sql = """
        INSERT INTO audit_log (
            user_id, context_unit, question_hash, as_of_date, structured_query,
            allowed_units, max_classification, authz_decision, retrieved_chunk_ids,
            source_hashes, outcome, abstain_reason, latency_ms, resolution
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    conn.execute(
        sql,
        (
            user_id,
            context_unit,
            question_hash,
            as_of_date,
            Jsonb(structured_query) if structured_query is not None else None,
            allowed_units,
            max_classification,
            Jsonb(authz_decision) if authz_decision is not None else None,
            retrieved_chunk_ids,
            source_hashes,
            outcome,
            abstain_reason,
            latency_ms,
            Jsonb(resolution) if resolution is not None else None,
        ),
    )


class Database:
    """Thin adapter used by the RAG pipeline.

    It keeps retrieval code decoupled from psycopg while still using the shared
    connection pool and schema in this module.
    """

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_settings().database_url

    def open(self) -> None:
        get_pool()

    def close(self) -> None:
        close_pool()

    def fetch_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with get_pool().connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params or {})
                return list(cur.fetchall())

    def execute(self, sql: str, params: Any = None) -> None:
        with get_pool().connection() as conn:
            conn.execute(sql, params)


# -----------------------------------------------------------------------------
# Health Check
# -----------------------------------------------------------------------------
def check_db() -> dict:
    with get_conn() as conn:
        conn.execute("SELECT 1")
        exts = {r[0] for r in conn.execute("SELECT extname FROM pg_extension")}
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
        }
    missing_ext = {"vector", "unaccent"} - exts
    missing_tables = EXPECTED_TABLES - tables
    ok = not missing_ext and not missing_tables
    return {
        "ok": ok,
        "missing_extensions": sorted(missing_ext),
        "missing_tables": sorted(missing_tables),
    }


def check_openfga() -> dict:
    s = get_settings()
    base = s.openfga_api_url.rstrip("/")
    try:
        r = httpx.get(f"{base}/healthz", timeout=5)
        healthy = r.status_code == 200
        stores = httpx.get(f"{base}/stores", timeout=5).json().get("stores", [])
    except httpx.HTTPError as e:
        return {"ok": False, "error": str(e)}
    configured = bool(s.openfga_store_id)
    store_found = (
        any(st["id"] == s.openfga_store_id for st in stores) if configured else None
    )
    return {
        "ok": healthy,
        "store_id_configured": configured,
        "store_found": store_found,
        "stores": len(stores),
    }


def check_all() -> bool:
    results = {}
    try:
        results["postgres"] = check_db()
    except Exception as e:  # noqa: BLE001
        results["postgres"] = {"ok": False, "error": str(e)}
    results["openfga"] = check_openfga()
    for name, res in results.items():
        print(f"[{'OK' if res['ok'] else 'FAIL'}] {name}: {res}")
    return all(r["ok"] for r in results.values())


def main(argv: list[str]) -> int:
    logging.basicConfig(level=get_settings().log_level)
    cmd = argv[1] if len(argv) > 1 else "check"
    try:
        if cmd == "init":
            init_schema()
            return 0 if check_all() else 1
        if cmd == "reset":
            if get_settings().app_env != "dev":
                print("reset chỉ được phép khi APP_ENV=dev")
                return 2
            init_schema(reset=True)
            return 0 if check_all() else 1
        if cmd == "check":
            return 0 if check_all() else 1
        print(__doc__)
        return 2
    finally:
        close_pool()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
