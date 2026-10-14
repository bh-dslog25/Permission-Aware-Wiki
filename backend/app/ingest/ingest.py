"""Nạp tài liệu và tạo chỉ mục cho MVP.

Quy trình:
    Markdown + front matter ─▶ validate (default-deny) ─▶ chunk theo Điều/Khoản
        ─▶ embedding (BGE-M3 hoặc Fake) ─▶ pgvector (1 transaction / tài liệu)

Nguyên tắc:
  - Fail-closed: tài liệu lỗi không bao giờ nạp vào DB. Nếu bản cũ đã có trong DB, đặt approved=false.
  - Bỏ qua tài liệu không đổi (so khớp content_hash).
  - Tối giản: không advisory lock, không quarantine table, không search CLI không phân quyền.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import math
import random
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol, Sequence

import yaml

from app.config import Settings, get_settings
from app.db import (
    ALLOWED_DOC_TYPES,
    CLASSIFICATION_LEVELS,
    DOC_STATUSES,
    get_conn,
    init_schema,
)

log = logging.getLogger("ingest")

MAX_FILE_BYTES = 2_000_000

REQUIRED_FIELDS = (
    "document_id", "title", "owner_unit", "scope", "doc_type", "version",
    "effective_from", "status", "classification", "mandatory",
)
OPTIONAL_FIELDS = ("logical_document_id", "effective_to", "topic", "parent_conformance", "supersedes", "amends")

ALLOWED_STATUS = frozenset(DOC_STATUSES)
ALLOWED_DOC_TYPES_SET = frozenset(ALLOWED_DOC_TYPES)
ALLOWED_CONFORMANCE = frozenset({"confirmed", "unverified", "conflicting"})

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
ARTICLE_RE = re.compile(r"^##[ \t]+Điều[ \t]+(\d{1,3})[ \t]*[.:–-][ \t]*(\S.*?)[ \t]*$", re.IGNORECASE)
CLAUSE_RE = re.compile(r"^(\d{1,3})[.)][ \t]+\S")
HEADING_RE = re.compile(r"^(#{1,2})[ \t]+\S")


# ════════════════════════════════════════════════════════════════════════════
# 1. Kiểu dữ liệu tối giản
# ════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Issue:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class ParsedDocument:
    path: Path
    source_path: str
    content_hash: str = ""
    doc_id: str = ""
    meta: dict[str, Any] | None = None
    chunks: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


# ════════════════════════════════════════════════════════════════════════════
# 2. Tham chiếu tổ chức (org.yaml)
# ════════════════════════════════════════════════════════════════════════════
def load_org_reference(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    units = data["units"]
    return {
        "unit_ids": frozenset(u["id"] for u in units),
        "unit_types": {u["id"]: u["type"] for u in units},
        "classification_rank": {c["id"]: int(c["rank"]) for c in data.get("classifications", [])}
        or CLASSIFICATION_LEVELS,
    }


# ════════════════════════════════════════════════════════════════════════════
# 3. Parse front matter & validate metadata (default-deny)
# ════════════════════════════════════════════════════════════════════════════
def normalize_text(s: str) -> str:
    s = s.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", s)


def split_front_matter(text: str) -> tuple[dict[str, Any] | None, str, int, list[Issue]]:
    m = _FM_RE.match(text)
    if not m:
        return None, text, 1, [Issue("E_NO_FRONT_MATTER", "Không tìm thấy front matter '---' ở đầu file")]
    body_first_line = text[: m.end()].count("\n") + 1
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        return None, text[m.end():], body_first_line, [Issue("E_YAML_PARSE", f"Front matter không hợp lệ: {exc}")]
    if not isinstance(data, dict):
        return None, text[m.end():], body_first_line, [Issue("E_YAML_PARSE", "Front matter phải là mapping")]
    return data, text[m.end():], body_first_line, []


def _parse_date(value: Any, name: str, issues: list[Issue]) -> date | None:
    if isinstance(value, datetime):
        issues.append(Issue("E_BAD_DATE", f"{name}: chỉ nhận ngày (YYYY-MM-DD), không nhận ngày-giờ"))
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    issues.append(Issue("E_BAD_DATE", f"{name}: phải là ngày ISO YYYY-MM-DD, nhận được {value!r}"))
    return None


def _ref_list(raw: Any, name: str, issues: list[Issue]) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) and _ID_RE.match(x) for x in raw):
        issues.append(Issue("E_BAD_REF", f"{name}: phải là danh sách document_id hợp lệ"))
        return []
    return list(raw)


def validate_meta(raw: dict[str, Any], stem: str, org: dict[str, Any]) -> tuple[dict[str, Any] | None, list[Issue], list[Issue]]:
    issues: list[Issue] = []
    warnings: list[Issue] = []

    for name in REQUIRED_FIELDS:
        v = raw.get(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            issues.append(Issue("E_MISSING_FIELD", f"Thiếu trường bắt buộc: {name}"))
    if issues:
        return None, issues, warnings

    def s(name: str) -> str:
        return str(raw[name]).strip()

    # Document ID: ưu tiên front matter, dự phòng bằng stem file
    doc_id = s("document_id") or stem
    if not _ID_RE.match(doc_id) or len(doc_id) > 80:
        issues.append(Issue("E_BAD_ID", f"document_id không hợp lệ: {doc_id!r}"))
    elif doc_id != stem:
        issues.append(Issue("E_ID_MISMATCH", f"document_id '{doc_id}' phải trùng tên file '{stem}.md'"))

    if s("owner_unit") not in org["unit_ids"]:
        issues.append(Issue("E_UNKNOWN_UNIT", f"owner_unit='{s('owner_unit')}' không có trong org.yaml"))
    elif s("scope") != org["unit_types"].get(s("owner_unit")):
        issues.append(Issue(
            "E_SCOPE_MISMATCH",
            f"scope='{s('scope')}' không khớp loại đơn vị '{s('owner_unit')}' ({org['unit_types'].get(s('owner_unit'))})",
        ))

    if s("doc_type") not in ALLOWED_DOC_TYPES_SET:
        issues.append(Issue("E_BAD_ENUM", f"doc_type '{s('doc_type')}' không thuộc {sorted(ALLOWED_DOC_TYPES_SET)}"))
    if s("status") not in ALLOWED_STATUS:
        issues.append(Issue("E_BAD_ENUM", f"status '{s('status')}' không thuộc {sorted(ALLOWED_STATUS)}"))

    # Classification: chuyển tên chuỗi sang số nguyên 0..3
    class_str = s("classification")
    if class_str not in org["classification_rank"]:
        issues.append(Issue("E_BAD_ENUM", f"classification '{class_str}' không hợp lệ"))
        class_rank = 3
    else:
        class_rank = org["classification_rank"][class_str]

    if not isinstance(raw["mandatory"], bool):
        issues.append(Issue("E_BAD_TYPE", f"mandatory phải là boolean, nhận được {raw['mandatory']!r}"))

    eff_from = _parse_date(raw["effective_from"], "effective_from", issues)
    eff_to = _parse_date(raw["effective_to"], "effective_to", issues) if raw.get("effective_to") is not None else None
    if eff_from and eff_to and eff_to < eff_from:
        issues.append(Issue("E_DATE_RANGE", f"effective_to ({eff_to}) sớm hơn effective_from ({eff_from})"))

    supersedes = _ref_list(raw.get("supersedes"), "supersedes", issues)
    amends = _ref_list(raw.get("amends"), "amends", issues)

    # Validate topic & parent_conformance (cần cho resolution ở Bước 6)
    topic = raw.get("topic")
    if topic is not None and not isinstance(topic, str):
        issues.append(Issue("E_BAD_TYPE", "topic phải là chuỗi"))
    topic_str = topic.strip() if isinstance(topic, str) and topic.strip() else None

    parent_conformance = raw.get("parent_conformance", "unverified")
    if parent_conformance not in ALLOWED_CONFORMANCE:
        issues.append(Issue("E_BAD_ENUM", f"parent_conformance '{parent_conformance}' không hợp lệ"))

    if issues:
        return None, issues, warnings

    return (
        {
            "document_id": doc_id,
            "title": s("title"),
            "owner_unit": s("owner_unit"),
            "scope": s("scope"),
            "doc_type": s("doc_type"),
            "classification": class_rank,
            "version": s("version"),
            "effective_from": eff_from,
            "effective_to": eff_to,
            "status": s("status"),
            "mandatory": raw["mandatory"],
            "topic": topic_str,
            "parent_conformance": parent_conformance,
            "supersedes": supersedes,
            "amends": amends,
        },
        issues,
        warnings,
    )


def validate_status(meta: dict[str, Any], today: date) -> tuple[list[Issue], list[Issue]]:
    issues: list[Issue] = []
    warnings: list[Issue] = []
    status = meta["status"]
    eff_from = meta.get("effective_from")
    eff_to = meta.get("effective_to")

    if status in {"superseded", "expired"}:
        if eff_to is None:
            issues.append(Issue("E_STATUS_INCONSISTENT", f"status '{status}' bắt buộc có effective_to"))
        elif eff_to >= today:
            warnings.append(Issue("W_STATUS_EARLY", f"status '{status}' nhưng effective_to ({eff_to}) chưa tới"))
    if status == "active":
        if eff_to is not None and eff_to < today:
            issues.append(Issue("E_STATUS_INCONSISTENT", f"status 'active' nhưng đã hết hiệu lực từ {eff_to}"))
        if eff_from and eff_from > today:
            warnings.append(Issue("W_FUTURE_EFFECTIVE", f"status 'active' nhưng chỉ có hiệu lực từ {eff_from}"))
    return issues, warnings


# ════════════════════════════════════════════════════════════════════════════
# 4. Chunking theo Điều / Khoản
# ════════════════════════════════════════════════════════════════════════════
def _trim_blank(lines: list[str]) -> list[str]:
    out: list[str] = []
    for t in lines:
        if not t.strip():
            if out and out[-1]:
                out.append("")
            continue
        out.append(t)
    while out and not out[-1]:
        out.pop()
    return out


def _chunk_article(
    doc_id: str,
    art_num: int,
    art_title: str,
    lines: list[str],
    max_chars: int,
) -> list[dict[str, Any]]:
    intro: list[str] = []
    clauses: list[tuple[int, list[str]]] = []
    for t in lines:
        m = CLAUSE_RE.match(t)
        if m:
            clauses.append((int(m.group(1)), [t]))
        elif clauses:
            clauses[-1][1].append(t)
        else:
            intro.append(t)

    full_text = "\n".join(lines).strip()
    if len(full_text) <= max_chars or len(clauses) < 2:
        return [{
            "chunk_id": f"{doc_id}#d{art_num:02d}",
            "section_path": f"Điều {art_num}. {art_title}",
            "text": full_text,
        }]

    # Tách theo Khoản khi Điều vượt quá max_chars
    chunks = []
    for i, (c_num, c_lines) in enumerate(clauses, start=1):
        content = "\n".join(c_lines).strip()
        chunks.append({
            "chunk_id": f"{doc_id}#d{art_num:02d}.k{c_num}",
            "section_path": f"Điều {art_num} > Khoản {c_num}",
            "text": content,
        })
    return chunks


def chunk_body(
    doc_id: str,
    body: str,
    max_chars: int,
) -> tuple[list[dict[str, Any]], list[Issue], list[Issue]]:
    errors: list[Issue] = []
    warnings: list[Issue] = []
    articles: list[tuple[int, str, list[str]]] = []
    curr_art: tuple[int, str, list[str]] | None = None

    for line in body.split("\n"):
        m = ARTICLE_RE.match(line)
        if m:
            curr_art = (int(m.group(1)), m.group(2).strip(), [])
            articles.append(curr_art)
            continue
        h = HEADING_RE.match(line)
        if h and h.group(1) == "#" and not articles:
            continue  # H1 document title
        if curr_art is not None:
            curr_art[2].append(line.rstrip())

    if not articles:
        errors.append(Issue("E_NO_ARTICLES", "Không có mục '## Điều N. Tên điều' nào"))
        return [], errors, warnings

    chunks: list[dict[str, Any]] = []
    seen: set[int] = set()
    for art_num, art_title, lines in articles:
        if art_num in seen:
            errors.append(Issue("E_DUP_ARTICLE", f"Điều {art_num} bị trùng lặp"))
            continue
        seen.add(art_num)
        trimmed = _trim_blank(lines)
        if not trimmed:
            errors.append(Issue("E_EMPTY_ARTICLE", f"Điều {art_num} không có nội dung"))
            continue
        chunks.extend(_chunk_article(doc_id, art_num, art_title, trimmed, max_chars))

    for idx, c in enumerate(chunks):
        c["chunk_index"] = idx
    return chunks, errors, warnings


# ════════════════════════════════════════════════════════════════════════════
# 5. Parse tài liệu và kiểm tra liên tài liệu
# ════════════════════════════════════════════════════════════════════════════
def parse_document(path: Path, root: Path, org: dict[str, Any], *, max_chars: int, today: date) -> ParsedDocument:
    pd = ParsedDocument(path=path, source_path=path.relative_to(root).as_posix(), doc_id=path.stem)
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        pd.issues.append(Issue("E_TOO_LARGE", f"File vượt giới hạn {MAX_FILE_BYTES} bytes"))
        return pd
    try:
        text = normalize_text(raw.decode("utf-8"))
    except UnicodeDecodeError:
        pd.issues.append(Issue("E_ENCODING", "File không phải UTF-8"))
        return pd
    pd.content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    fm, body, _, fm_issues = split_front_matter(text)
    pd.issues += fm_issues
    if fm is None:
        return pd

    meta, m_issues, m_warn = validate_meta(fm, path.stem, org)
    pd.issues += m_issues
    pd.warnings += m_warn
    if meta:
        pd.doc_id = meta["document_id"]
        s_issues, s_warn = validate_status(meta, today)
        pd.issues += s_issues
        pd.warnings += s_warn

    chunks, c_issues, c_warn = chunk_body(pd.doc_id, body, max_chars)
    pd.issues += c_issues
    pd.warnings += c_warn
    pd.meta, pd.chunks = meta, chunks
    return pd


def validate_corpus(docs: list[ParsedDocument], today: date) -> None:
    by_id: dict[str, list[ParsedDocument]] = defaultdict(list)
    for d in docs:
        if d.doc_id:
            by_id[d.doc_id].append(d)

    for did, group in by_id.items():
        if len(group) > 1:
            where = ", ".join(sorted(g.source_path for g in group))
            for g in group:
                g.issues.append(Issue("E_DUP_ID", f"document_id '{did}' bị trùng ở: {where}"))

    known = set(by_id)
    for d in docs:
        if not d.meta:
            continue
        for ref in (*d.meta.get("supersedes", []), *d.meta.get("amends", [])):
            if ref == d.doc_id:
                d.issues.append(Issue("E_SELF_REF", "Tài liệu không thể supersedes/amends chính nó"))
            elif ref not in known:
                d.issues.append(Issue("E_DANGLING_REF", f"Tham chiếu tới document_id không tồn tại: '{ref}'"))


def scan_documents(settings: Settings, *, today: date | None = None, max_chars: int | None = None) -> list[ParsedDocument]:
    root = settings.documents_dir
    if not root.is_dir():
        raise FileNotFoundError(f"Không tìm thấy thư mục tài liệu: {root}")
    org = load_org_reference(settings.org_file)
    today = today or date.today()
    files = sorted(p for p in root.rglob("*.md") if not p.name.startswith("_") and p.name.lower() != "readme.md")
    docs = [parse_document(p, root, org, max_chars=max_chars or settings.max_chunk_chars, today=today) for p in files]
    validate_corpus(docs, today)
    return docs


# ════════════════════════════════════════════════════════════════════════════
# 6. Embedding
# ════════════════════════════════════════════════════════════════════════════
class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class BgeM3Embedder:
    def __init__(self, model_name: str, dim: int, batch_size: int, device: str | None):
        self.name, self.dim = model_name, dim
        self._batch, self._device = batch_size, device
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            log.info("Đang tải model embedding %s ...", self.name)
            model = SentenceTransformer(self.name, device=self._device)
            model.max_seq_length = 1024
            actual = model.get_sentence_embedding_dimension()
            if actual != self.dim:
                raise RuntimeError(f"Model trả về {actual} chiều nhưng EMBEDDING_DIM={self.dim}")
            self._model = model
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vecs = self._load().encode(list(texts), batch_size=self._batch, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]


class FakeEmbedder:
    def __init__(self, dim: int):
        self.name, self.dim = "fake-hash-embedder", dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for t in texts:
            rng = random.Random(int.from_bytes(hashlib.sha256(t.encode("utf-8")).digest()[:8], "big"))
            v = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


def embedding_text(meta: dict[str, Any], chunk: dict[str, Any]) -> str:
    return f"{meta['title']}\n{chunk['section_path']}\n{chunk['text']}"


# ════════════════════════════════════════════════════════════════════════════
# 7. Persistence (ghi PostgreSQL trong 1 transaction)
# ════════════════════════════════════════════════════════════════════════════
_UPSERT_DOC = """
INSERT INTO documents (
    document_id, title, doc_type, owner_unit, scope, classification,
    version, valid_from, valid_to, status, mandatory, approved,
    topic, parent_conformance, source_path, content_hash
)
VALUES (
    %(document_id)s, %(title)s, %(doc_type)s, %(owner_unit)s, %(scope)s, %(classification)s,
    %(version)s, %(valid_from)s, %(valid_to)s, %(status)s, %(mandatory)s, %(approved)s,
    %(topic)s, %(parent_conformance)s, %(source_path)s, %(content_hash)s
)
ON CONFLICT (document_id) DO UPDATE SET
    title = EXCLUDED.title,
    doc_type = EXCLUDED.doc_type,
    owner_unit = EXCLUDED.owner_unit,
    scope = EXCLUDED.scope,
    classification = EXCLUDED.classification,
    version = EXCLUDED.version,
    valid_from = EXCLUDED.valid_from,
    valid_to = EXCLUDED.valid_to,
    status = EXCLUDED.status,
    mandatory = EXCLUDED.mandatory,
    approved = EXCLUDED.approved,
    topic = EXCLUDED.topic,
    parent_conformance = EXCLUDED.parent_conformance,
    source_path = EXCLUDED.source_path,
    content_hash = EXCLUDED.content_hash;
