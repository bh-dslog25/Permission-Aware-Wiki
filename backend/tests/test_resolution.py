"""Unit test suite chuyên biệt cho resolution.py (Step 5 - Temporal & policy resolution).
Chạy độc lập, không phụ thuộc DB hay OpenFGA.
"""
from datetime import date
import pytest

from app.rag.retrieval import Candidate
from app.rag.resolution import (
    AbstainCode,
    ROLE_BASELINE,
    ROLE_CONTROLLING,
    ROLE_FALLBACK,
    resolve,
)


def _cand(
    document_id: str,
    owner_unit_id: str,
    *,
    scope: str = "group",
    topic: str = "ot",
    doc_type: str = "policy",
    classification: int = 1,
    valid_from: date | None = date(2025, 1, 1),
    valid_to: date | None = None,
    doc_status: str = "active",
    mandatory: bool = False,
    parent_conformance: str | None = None,
    superseded_by: str | None = None,
    amended_by: str | None = None,
    content: str = "",
    policy_values: dict | None = None,
) -> Candidate:
    return Candidate(
        chunk_id=f"{document_id}_c1",
        document_id=document_id,
        title=f"Title {document_id}",
        content=content or f"Content of {document_id}",
        content_hash="hash123",
        chunk_index=0,
        owner_unit_id=owner_unit_id,
        scope=scope,
        topic=topic,
        doc_type=doc_type,
        classification=classification,
        version_number="2025",
        valid_from=valid_from,
        valid_to=valid_to,
        doc_status=doc_status,
        mandatory=mandatory,
        superseded_by=superseded_by,
        amended_by=amended_by,
        parent_conformance=parent_conformance,
        policy_values=policy_values,
    )


UNIT_CHAIN = ["a_group", "company_a", "a_tech"]


def test_validity_before_valid_from():
    c = _cand("doc1", "a_group", valid_from=date(2025, 1, 1))
    res = resolve([c], unit_chain=UNIT_CHAIN, as_of=date(2024, 12, 31))
    assert not res.ok
    assert res.abstain_code == AbstainCode.OUT_OF_SCOPE
    assert any(d.reason == "not_yet_effective" for d in res.dropped)


def test_validity_exact_valid_from():
    c = _cand("doc1", "a_group", valid_from=date(2025, 1, 1))
    res = resolve([c], unit_chain=UNIT_CHAIN, as_of=date(2025, 1, 1))
    assert res.ok
    assert len(res.evidences) == 1


def test_validity_exact_valid_to():
    c = _cand("doc1", "a_group", valid_from=date(2024, 1, 1), valid_to=date(2025, 1, 1))
    res = resolve([c], unit_chain=UNIT_CHAIN, as_of=date(2025, 1, 1))
    assert res.ok
    assert len(res.evidences) == 1


def test_validity_after_valid_to():
    c = _cand("doc1", "a_group", valid_from=date(2024, 1, 1), valid_to=date(2025, 1, 1))
    res = resolve([c], unit_chain=UNIT_CHAIN, as_of=date(2025, 1, 2))
    assert not res.ok
    assert res.abstain_code == AbstainCode.OUT_OF_SCOPE
    assert any(d.reason == "expired_at_as_of" for d in res.dropped)


def test_draft_always_rejected():
    c = _cand("doc1", "a_group", doc_status="draft")
    res = resolve([c], unit_chain=UNIT_CHAIN, as_of=date(2025, 1, 1))
    assert not res.ok
    assert any(d.reason == "draft" for d in res.dropped)


def test_missing_valid_from_inconsistent():
    c = _cand("doc1", "a_group", valid_from=None)
    res = resolve([c], unit_chain=UNIT_CHAIN, as_of=date(2025, 1, 1))
    assert not res.ok
    assert res.abstain_code == AbstainCode.METADATA_INCONSISTENT


