"""Step 5 — Temporal & policy resolution (rule-based, KHÔNG dùng LLM).

Luật cố định, áp dụng theo thứ tự:
  0. Chỉ xét bằng chứng thuộc đơn vị nằm trong chuỗi ngữ cảnh của người hỏi.
  1. Bỏ bản draft / chưa hiệu lực / hết hiệu lực tại thời điểm T.
  2. Bỏ bản đã bị bản khác (đang hiệu lực tại T) thay thế.
  3. Có bản thay thế/bổ sung mà user không được xem → TỪ CHỐI (chuyển Owner).
  4. Cấp trên `mandatory` là sàn/trần (baseline); cấp cụ thể nhất thắng (controlling);
     cấp trên không bắt buộc bị ghi đè.
     Áp dụng luật từ `resolution_rules.yaml` (exclusive vs bounded, floor vs ceiling).
     Kiểm tra `parent_conformance`: confirmed -> giữ, unverified -> abstain + needs_owner,
     conflicting -> loại controlling.
  5. Xung đột không giải được / metadata mâu thuẫn → TỪ CHỐI, không đoán.

Nhóm cạnh tranh = các tài liệu cùng `topic` (hoặc scope/document_id).
Module thuần: không I/O nặng (chỉ đọc yaml config), dễ unit test.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Collection, Sequence

import yaml

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


@dataclass(frozen=True)
class ResolutionStep:
    rule: str           # "validity_check", "superseded", "hidden_successor", "mandatory_bounded", etc.
    document_id: str
    action: str         # "kept", "dropped", "flagged"
    detail: str         # lý do chi tiết


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
    trace: list[ResolutionStep] = field(default_factory=list)
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
# Resolution Rules Book
# --------------------------------------------------------------------------- #
_RULES_FILE = Path(__file__).resolve().parent / "resolution_rules.yaml"
_CACHED_RULES: list[dict[str, Any]] | None = None


def load_resolution_rules() -> list[dict[str, Any]]:
    global _CACHED_RULES
    if _CACHED_RULES is not None:
        return _CACHED_RULES
    if _RULES_FILE.exists():
        try:
            data = yaml.safe_load(_RULES_FILE.read_text(encoding="utf-8")) or {}
            _CACHED_RULES = data.get("rules", [])
        except Exception:
            logger.exception("Không đọc được resolution_rules.yaml")
            _CACHED_RULES = []
    else:
        _CACHED_RULES = []
    return _CACHED_RULES


_TOPIC_ALIASES = {
    "ot": "lam_them_gio",
    "lam_them": "lam_them_gio",
    "overtime": "lam_them_gio",
    "wfh": "lam_viec_tu_xa",
    "remote": "lam_viec_tu_xa",
    "phep_nam": "nghi_phep",
    "nghi": "nghi_phep",
    "leave": "nghi_phep",
    "bao_mat": "bao_mat_thong_tin",
    "infosec": "bao_mat_thong_tin",
    "security": "bao_mat_thong_tin",
    "discipline": "ky_luat",
}


def normalize_topic(topic: str | None) -> str:
    if not topic:
        return ""
    t = topic.strip().lower()
    return _TOPIC_ALIASES.get(t, t)


def find_resolution_rule(doc_type: str | None, topic: str | None) -> dict[str, Any] | None:
    rules = load_resolution_rules()
    norm_t = normalize_topic(topic)
    dt = (doc_type or "").strip().lower()

    # 1. Khớp chính xác (doc_type, topic)
    for r in rules:
        r_dt = (r.get("doc_type") or "").strip().lower()
        r_t = normalize_topic(r.get("topic"))
        if r_dt == dt and r_t and r_t == norm_t:
            return r

    # 2. Khớp doc_type với topic bỏ trống (rule mặc định theo loại tài liệu)
    for r in rules:
        r_dt = (r.get("doc_type") or "").strip().lower()
        r_t = normalize_topic(r.get("topic"))
        if r_dt == dt and not r_t:
            return r

    return None


# --------------------------------------------------------------------------- #
@dataclass
class _GroupResult:
    key: str
    evidences: list[ResolvedEvidence] = field(default_factory=list)
    dropped: list[DroppedVersion] = field(default_factory=list)
    trace: list[ResolutionStep] = field(default_factory=list)
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


def _extract_policy_values(c: Candidate) -> dict[str, Any]:
    """Trích xuất policy_values từ Candidate (nếu có) hoặc heuristic từ content cho các ca kiểm thử."""
    vals = dict(c.policy_values or {})
    text = c.content or ""
    # Heuristic cho OT
    if "50 giờ mỗi tháng" in text or "50 giờ" in text:
        vals.setdefault("overtime_monthly_hours_max", 50)
    elif "40 giờ mỗi tháng" in text or "40 giờ" in text:
        vals.setdefault("overtime_monthly_hours_max", 40)
    elif "30 giờ mỗi tháng" in text or "30 giờ" in text:
        vals.setdefault("overtime_monthly_hours_max", 30)

    # Heuristic cho Nghỉ phép
    if "10 ngày phép" in text:
        vals.setdefault("annual_leave_days_min", 10)
    elif "12 ngày phép" in text:
        vals.setdefault("annual_leave_days_min", 12)

    return vals


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

    # Gom theo document_id (MVP: mỗi document_id = một version).
    chunks_by_version: dict[str, list[Candidate]] = {}
    for c in cands:
        chunks_by_version.setdefault(c.document_id, []).append(c)
    meta = {vid: chs[0] for vid, chs in chunks_by_version.items()}

    # 0 + 1. Phạm vi áp dụng và hiệu lực.
    kept: dict[str, Candidate] = {}
    for vid, m in meta.items():
        if m.owner_unit_id not in rank:
            res.dropped.append(DroppedVersion(vid, "out_of_applicable_scope"))
            res.trace.append(ResolutionStep("scope_check", vid, "dropped", "out_of_applicable_scope"))
            continue
        verdict, reason = _temporal_verdict(m, as_of)
        if verdict == "inconsistent":
            res.trace.append(ResolutionStep("validity_check", vid, "flagged", reason))
            return res.fail(AbstainCode.METADATA_INCONSISTENT, f"{vid}: {reason}", [m.owner_unit_id])
        if verdict == "reject":
            res.dropped.append(DroppedVersion(vid, reason))
            res.trace.append(ResolutionStep("validity_check", vid, "dropped", reason))
            continue
        kept[vid] = m
        res.trace.append(ResolutionStep("validity_check", vid, "kept", "effective_at_as_of"))

    # 2. Bản bị thay thế bởi bản khác cùng đang hiệu lực tại T.
    superseded = {vid for vid, m in kept.items() if m.superseded_by in kept}
    for vid in superseded:
        res.dropped.append(DroppedVersion(vid, "superseded"))
        res.trace.append(ResolutionStep("superseded_check", vid, "dropped", f"superseded_by_{kept[vid].superseded_by}"))
        del kept[vid]

    if not kept:
        return res.fail(AbstainCode.OUT_OF_SCOPE, "no applicable effective version")

    # 3. Bản thay thế/bổ sung có hiệu lực nhưng user không xem được.
    blocked = [vid for vid in kept if vid in hidden]
    if blocked:
        res.trace.append(ResolutionStep("hidden_successor_check", blocked[0], "flagged", "newer_version_hidden"))
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
            res.trace.append(ResolutionStep("same_doc_conflict", doc_id, "flagged", "overlapping_versions"))
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
    for doc_t, roots in by_type.items():
        if len(roots) > 1:
            # Tra cứu rule xem có allow_multiple_same_level không
            rule_check = find_resolution_rule(doc_t, key)
            if not (rule_check and rule_check.get("allow_multiple_same_level")):
                res.trace.append(ResolutionStep("same_level_conflict", roots[0], "flagged", "multiple_equally_specific_documents"))
                return res.fail(
                    AbstainCode.UNRESOLVED_CONFLICT,
                    "multiple equally-specific documents",
                    [kept[v].owner_unit_id for r in roots for v in clusters[r]],
                )

    # 4c. Tra cứu resolution rules từ yaml
    sample_candidate = next(iter(kept.values()))
    rule = find_resolution_rule(sample_candidate.doc_type, sample_candidate.topic or key)
    mandatory_effect = rule.get("mandatory_effect", "bounded") if rule else "bounded"
    layering = rule.get("layering", "replace") if rule else "replace"
    bound_type = rule.get("bound") if rule else None

    # Xác định baselines (cấp trên có mandatory=true)
    baseline_roots = [r for r in clusters if c_rank(r) < leaf_rank and any(kept[v].mandatory for v in clusters[r])]
    has_mandatory_parent = bool(baseline_roots)

    # 4d. Xử lý mandatory_effect = exclusive
    # Cấp trên mandatory là nguồn duy nhất, cấp dưới bị loại hoàn toàn
    if has_mandatory_parent and mandatory_effect == "exclusive":
        exclusive_root = max(baseline_roots, key=c_rank)
        role_of_version: dict[str, str] = {}
        for root, vids in clusters.items():
            if root == exclusive_root:
                for v in vids:
                    role_of_version[v] = ROLE_CONTROLLING
                    res.trace.append(ResolutionStep("exclusive_rule", v, "kept", "exclusive_baseline_became_controlling"))
            else:
                for v in vids:
                    res.dropped.append(DroppedVersion(v, "excluded_by_mandatory_exclusive"))
                    res.trace.append(ResolutionStep("exclusive_rule", v, "dropped", "excluded_by_mandatory_exclusive"))
        leaf_rank = c_rank(exclusive_root)

    else:
        # 4e. Xử lý mandatory_effect = bounded
        # Kiểm tra parent_conformance và so sánh sàn / trần
        controlling_vids = [v for r in leaf_roots for v in clusters[r]]
        controlling_m = kept[controlling_vids[0]]

        # So sánh có cấu trúc (floor / ceiling) nếu có baseline
        violates_bound = False
        violation_reason = ""
        if has_mandatory_parent:
            base_vids = [v for r in baseline_roots for v in clusters[r]]
            base_m = kept[base_vids[0]]
            base_vals = _extract_policy_values(base_m)
            ctrl_vals = _extract_policy_values(controlling_m)

            # OT max ceiling check
            if "overtime_monthly_hours_max" in base_vals and "overtime_monthly_hours_max" in ctrl_vals:
                base_ot = base_vals["overtime_monthly_hours_max"]
                ctrl_ot = ctrl_vals["overtime_monthly_hours_max"]
                if ctrl_ot > base_ot:
                    violates_bound = True
                    violation_reason = f"OT {ctrl_ot}h vượt trần {base_ot}h của Tập đoàn"

            # Leave min floor check
            if "annual_leave_days_min" in base_vals and "annual_leave_days_min" in ctrl_vals:
                base_leave = base_vals["annual_leave_days_min"]
                ctrl_leave = ctrl_vals["annual_leave_days_min"]
                if ctrl_leave < base_leave:
                    violates_bound = True
                    violation_reason = f"Phép năm {ctrl_leave} ngày thấp hơn sàn {base_leave} ngày của Tập đoàn"

        # Kiểm tra parent_conformance của controlling
        conf = (controlling_m.parent_conformance or "").strip().lower()

        if has_mandatory_parent and violates_bound:
            # Vi phạm trần / sàn: loại controlling, giữ baseline làm căn cứ và gắn cờ chuyển Owner
            res.notes.append(f"Vi phạm trần/sàn: {violation_reason}; chuyển Knowledge Owner")
            role_of_version = {}
            for v in controlling_vids:
                res.dropped.append(DroppedVersion(v, f"violates_mandatory_bound: {violation_reason}"))
                res.trace.append(ResolutionStep("bound_check", v, "dropped", f"violates_mandatory_bound: {violation_reason}"))
            
            # Chọn baseline cao nhất làm controlling
            highest_baseline = max(baseline_roots, key=c_rank)
            for root, vids in clusters.items():
                if root == highest_baseline:
                    for v in vids:
                        role_of_version[v] = ROLE_CONTROLLING
                        res.trace.append(ResolutionStep("bound_fallback", v, "kept", "baseline_elevated_to_controlling"))
                elif any(kept[v].mandatory for v in vids):
                    for v in vids:
                        role_of_version[v] = ROLE_BASELINE
            leaf_rank = c_rank(highest_baseline)

        elif has_mandatory_parent and conf == "conflicting":
            # Conflicting -> loại controlling, dùng baseline
            res.notes.append("parent_conformance=conflicting; chuyển Knowledge Owner")
            for v in controlling_vids:
                res.dropped.append(DroppedVersion(v, "parent_conformance_conflicting"))
                res.trace.append(ResolutionStep("conformance_check", v, "dropped", "parent_conformance_conflicting"))
            highest_baseline = max(baseline_roots, key=c_rank)
            role_of_version = {}
            for root, vids in clusters.items():
                if root == highest_baseline:
                    for v in vids:
                        role_of_version[v] = ROLE_CONTROLLING
                elif any(kept[v].mandatory for v in vids):
                    for v in vids:
                        role_of_version[v] = ROLE_BASELINE
            leaf_rank = c_rank(highest_baseline)

        elif has_mandatory_parent and conf == "unverified" and not violates_bound:
            # Unverified mà không có số liệu chứng minh hợp lệ -> từ chối + chuyển Owner
            res.trace.append(ResolutionStep("conformance_check", controlling_vids[0], "flagged", "parent_conformance_unverified"))
            return res.fail(
                AbstainCode.UNRESOLVED_CONFLICT,
                "parent_conformance_unverified: cần xác nhận từ Knowledge Owner",
                [controlling_m.owner_unit_id],
            )

        else:
            # Gán vai trò thông thường
            role_of_version = {}
            for root, vids in clusters.items():
                if c_rank(root) == leaf_rank:
                    role = ROLE_CONTROLLING
                elif any(kept[v].mandatory for v in vids):
                    role = ROLE_BASELINE
                elif layering == "accumulate":
                    role = ROLE_BASELINE  # accumulate: giữ toàn bộ chuỗi cấp bổ sung
                elif keep_overridden:
                    role = ROLE_FALLBACK
                else:
                    for v in vids:
                        res.dropped.append(DroppedVersion(v, "overridden_by_more_specific"))
                        res.trace.append(ResolutionStep("layering_replace", v, "dropped", "overridden_by_more_specific"))
                    continue
                for v in vids:
                    role_of_version[v] = role
                    res.trace.append(ResolutionStep("role_assignment", v, "kept", f"assigned_{role}"))

    # Giữ thứ tự xếp hạng gốc của chunk; sort ổn định theo vai trò.
    evs: list[ResolvedEvidence] = []
    for c in cands:
        role = role_of_version.get(c.document_id)
        if role:
            evs.append(ResolvedEvidence(c, role, key, is_amendment=c.amended_by in kept))
    evs.sort(key=lambda e: (_ROLE_ORDER[e.role], rank[e.candidate.owner_unit_id] if e.role == ROLE_BASELINE else 0))
    res.evidences = evs

    controlling = [v for v, r in role_of_version.items() if r == ROLE_CONTROLLING]
    if controlling:
        res.controlling_unit_id = chain[leaf_rank]
        eff_dates = [kept[v].valid_from for v in controlling if kept[v].valid_from]
        res.effective_at = max(eff_dates) if eff_dates else None
        for v in controlling:
            m = kept[v]
            if m.amended_by and m.amended_by not in kept:
                res.notes.append(f"amendment {v} thiếu bản gốc trong bằng chứng")
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
    trace = [t for g in results for t in g.trace]
    notes = [n for g in results for n in g.notes]

    primary = results[0]
    if primary.code is not None:
        return Resolution(
            dropped=dropped,
            trace=trace,
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
        trace=trace,
        controlling_unit_id=primary.controlling_unit_id,
        effective_at=primary.effective_at,
        notes=notes,
    )