"""

_INSERT_RELATION = """
INSERT INTO document_relations (source_document_id, target_document_id, relation_type)
VALUES (%(source)s, %(target)s, %(rel)s)
ON CONFLICT DO NOTHING;
"""

_INSERT_CHUNK = """
INSERT INTO chunks (
    chunk_id, document_id, chunk_index, section_path, content, embedding,
    owner_unit, classification, valid_from, valid_to, status, mandatory, approved
)
VALUES (
    %(chunk_id)s, %(document_id)s, %(chunk_index)s, %(section_path)s, %(content)s, %(embedding)s,
    %(owner_unit)s, %(classification)s, %(valid_from)s, %(valid_to)s, %(status)s, %(mandatory)s, %(approved)s
);
"""


def _persist_document(
    conn: Any,
    pd: ParsedDocument,
    vectors: list[list[float]],
) -> None:
    from pgvector import Vector

    m = pd.meta
    assert m is not None
    doc_id = m["document_id"]
    # "approved" means the metadata/content is owner-approved for retrieval.
    # Historical versions still need to be retrievable for as-of questions; draft is excluded.
    is_approved = m["status"] != "draft"

    doc_row = {
        "document_id": doc_id,
        "title": m["title"],
        "doc_type": m["doc_type"],
        "owner_unit": m["owner_unit"],
        "scope": m["scope"],
        "classification": m["classification"],
        "version": m["version"],
        "valid_from": m["effective_from"],
        "valid_to": m["effective_to"],
        "status": m["status"],
        "mandatory": m["mandatory"],
        "approved": is_approved,
        "topic": m["topic"],
        "parent_conformance": m["parent_conformance"],
        "source_path": pd.source_path,
        "content_hash": pd.content_hash,
    }

    chunk_rows = [
        {
            "chunk_id": c["chunk_id"],
            "document_id": doc_id,
            "chunk_index": c["chunk_index"],
            "section_path": c["section_path"],
            "content": c["text"],
            "embedding": Vector(vec),
            "owner_unit": m["owner_unit"],
            "classification": m["classification"],
            "valid_from": m["effective_from"],
            "valid_to": m["effective_to"],
            "status": m["status"],
            "mandatory": m["mandatory"],
            "approved": is_approved,
        }
        for c, vec in zip(pd.chunks, vectors)
    ]

    with conn.transaction():
        # Xóa bản cũ của document nếu khác document_id nhưng cùng file
        conn.execute("DELETE FROM documents WHERE source_path = %s AND document_id <> %s", (pd.source_path, doc_id))
        conn.execute(_UPSERT_DOC, doc_row)

        # Thay thế toàn bộ chunk của tài liệu này
        conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
        with conn.cursor() as cur:
            cur.executemany(_INSERT_CHUNK, chunk_rows)


def _persist_relations(conn: Any, docs: Sequence[ParsedDocument]) -> None:
    """Cập nhật quan hệ supersedes / amends sau khi TẤT CẢ documents đã tồn tại trong DB."""
    with conn.transaction():
        for d in docs:
            m = d.meta
            if not m:
                continue
            doc_id = m["document_id"]
            conn.execute("DELETE FROM document_relations WHERE source_document_id = %s", (doc_id,))
            for sup in m.get("supersedes", []):
                conn.execute(_INSERT_RELATION, {"source": doc_id, "target": sup, "rel": "supersedes"})
            for amd in m.get("amends", []):
                conn.execute(_INSERT_RELATION, {"source": doc_id, "target": amd, "rel": "amends"})


# ════════════════════════════════════════════════════════════════════════════
# 8. Điều phối Ingest
# ════════════════════════════════════════════════════════════════════════════
def run_ingest(
    settings: Settings,
    *,
    use_fake: bool = False,
    dry_run: bool = False,
    force: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    docs = scan_documents(settings, today=today)
    accepted = [d for d in docs if d.ok]
    rejected = [d for d in docs if not d.ok]

    for d in rejected:
        log.error("REJECT %s: %s", d.source_path, "; ".join(f"{i.code}" for i in d.issues))
    for d in docs:
        for w in d.warnings:
            log.warning("WARN  %s: [%s] %s", d.source_path, w.code, w.message)

    stats = {
        "scanned": len(docs),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "skipped_unchanged": 0,
        "ingested": 0,
        "chunks_written": 0,
        "duration_s": 0.0,
    }

    if dry_run:
        stats["duration_s"] = round(time.monotonic() - started, 2)
        return stats

    embedder: Embedder = (
        FakeEmbedder(settings.embedding_dim)
        if use_fake
        else BgeM3Embedder(settings.embedding_model, settings.embedding_dim, settings.embedding_batch_size, settings.embedding_device)
    )

    with get_conn() as conn:
        init_schema()

        # Fail-closed: vô hiệu hóa tài liệu lỗi đã từng được nạp
        for d in rejected:
            with conn.transaction():
                conn.execute("UPDATE documents SET approved = FALSE WHERE document_id = %s", (d.doc_id,))

        # Lọc tài liệu cần nạp (bỏ qua nếu content_hash không đổi)
        existing_hashes = dict(conn.execute("SELECT document_id, content_hash FROM documents").fetchall())

        pending: list[ParsedDocument] = []
        for d in accepted:
            if not force and existing_hashes.get(d.doc_id) == d.content_hash:
                stats["skipped_unchanged"] += 1
            else:
                pending.append(d)

        if pending:
            texts = [embedding_text(d.meta, c) for d in pending for c in d.chunks]  # type: ignore[arg-type]
            log.info("Embedding %d chunk từ %d tài liệu ...", len(texts), len(pending))
            vectors = embedder.embed(texts)
            pos = 0
            for d in pending:
                n = len(d.chunks)
                _persist_document(conn, d, vectors[pos:pos + n])
                pos += n
                stats["ingested"] += 1
                stats["chunks_written"] += n
                log.info("OK    %s (%d chunk)", d.source_path, n)

            _persist_relations(conn, pending)

    stats["duration_s"] = round(time.monotonic() - started, 2)
    return stats


# ════════════════════════════════════════════════════════════════════════════
# 9. CLI tối giản
# ════════════════════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest tài liệu vào PostgreSQL pgvector")
    parser.add_argument("cmd", choices=["run"], default="run", nargs="?", help="Lệnh thực thi")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ kiểm tra và chunk, không ghi DB")
    parser.add_argument("--force", action="store_true", help="Nạp lại kể cả tài liệu không đổi")
    parser.add_argument("--fake", action="store_true", help="Dùng FakeEmbedder để kiểm thử nhanh")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()

    stats = run_ingest(settings, use_fake=args.fake, dry_run=args.dry_run, force=args.force)
    log.info("Tổng kết: %s", json.dumps(stats, ensure_ascii=False))
    return 1 if stats["rejected"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