def test_same_level_same_topic_conflict():
    c1 = _cand("doc1", "company_a", topic="kinh_phi")
    c2 = _cand("doc2", "company_a", topic="kinh_phi")
    res = resolve([c1, c2], unit_chain=UNIT_CHAIN, as_of=date(2025, 1, 1))
    assert not res.ok
    assert res.abstain_code == AbstainCode.UNRESOLVED_CONFLICT


def test_amendment_missing_base():
    # Amendment có hiệu lực nhưng bản gốc không trong candidates
    c = _cand("doc_amend", "a_tech", amended_by=None)
    c.amended_by = "doc_base"  # doc_base không có trong candidates
    res = resolve([c], unit_chain=UNIT_CHAIN, as_of=date(2025, 1, 1))
    assert res.ok
    assert any("thiếu bản gốc" in n for n in res.notes)


def test_mandatory_ceiling_violated():
    # Tập đoàn đặt trần OT 40h mandatory; Phòng Kỹ thuật đặt 50h
    base = _cand("grp-ot", "a_group", topic="lam_them_gio", mandatory=True, content="40 giờ mỗi tháng")
    ctrl = _cand("a-tech-ot", "a_tech", topic="lam_them_gio", mandatory=False, content="50 giờ mỗi tháng")
    res = resolve([ctrl, base], unit_chain=UNIT_CHAIN, as_of=date(2025, 6, 1))
    assert res.ok
    # Controlling bị loại vì vi phạm trần, baseline được chọn làm controlling
    assert any("vượt trần" in n for n in res.notes)
    ev_docs = [e.candidate.document_id for e in res.evidences]
    assert "grp-ot" in ev_docs
    assert "a-tech-ot" not in ev_docs


def test_mandatory_floor_violated():
    # Tập đoàn đặt sàn nghỉ phép 12 ngày mandatory; Phòng Kỹ thuật đặt 10 ngày
    base = _cand("grp-leave", "a_group", topic="nghi_phep", mandatory=True, content="12 ngày phép")
    ctrl = _cand("a-tech-leave", "a_tech", topic="nghi_phep", mandatory=False, content="10 ngày phép")
    res = resolve([ctrl, base], unit_chain=UNIT_CHAIN, as_of=date(2025, 6, 1))
    assert res.ok
    # Controlling bị loại vì thấp hơn sàn, baseline được chọn làm controlling
    assert any("thấp hơn sàn" in n for n in res.notes)
    ev_docs = [e.candidate.document_id for e in res.evidences]
    assert "grp-leave" in ev_docs
    assert "a-tech-leave" not in ev_docs


def test_mandatory_bounded_confirmed():
    base = _cand("grp-ot", "a_group", topic="lam_them_gio", mandatory=True, content="40 giờ mỗi tháng")
    ctrl = _cand("cob-ot", "company_a", topic="lam_them_gio", mandatory=False, parent_conformance="confirmed", content="30 giờ mỗi tháng")
    res = resolve([ctrl, base], unit_chain=UNIT_CHAIN, as_of=date(2025, 6, 1))
    assert res.ok
    assert res.controlling_unit_id == "company_a"
    ev_docs = [e.candidate.document_id for e in res.evidences]
    assert "cob-ot" in ev_docs


def test_mandatory_bounded_unverified():
    base = _cand("grp-ot", "a_group", topic="lam_them_gio", mandatory=True, content="Không rõ số giờ")
    ctrl = _cand("a-tech-incident", "a_tech", topic="lam_them_gio", mandatory=False, parent_conformance="unverified", content="Quy định chưa rõ")
    res = resolve([ctrl, base], unit_chain=UNIT_CHAIN, as_of=date(2025, 6, 1))
    assert not res.ok
    assert res.abstain_code == AbstainCode.UNRESOLVED_CONFLICT
    assert "parent_conformance_unverified" in res.abstain_detail


def test_resolution_trace_generated():
    c = _cand("doc1", "a_group", valid_from=date(2025, 1, 1))
    res = resolve([c], unit_chain=UNIT_CHAIN, as_of=date(2025, 1, 1))
    assert res.ok
    assert len(res.trace) > 0
    assert any(s.rule == "validity_check" and s.action == "kept" for s in res.trace)
