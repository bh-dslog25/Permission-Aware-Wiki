"""Điều phối pipeline online (8 bước trong Wiki) — pipeline cố định, không agent.

  1  Danh tính + ngữ cảnh   : do auth.py giải quyết, truyền vào dưới dạng RequestContext
  2  Hiểu câu hỏi            : QueryParser → StructuredQuery (song song với bước 3)
  3  Authorization           : PolicyAdapter (OpenFGA + ABAC) → UserScope
  4  Constrained retrieval   : HybridRetriever (lọc quyền/hiệu lực tại index)
  5  Policy resolution       : resolution.resolve (rule-based)
  6  Sinh + verify citation  : generation.generate_answer
  7  Audit                   : AuditSink
  Mọi lỗi ở bước quyền/truy xuất → fail closed (abstain), không có đường tắt.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .generation import (
    GenerationConfig,
    GenerationResult,
    LLMClient,
    abstain,
    generate_answer,
    parse_llm_json,
    public_refusal_reason,
)
from .resolution import AbstainCode, resolve
from .retrieval import HybridRetriever, RetrievalError, UserScope, tokenize, _STOPWORDS

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RequestContext:
    """Đã được auth.py xác minh từ nguồn tin cậy — KHÔNG lấy từ body của client."""

    user_id: str
    context_id: str
    clearance: str = "internal"


class PolicyAdapter(Protocol):
    def build_scope(self, ctx: RequestContext) -> UserScope:
        """OpenFGA + ABAC → UserScope. Phải raise nếu không xác định được quyền."""


class AuditSink(Protocol):
    def record(
        self, *, request_id: str, actor_id: str, active_context: str, action: str,
        authorization_result: dict[str, Any], source_ids: list[str], source_hashes: list[str],
        details: dict[str, Any],
    ) -> None: ...


class TimeSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: Literal["current", "at"] = "current"
    at: date | None = None


class StructuredQuery(BaseModel):
    """Chỉ mô tả ý định/phạm vi. Mọi trường quyền do LLM sinh ra (allowed_units, ...) bị bỏ."""

    model_config = ConfigDict(extra="ignore")
    intent: str = "policy_lookup"
    topic: str | None = None
    subject: str | None = None
    time: TimeSpec = Field(default_factory=TimeSpec)
    requested_scope: str | None = "my_applicable_policy"
    keywords: list[str] = Field(default_factory=list)


class CitationOut(BaseModel):
    index: int
    document_id: str
    chunk_id: str
    title: str
    excerpt: str
    quote_verified: bool
    version_number: str | None = None


class AskResponse(BaseModel):
    status: Literal["answered", "abstained", "needs_clarification"]
    answer: str
    applies_to: str | None = None
    context_label: str | None = None
    effective_at: date | None = None
    as_of: date | None = None
    citations: list[CitationOut] = Field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None  # not_found | needs_owner | insufficient_evidence | system_error
    request_id: str


@dataclass(frozen=True)
class PipelineConfig:
    keep_overridden_context: bool = False
    authz_timeout_s: float = 5.0
    parse_timeout_s: float = 15.0
    generation: GenerationConfig = field(default_factory=GenerationConfig)


# --------------------------------------------------------------------------- #
# PII mask — câu hỏi lên LLM/API chỉ đi dưới dạng đã che PII
# --------------------------------------------------------------------------- #
_PII = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"(?<!\d)\d{12}(?!\d)"), "[ID]"),
    (re.compile(r"(?<!\d)(?:\+?84|0)(?:[\s.-]?\d){8,10}(?!\d)"), "[PHONE]"),
]


def mask_pii(text: str) -> str:
    for rx, repl in _PII:
        text = rx.sub(repl, text)
    return text


# --------------------------------------------------------------------------- #
# Query understanding (bước 2)
# --------------------------------------------------------------------------- #
_PARSER_SYSTEM = """Bạn chuẩn hóa câu hỏi về quy định nội bộ thành JSON. Chỉ in một đối tượng JSON:
{"intent": "policy_lookup", "topic": "<chủ đề, snake_case>", "subject": "<đối tượng cụ thể hoặc null>",
 "time": {"mode": "current" | "at", "at": "YYYY-MM-DD hoặc null"},
 "requested_scope": "my_applicable_policy", "keywords": ["<thuật ngữ tiếng Việt, tối đa 8>"]}
