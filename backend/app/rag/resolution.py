"""Step 5 — Temporal & policy resolution (rule-based, KHÔNG dùng LLM).

Luật cố định, áp dụng theo thứ tự:
  0. Chỉ xét bằng chứng thuộc đơn vị nằm trong chuỗi ngữ cảnh của người hỏi.
  1. Bỏ bản draft / chưa hiệu lực / hết hiệu lực tại thời điểm T.
  2. Bỏ bản đã bị bản khác (đang hiệu lực tại T) thay thế.
  3. Có bản thay thế/bổ sung mà user không được xem  → TỪ CHỐI (chuyển Owner).
  4. Cấp trên `mandatory` là sàn/trần (baseline); cấp cụ thể nhất thắng (controlling);
     cấp trên không bắt buộc bị ghi đè.
  5. Xung đột không giải được / metadata mâu thuẫn → TỪ CHỐI, không đoán.

Nhóm cạnh tranh = các tài liệu cùng `scope` (phạm vi nghiệp vụ; fallback document_id).
Module thuần: không I/O, dễ unit test.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Collection, Sequence

from .retrieval import Candidate

logger = logging.getLogger(__name__)


class AbstainCode(str, Enum):
    NO_EVIDENCE = "no_evidence"
    OUT_OF_SCOPE = "out_of_scope"
    HIDDEN_SUCCESSOR = "hidden_successor"
    UNRESOLVED_CONFLICT = "unresolved_conflict"
    METADATA_INCONSISTENT = "metadata_inconsistent"
    AUTHZ_ERROR = "authz_error"
    RETRIEVAL_ERROR = "retrieval_error"
    LLM_ERROR = "llm_error"
    INVALID_OUTPUT = "invalid_output"
    NO_CITATION = "no_citation"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


ROLE_BASELINE = "baseline"        # quy định cấp trên bắt buộc: sàn/trần
ROLE_CONTROLLING = "controlling"  # quy định cụ thể nhất áp dụng cho người hỏi
ROLE_FALLBACK = "fallback"        # cấp trên không bắt buộc, chỉ giữ khi keep_overridden_context
_ROLE_ORDER = {ROLE_BASELINE: 0, ROLE_CONTROLLING: 1, ROLE_FALLBACK: 2}


@dataclass(frozen=True)
class DroppedVersion:
    document_id: str
    reason: str


@dataclass
class ResolvedEvidence:
    candidate: Candidate
    role: str
    group: str
    is_amendment: bool = False


@dataclass
class Resolution:
    evidences: list[ResolvedEvidence] = field(default_factory=list)
    dropped: list[DroppedVersion] = field(default_factory=list)
    abstain_code: AbstainCode | None = None
    abstain_detail: str = ""
    controlling_unit_id: str | None = None
    effective_at: date | None = None
    conflict_unit_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.abstain_code is None and bool(self.evidences)


# --------------------------------------------------------------------------- #
@dataclass
class _GroupResult:
    key: str
    evidences: list[ResolvedEvidence] = field(default_factory=list)
    dropped: list[DroppedVersion] = field(default_factory=list)
    code: AbstainCode | None = None
    detail: str = ""
    conflict_units: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    controlling_unit_id: str | None = None
    effective_at: date | None = None

    def fail(self, code: AbstainCode, detail: str, units: Sequence[str] = ()) -> "_GroupResult":
        self.code, self.detail = code, detail
        self.conflict_units = list(dict.fromkeys(units))
        self.evidences = []
        return self


def group_key(c: Candidate) -> str:
    return c.topic or c.scope or c.document_id


def _temporal_verdict(c: Candidate, as_of: date) -> tuple[str, str]:
    """-> ('ok'|'reject'|'inconsistent', reason)."""
    if c.doc_status == "draft":
        return "reject", "draft"
    if c.valid_from is None:
        return "inconsistent", "missing_valid_from"
    if c.doc_status not in ("active", "superseded", "expired"):
        return "reject", f"status_{c.doc_status or 'unknown'}"
    if c.valid_from > as_of:
        return "reject", "not_yet_effective"
    if c.valid_to is not None and c.valid_to < as_of:
        return "reject", "expired_at_as_of"
    if c.doc_status in ("superseded", "expired") and c.valid_to is None:
        return "inconsistent", "status_without_valid_to"
    return "ok", ""


def _resolve_group(
    key: str,
    cands: list[Candidate],
    *,
    rank: dict[str, int],
    chain: Sequence[str],
    as_of: date,
    hidden: Collection[str],
    keep_overridden: bool,
) -> _GroupResult:
    res = _GroupResult(key)

    # Gom theo document_id (MVP: moi document_id = mot version).
    chunks_by_version: dict[str, list[Candidate]] = {}
    for c in cands:
        chunks_by_version.setdefault(c.document_id, []).append(c)
    meta = {vid: chs[0] for vid, chs in chunks_by_version.items()}

    # 0 + 1. Pham vi ap dung va hieu luc.
    kept: dict[str, Candidate] = {}
    for vid, m in meta.items():
        if m.owner_unit_id not in rank:
            res.dropped.append(DroppedVersion(vid, "out_of_applicable_scope"))
            continue
        verdict, reason = _temporal_verdict(m, as_of)
        if verdict == "inconsistent":
            return res.fail(AbstainCode.METADATA_INCONSISTENT, f"{vid}: {reason}", [m.owner_unit_id])
        if verdict == "reject":
            res.dropped.append(DroppedVersion(vid, reason))
            continue
        kept[vid] = m

    # 2. Ban bi thay the boi ban khac cung dang hieu luc tai T.
    superseded = {vid for vid, m in kept.items() if m.superseded_by in kept}
    for vid in superseded:
        res.dropped.append(DroppedVersion(vid, "superseded"))
        del kept[vid]

    if not kept:
        return res.fail(AbstainCode.OUT_OF_SCOPE, "no applicable effective version")

    # 3. Bản thay thế/bổ sung có hiệu lực nhưng user không xem được.
    blocked = [vid for vid in kept if vid in hidden]
    if blocked:
        return res.fail(
            AbstainCode.HIDDEN_SUCCESSOR,
            "newer version not visible to user",
            [kept[v].owner_unit_id for v in blocked],
        )

    # 4a. Cụm "amends": bản gốc + các bản bổ sung được coi là một quy định.
    parent = {vid: vid for vid in kept}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for vid, m in kept.items():
        if m.amended_by in kept:
            parent[find(vid)] = find(m.amended_by)  # type: ignore[index]
    clusters: dict[str, list[str]] = {}
    for vid in kept:
        clusters.setdefault(find(vid), []).append(vid)

    # Hai bản của cùng một tài liệu cùng hiệu lực mà không liên kết thay thế/bổ sung → mơ hồ.
    roots_by_doc: dict[str, set[str]] = {}
    for root, vids in clusters.items():
        for v in vids:
            roots_by_doc.setdefault(kept[v].document_id, set()).add(root)
    for doc_id, roots in roots_by_doc.items():
        if len(roots) > 1:
            return res.fail(
                AbstainCode.UNRESOLVED_CONFLICT,
                f"overlapping versions of {doc_id}",
                [m.owner_unit_id for m in kept.values() if m.document_id == doc_id],
            )

    def c_rank(root: str) -> int:
        return max(rank[kept[v].owner_unit_id] for v in clusters[root])

    def c_doc_type(root: str) -> str:
        base = [v for v in clusters[root] if kept[v].amended_by not in kept]
        return (kept[(base or clusters[root])[0]].doc_type) or ""

    # 4b. Cụ thể nhất thắng; cùng cấp + cùng loại tài liệu mà khác tài liệu → xung đột.
    leaf_rank = max(c_rank(r) for r in clusters)
    leaf_roots = [r for r in clusters if c_rank(r) == leaf_rank]
    by_type: dict[str, list[str]] = {}
    for r in leaf_roots:
        by_type.setdefault(c_doc_type(r), []).append(r)
    for roots in by_type.values():
        if len(roots) > 1:
            return res.fail(
                AbstainCode.UNRESOLVED_CONFLICT,
                "multiple equally-specific documents",
                [kept[v].owner_unit_id for r in roots for v in clusters[r]],
            )

    # 4c. Gán vai trò.
    role_of_version: dict[str, str] = {}
    for root, vids in clusters.items():
        if c_rank(root) == leaf_rank:
            role = ROLE_CONTROLLING
        elif any(kept[v].mandatory for v in vids):
            role = ROLE_BASELINE
        elif keep_overridden:
            role = ROLE_FALLBACK
        else:
            for v in vids:
                res.dropped.append(DroppedVersion(v, "overridden_by_more_specific"))
            continue
        for v in vids:
            role_of_version[v] = role

    # Giữ thứ tự xếp hạng gốc của chunk; sort ổn định theo vai trò.
    evs: list[ResolvedEvidence] = []
    for c in cands:
        role = role_of_version.get(c.document_id)
        if role:
            evs.append(ResolvedEvidence(c, role, key, is_amendment=c.amended_by in kept))
    evs.sort(key=lambda e: (_ROLE_ORDER[e.role], rank[e.candidate.owner_unit_id] if e.role == ROLE_BASELINE else 0))
    res.evidences = evs

    controlling = [v for v, r in role_of_version.items() if r == ROLE_CONTROLLING]
    res.controlling_unit_id = chain[leaf_rank]
    res.effective_at = max(kept[v].valid_from for v in controlling if kept[v].valid_from)  # type: ignore[type-var]
    for v in controlling:
        m = kept[v]
        if m.amended_by and m.amended_by not in kept:
            res.notes.append(f"amendment {v} thieu ban goc trong bang chung")
    return res


def resolve(
    candidates: Sequence[Candidate],
    *,
    unit_chain: Sequence[str],
    as_of: date,
    hidden_successor_ids: Collection[str] = frozenset(),
    keep_overridden_context: bool = False,
) -> Resolution:
    """Áp dụng luật giải quyết lên tập bằng chứng đã được cấp quyền.

    `candidates` phải theo thứ tự xếp hạng (tốt nhất trước). Nhóm chứa chunk xếp hạng cao
    nhất là nhóm chính: nếu nhóm chính không giải quyết được thì toàn bộ câu hỏi bị từ chối.
    """
    if not candidates:
        return Resolution(abstain_code=AbstainCode.NO_EVIDENCE, abstain_detail="no candidates")

    chain = list(unit_chain)
    rank = {u: i for i, u in enumerate(chain)}

    groups: dict[str, list[Candidate]] = {}
    for c in candidates:
        groups.setdefault(group_key(c), []).append(c)

    results = [
        _resolve_group(
            k, cs, rank=rank, chain=chain, as_of=as_of,
            hidden=hidden_successor_ids, keep_overridden=keep_overridden_context,
        )
        for k, cs in groups.items()
    ]
    dropped = [d for g in results for d in g.dropped]
    notes = [n for g in results for n in g.notes]

    primary = results[0]
    if primary.code is not None:
        return Resolution(
            dropped=dropped,
            abstain_code=primary.code,
            abstain_detail=primary.detail,
            conflict_unit_ids=primary.conflict_units,
            notes=notes,
        )

    evidences = list(primary.evidences)
    for g in results[1:]:  # nhóm phụ chỉ dùng nếu tự giải quyết được
        if g.code is None:
            evidences.extend(g.evidences)
        else:
            notes.append(f"secondary group {g.key} bỏ qua: {g.code.value}")

    return Resolution(
        evidences=evidences,
        dropped=dropped,
        controlling_unit_id=primary.controlling_unit_id,
        effective_at=primary.effective_at,
        notes=notes,
    )