- Chuẩn hóa thuật ngữ nội bộ (OT=làm thêm giờ, WFH=làm việc từ xa, phép năm=nghỉ phép năm, onsite=làm việc tại chỗ) và đưa cả dạng gốc lẫn dạng chuẩn vào keywords.
- Nếu câu hỏi nhắc một thời điểm cụ thể: mode="at". Chỉ có năm → dùng ngày 31/12 của năm đó. Không nhắc thời điểm → mode="current", at=null.
- TUYỆT ĐỐI không sinh trường về quyền truy cập, đơn vị được phép hay độ mật."""


class LLMQueryParser:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def parse(self, masked_question: str, today: date) -> StructuredQuery:
        try:
            raw = self._llm.generate(
                system=_PARSER_SYSTEM,
                user=f"Hôm nay: {today.isoformat()}\nCâu hỏi: {masked_question}",
                temperature=0.0, json_mode=True,
            )
            sq = StructuredQuery.model_validate(parse_llm_json(raw))
            sq.keywords = [k.strip() for k in sq.keywords if k and k.strip()][:8]
            return sq
        except Exception:  # noqa: BLE001 — rơi về heuristic, không chặn pipeline
            logger.warning("Query parser lỗi, dùng heuristic", exc_info=True)
            return heuristic_query(masked_question)


def heuristic_query(question: str) -> StructuredQuery:
    toks = [t for t in tokenize(question) if t not in _STOPWORDS and len(t) > 1]
    return StructuredQuery(keywords=list(dict.fromkeys(toks))[:8])


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
@dataclass
class _Trace:
    request_id: str
    ctx: RequestContext
    scope: UserScope | None = None
    timings: dict[str, float] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    source_ids: list[str] = field(default_factory=list)
    source_hashes: list[str] = field(default_factory=list)


class RagPipeline:
    def __init__(
        self,
        *,
        retriever: HybridRetriever,
        policy_adapter: PolicyAdapter,
        llm: LLMClient,
        query_parser: Any | None = None,
        audit: AuditSink | None = None,
        config: PipelineConfig | None = None,
        today_fn=date.today,
    ) -> None:
        self._retriever = retriever
        self._adapter = policy_adapter
        self._llm = llm
        self._parser = query_parser or LLMQueryParser(llm)
        self._audit = audit
        self._cfg = config or PipelineConfig()
        self._today = today_fn
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rag")

    # ------------------------------------------------------------------ #
    def ask(self, *, ctx: RequestContext, question: str, as_of: date | None = None) -> AskResponse:
        trace = _Trace(request_id=f"req_{uuid.uuid4().hex[:12]}", ctx=ctx)
        t_start = time.perf_counter()
        today = self._today()
        masked = mask_pii(question)

        # Bước 2 & 3 chạy song song.
        parse_f = self._pool.submit(self._parser.parse, masked, today)
        scope_f = self._pool.submit(self._adapter.build_scope, ctx)

        t = time.perf_counter()
        try:
            scope = scope_f.result(timeout=self._cfg.authz_timeout_s)
            if scope.user_id != ctx.user_id or scope.context_id != ctx.context_id:
                raise PermissionError("scope không khớp danh tính đã xác minh")
        except Exception:  # noqa: BLE001 — fail closed
            logger.exception("authorization failed")
            trace.details["authz_error"] = True
            return self._finish(trace, t_start, abstain(AbstainCode.AUTHZ_ERROR), None, None, today)
        trace.scope = scope
        trace.timings["authz_ms"] = _ms(t)

        t = time.perf_counter()
        try:
            sq = parse_f.result(timeout=self._cfg.parse_timeout_s)
        except Exception:  # noqa: BLE001
            sq = heuristic_query(masked)
        trace.timings["parse_ms"] = _ms(t)
        trace.details["structured_query"] = sq.model_dump(mode="json")

        # Thời điểm T: tham số request > thời điểm trong câu hỏi > hôm nay.
        t_asof = as_of or (sq.time.at if sq.time.mode == "at" else None) or today
        historical = t_asof < today
        trace.details.update(as_of=t_asof.isoformat(), historical=historical)

        # Bước 4
        t = time.perf_counter()
        try:
            rr = self._retriever.search(
                question=masked, keywords=sq.keywords, scope=scope, as_of=t_asof, historical=historical
            )
        except RetrievalError:
            logger.exception("retrieval failed")
            return self._finish(trace, t_start, abstain(AbstainCode.RETRIEVAL_ERROR), None, scope, t_asof)
        trace.timings["retrieval_ms"] = _ms(t)
        trace.details.update(
            keyword_hits=rr.keyword_hits, vector_hits=rr.vector_hits, expanded=rr.expanded,
            rejected_out_of_scope=rr.rejected_out_of_scope,
        )
        if not rr.candidates:
            return self._finish(trace, t_start, abstain(AbstainCode.NO_EVIDENCE), None, scope, t_asof)

        # Bước 5
        t = time.perf_counter()
        try:
            hidden = self._retriever.find_hidden_successors(
                [c.document_id for c in rr.candidates], scope, t_asof
            )
        except RetrievalError:
            logger.exception("successor check failed")
            return self._finish(trace, t_start, abstain(AbstainCode.RETRIEVAL_ERROR), None, scope, t_asof)
        resolution = resolve(
            rr.candidates, unit_chain=scope.unit_chain, as_of=t_asof,
            hidden_successor_ids=hidden, keep_overridden_context=self._cfg.keep_overridden_context,
        )
        trace.timings["resolution_ms"] = _ms(t)
        trace.details["dropped"] = [(d.document_id, d.reason) for d in resolution.dropped]
        trace.details["resolution_notes"] = resolution.notes
        if not resolution.ok:
            owners = [scope.unit_labels.get(u, u) for u in resolution.conflict_unit_ids]
            gen = abstain(resolution.abstain_code or AbstainCode.NO_EVIDENCE, resolution.abstain_detail, owners)
            trace.details["abstain_code"] = gen.abstain_code.value if gen.abstain_code else None
            return self._finish(trace, t_start, gen, resolution, scope, t_asof)

        # Bước 6
        t = time.perf_counter()
        gen = generate_answer(
            question=masked, resolution=resolution, llm=self._llm, as_of=t_asof,
            context_label=scope.context_label, unit_labels=scope.unit_labels, config=self._cfg.generation,
            authorized_chunk_ids={c.chunk_id for c in rr.candidates},
        )
        trace.timings["generation_ms"] = _ms(t)
        return self._finish(trace, t_start, gen, resolution, scope, t_asof)

    # ------------------------------------------------------------------ #
    def _finish(self, trace: _Trace, t_start: float, gen: GenerationResult, resolution, scope, as_of) -> AskResponse:
        trace.timings["total_ms"] = _ms(t_start)
        if gen.abstain_code:
            trace.details["abstain_code"] = gen.abstain_code.value
            trace.details["abstain_detail"] = gen.detail
        trace.details.update(
            status=gen.status, dropped_claims=gen.dropped_claims, injection_flagged=gen.injection_flagged
        )
        if resolution is not None:
            used = set(gen.used_chunk_ids)
            for ev in resolution.evidences:
                if ev.candidate.chunk_id in used:
                    trace.source_ids.append(ev.candidate.chunk_id)
                    if ev.candidate.content_hash:
                        trace.source_hashes.append(ev.candidate.content_hash)

        applies_to = effective_at = None
        if gen.status == "answered" and resolution is not None and scope is not None:
            applies_to = scope.unit_labels.get(resolution.controlling_unit_id or "", resolution.controlling_unit_id)
            effective_at = resolution.effective_at

        self._write_audit(trace, scope)
        return AskResponse(
            status=gen.status,  # type: ignore[arg-type]
            answer=gen.answer,
            applies_to=applies_to,
            context_label=scope.context_label if scope else None,
            effective_at=effective_at,
            as_of=as_of,
            refused=gen.status != "answered",
            refusal_reason=public_refusal_reason(gen.abstain_code),
            citations=[CitationOut(**{k: getattr(c, k) for k in CitationOut.model_fields}) for c in gen.citations],
            request_id=trace.request_id,
        )

    def _write_audit(self, trace: _Trace, scope: UserScope | None) -> None:
        if not self._audit:
            return
        authz = (
            {"allowed_units": list(scope.allowed_unit_ids), "max_classification": scope.max_classification,
             "unit_chain": list(scope.unit_chain)}
            if scope else {"error": "authorization_failed"}
        )
        try:  # không lưu nguyên văn câu hỏi/câu trả lời/nội dung chunk
            self._audit.record(
                request_id=trace.request_id, actor_id=trace.ctx.user_id, active_context=trace.ctx.context_id,
                action="ask", authorization_result=authz, source_ids=trace.source_ids,
                source_hashes=trace.source_hashes, details={**trace.details, "timings": trace.timings},
            )
        except Exception:  # noqa: BLE001
            logger.exception("audit write failed")  # MVP: ghi log lỗi; production nên fail closed/queue lại


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


# --------------------------------------------------------------------------- #
# Điểm vào đầu-cuối
# --------------------------------------------------------------------------- #
def build_default_pipeline(
    *, db, embedder, policy_adapter: PolicyAdapter, audit: AuditSink | None = None,
    reranker=None, llm: LLMClient | None = None, config: PipelineConfig | None = None,
) -> RagPipeline:
    """Ghép toàn bộ pipeline với Gemini (cấu hình từ biến môi trường) làm LLM mặc định."""
    from ..llm import get_default_llm  # import muộn: không bắt buộc SDK khi test

    return RagPipeline(
        retriever=HybridRetriever(db, embedder, reranker),
        policy_adapter=policy_adapter,
        llm=llm or get_default_llm(),
        audit=audit,
        config=config,
    )


def answer_question(
    pipeline: RagPipeline, *, user_id: str, context_id: str, question: str, as_of: date | None = None,
    clearance: str = "internal",
) -> AskResponse:
    """Xử lý một câu hỏi từ đầu đến cuối.

    `user_id` / `context_id` PHẢI đến từ auth.py đã xác minh, không lấy từ body client.
    Trả về AskResponse: `answer`, `citations` (nguồn), `status`, `refused` và `refusal_reason`.
    """
    return pipeline.ask(ctx=RequestContext(user_id, context_id, clearance), question=question, as_of=as_of)
