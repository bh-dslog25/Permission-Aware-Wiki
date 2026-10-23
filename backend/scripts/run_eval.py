"""Evaluation script for Permission-Aware Wiki RAG.

Usage:
    python -m scripts.run_eval --suite data/eval/golden.jsonl --out data/eval/baseline.jsonl
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Add backend directory to sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.auth import get_persona, list_personas, UnknownPersona
from app.config import get_settings
from app.main import build_container
from app.rag.pipeline import RequestContext

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger("run_eval")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RAG Evaluation Suite")
    parser.add_argument(
        "--suite",
        type=str,
        default=str(REPO_ROOT / "data" / "eval" / "golden.jsonl"),
        help="Path to golden.jsonl test suite",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(REPO_ROOT / "data" / "eval" / "baseline.jsonl"),
        help="Path to output results JSONL",
    )
    parser.add_argument(
        "--meta",
        type=str,
        default=str(REPO_ROOT / "data" / "eval" / "baseline_meta.json"),
        help="Path to output metadata JSON",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit number of cases")
    parser.add_argument("--id", type=str, default=None, help="Run only specific case ID")
    parser.add_argument("--label", type=str, default="baseline", help="Evaluation label")
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BASELINE_JSONL", "AFTER_JSONL"),
        help="Compare two evaluation JSONL results and print diff table",
    )
    return parser.parse_args()


def get_git_info() -> dict[str, str]:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    except Exception:
        sha = "unknown"
    try:
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    except Exception:
        branch = "unknown"
    return {"git_sha": sha, "git_branch": branch}


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)


def evaluate_case(item: dict[str, Any], pipeline: Any) -> dict[str, Any]:
    case_id = item["id"]
    category = item.get("category", "unknown")
    user_id = item["user_id"]
    context_id = item.get("context_id")
    as_of_str = item.get("as_of")
    as_of = date.fromisoformat(as_of_str) if as_of_str else None
    question = item["question"]
    expected_behavior = item.get("expected_behavior", "answer")
    expected_sources = set(item.get("expected_sources") or [])
    must_not_retrieve = set(item.get("must_not_retrieve") or [])
    must_not_cite = set(item.get("must_not_cite") or [])

    try:
        persona = get_persona(user_id)
        clearance = persona.clearance
    except UnknownPersona:
        clearance = "internal"

    ctx = RequestContext(
        user_id=user_id,
        context_id=context_id,
        clearance=clearance,
    )

    t0 = time.perf_counter()
    resp = None
    diag = {}
    err_msg = None

    try:
        ask_res = pipeline.ask(
            ctx=ctx,
            question=question,
            as_of=as_of,
            _diag=True,
        )
        if isinstance(ask_res, tuple):
            resp, diag = ask_res
        else:
            resp = ask_res
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"

    total_ms = (time.perf_counter() - t0) * 1000.0
    if diag and "timings" in diag and "total_ms" in diag["timings"]:
        total_ms = diag["timings"]["total_ms"]

    actual_status = resp.status if resp else "error"
    candidate_doc_ids = set(diag.get("candidate_document_ids") or [])
    candidate_chunk_ids = set(diag.get("candidate_chunk_ids") or [])

    cited_documents = set()
    cited_chunk_ids = set()
    quote_verified_all = True

    if resp and resp.citations:
        for cit in resp.citations:
            doc_id = getattr(cit, "document_id", None)
            chunk_id = getattr(cit, "chunk_id", None)
            quote_v = getattr(cit, "quote_verified", True)
            if doc_id:
                cited_documents.add(doc_id)
            if chunk_id:
                cited_chunk_ids.add(chunk_id)
            if not quote_v:
                quote_verified_all = False

    expected_sources_found = sorted(list(expected_sources & cited_documents))
    expected_sources_missing = sorted(list(expected_sources - cited_documents))
    unauthorized_retrieved = sorted(list(must_not_retrieve & candidate_doc_ids))
    unauthorized_cited = sorted(list((must_not_cite | must_not_retrieve) & cited_documents))

    # Metric: unauthorized_chunks
    # Candidate contains document in must_not_retrieve
    unauthorized_chunks_count = len(unauthorized_retrieved)

    # Metric: scope_correct
    # expected_sources (document-level) ⊆ cited documents AND không cite must_not_cite AND đúng status
    status_matches = False
    if expected_behavior == "answer":
        status_matches = actual_status in ("answered", "answer")
    elif expected_behavior == "abstain":
        status_matches = actual_status in ("abstained", "refused", "abstain")
    elif expected_behavior == "ask_context":
        status_matches = actual_status in ("ask_context", "needs_clarification")
    elif expected_behavior == "answer_with_escalation":
        status_matches = actual_status in ("answered", "answer_with_escalation")

    scope_correct = (
        expected_sources.issubset(cited_documents)
        and len(unauthorized_cited) == 0
        and status_matches
    )

    # Metric: temporal_correct
    # Cho category temporal: cite đúng version theo as_of, không cite version bị cấm
    temporal_correct = True
    if "temporal" in category:
        temporal_correct = (
            expected_sources.issubset(cited_documents)
            and len(unauthorized_retrieved) == 0
            and len(unauthorized_cited) == 0
        )
    else:
        temporal_correct = len(unauthorized_retrieved) == 0 and len(unauthorized_cited) == 0

    # Metric: citation_correct
    # Mỗi citation thuộc tập evidence -> generation VÀ quote_verified=true
    if actual_status == "answered" and resp and resp.citations:
        citations_in_candidates = cited_chunk_ids.issubset(candidate_chunk_ids) if candidate_chunk_ids else True
        citation_correct = citations_in_candidates and quote_verified_all
    else:
        citation_correct = True

    abstain_code = None
    if resp and getattr(resp, "refused", False):
        abstain_code = getattr(resp, "refusal_reason", None)
    if diag.get("resolution_trace", {}).get("abstain_code"):
        abstain_code = diag["resolution_trace"]["abstain_code"]

    return {
        "id": case_id,
        "category": category,
        "expected_behavior": expected_behavior,
        "actual_status": actual_status,
        "expected_sources_found": expected_sources_found,
        "expected_sources_missing": expected_sources_missing,
        "unauthorized_retrieved": unauthorized_retrieved,
        "unauthorized_cited": unauthorized_cited,
        "cited_documents": sorted(list(cited_documents)),
        "unauthorized_chunks": unauthorized_chunks_count,
        "scope_correct": bool(scope_correct),
        "temporal_correct": bool(temporal_correct),
        "citation_correct": bool(citation_correct),
        "abstain_code": abstain_code,
        "total_ms": round(total_ms, 2),
        "error": err_msg,
    }


def compute_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    if total == 0:
        return {}

    total_unauthorized_chunks = sum(r["unauthorized_chunks"] for r in records)
    scope_correct_count = sum(1 for r in records if r["scope_correct"])
    temporal_records = [r for r in records if "temporal" in r.get("category", "")]
    temporal_correct_count = sum(1 for r in temporal_records if r["temporal_correct"])
    temporal_total = len(temporal_records) if temporal_records else 1

    citation_correct_count = sum(1 for r in records if r["citation_correct"])

    # Abstain metrics
    # Ca expected_behavior == "abstain"
    abstain_expected = [r for r in records if r["expected_behavior"] == "abstain"]
    # Ca actual_status == "abstained"
    abstain_actual = [r for r in records if r["actual_status"] in ("abstained", "refused", "abstain")]
    # True positives: actual is abstain AND expected is abstain
    tp_abstain = sum(1 for r in abstain_actual if r["expected_behavior"] == "abstain")

    abstain_precision = (tp_abstain / len(abstain_actual)) if abstain_actual else 1.0
    abstain_recall = (tp_abstain / len(abstain_expected)) if abstain_expected else 1.0

    latencies = [r["total_ms"] for r in records if r.get("total_ms") is not None]
    p50 = percentile(latencies, 50)
    p90 = percentile(latencies, 90)
    p95 = percentile(latencies, 95)

    # Categories breakdown
    categories: dict[str, dict[str, Any]] = {}
    for r in records:
        cat = r.get("category", "unknown")
        if cat not in categories:
            categories[cat] = {
                "total": 0,
                "scope_correct": 0,
                "temporal_correct": 0,
                "unauthorized": 0,
                "latencies": [],
            }
        categories[cat]["total"] += 1
        if r["scope_correct"]:
            categories[cat]["scope_correct"] += 1
        if r["temporal_correct"]:
            categories[cat]["temporal_correct"] += 1
        if r["unauthorized_chunks"] > 0:
            categories[cat]["unauthorized"] += 1
        categories[cat]["latencies"].append(r["total_ms"])

    cat_summary = {}
    for cat, data in sorted(categories.items()):
        cnt = data["total"]
        cat_summary[cat] = {
            "total": cnt,
            "scope_accuracy": round(data["scope_correct"] / cnt, 4),
            "temporal_accuracy": round(data["temporal_correct"] / cnt, 4),
            "unauthorized_count": data["unauthorized"],
            "p95_ms": round(percentile(data["latencies"], 95), 1),
        }

    return {
        "total_cases": total,
        "metrics": {
            "unauthorized_chunks": total_unauthorized_chunks,
            "scope_correct_rate": round(scope_correct_count / total, 4),
            "temporal_correct_rate": round(temporal_correct_count / temporal_total, 4),
            "citation_correct_rate": round(citation_correct_count / total, 4),
            "abstain_precision": round(abstain_precision, 4),
            "abstain_recall": round(abstain_recall, 4),
            "latency_p50_ms": round(p50, 1),
            "latency_p90_ms": round(p90, 1),
            "latency_p95_ms": round(p95, 1),
        },
        "by_category": cat_summary,
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 70)
    print("                    EVALUATION SUMMARY")
    print("=" * 70)
    m = summary["metrics"]
    print(f"Total Cases             : {summary['total_cases']}")
    print(f"Unauthorized Chunks     : {m['unauthorized_chunks']} (Target: 0)")
    print(f"Scope Correct Rate      : {m['scope_correct_rate'] * 100:.1f}%")
    print(f"Temporal Correct Rate   : {m['temporal_correct_rate'] * 100:.1f}%")
    print(f"Citation Correct Rate   : {m['citation_correct_rate'] * 100:.1f}%")
    print(f"Abstain Precision       : {m['abstain_precision'] * 100:.1f}%")
    print(f"Abstain Recall          : {m['abstain_recall'] * 100:.1f}%")
    print(f"Latency (p50 / p90 / p95): {m['latency_p50_ms']}ms / {m['latency_p90_ms']}ms / {m['latency_p95_ms']}ms")
    print("-" * 70)
    print(f"{'Category':<32} | {'Total':<5} | {'Scope Acc':<9} | {'Unauth':<6} | {'P95 (ms)':<8}")
    print("-" * 70)
    for cat, cdata in summary["by_category"].items():
        print(
            f"{cat:<32} | {cdata['total']:<5} | {cdata['scope_accuracy'] * 100:6.1f}%  | {cdata['unauthorized_count']:<6} | {cdata['p95_ms']:<8}"
        )
    print("=" * 70 + "\n")


def compare_results(baseline_file: str, after_file: str) -> None:
    p_base = Path(baseline_file)
    p_after = Path(after_file)
    if not p_base.exists() or not p_after.exists():
        print(f"Lỗi: Không tìm thấy file so sánh: {baseline_file} hoặc {after_file}", file=sys.stderr)
        sys.exit(1)

    with open(p_base, encoding="utf-8") as f:
        base_rows = [json.loads(line) for line in f if line.strip()]
    with open(p_after, encoding="utf-8") as f:
        after_rows = [json.loads(line) for line in f if line.strip()]

    s_base = compute_summary(base_rows)
    s_after = compute_summary(after_rows)

    m_b = s_base.get("metrics", {})
    m_a = s_after.get("metrics", {})

    print("\n" + "=" * 80)
    print("                    SO SÁNH ĐÁNH GIÁ (BEFORE / AFTER)")
    print("=" * 80)
    print(f"{'Chỉ số':<30} | {'Baseline':<12} | {'After':<12} | {'Thay đổi':<15}")
    print("-" * 80)
    metrics_to_show = [
        ("unauthorized_chunks", "Rò rỉ quyền (unauthorized)", False),
        ("scope_correct_rate", "Đúng scope & status (%)", True),
        ("temporal_correct_rate", "Đúng hiệu lực thời gian (%)", True),
        ("citation_correct_rate", "Trích dẫn được xác minh (%)", True),
        ("abstain_precision", "Độ chuẩn xác từ chối (%)", True),
        ("abstain_recall", "Độ phủ từ chối (%)", True),
        ("latency_p95_ms", "Độ trễ P95 (ms)", False),
    ]

    for key, label, is_pct in metrics_to_show:
        vb = m_b.get(key, 0)
        va = m_a.get(key, 0)
        if is_pct:
            diff = (va - vb) * 100
            diff_str = f"{diff:+.1f}%" if diff != 0 else "0.0%"
            print(f"{label:<30} | {vb * 100:10.1f}% | {va * 100:10.1f}% | {diff_str:<15}")
        else:
            diff = va - vb
            diff_str = f"{diff:+}" if diff != 0 else "0"
            print(f"{label:<30} | {vb:12} | {va:12} | {diff_str:<15}")
    print("=" * 80 + "\n")


def main() -> None:
    args = parse_args()
    if args.compare:
        compare_results(args.compare[0], args.compare[1])
        return

    suite_path = Path(args.suite)
    out_path = Path(args.out)
    meta_path = Path(args.meta)

    if not suite_path.exists():
        print(f"Error: test suite not found at {suite_path}", file=sys.stderr)
        sys.exit(1)

    with open(suite_path, encoding="utf-8") as f:
        items = [json.loads(line) for line in f if line.strip()]

    if args.id:
        items = [i for i in items if i["id"] == args.id]
        if not items:
            print(f"Error: no case with ID '{args.id}' found.", file=sys.stderr)
            sys.exit(1)

    if args.limit:
        items = items[: args.limit]

    print(f"Initializing container and pipeline for evaluation ({len(items)} cases)...")
    settings = get_settings()
    container = build_container(settings)
    pipeline = container.pipeline

    results = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Running evaluation...")
    for idx, item in enumerate(items, 1):
        cid = item["id"]
        res = evaluate_case(item, pipeline)
        results.append(res)
        status_icon = "✓" if res["scope_correct"] else "✗"
        print(f"[{idx:02d}/{len(items):02d}] {cid} ({item['category']}): {res['actual_status']} {status_icon} ({res['total_ms']}ms)")

    # Write output JSONL
    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    summary = compute_summary(results)
    print_summary(summary)

    # Write metadata JSON
    git_info = get_git_info()
    meta = {
        "run_at": datetime.now().isoformat(),
        "git_sha": git_info["git_sha"],
        "git_branch": git_info["git_branch"],
        "label": args.label,
        "suite": str(suite_path),
        "total_cases": len(results),
        "summary": summary,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    container.db.close()
    print(f"Results written to: {out_path}")
    print(f"Metadata written to: {meta_path}")


if __name__ == "__main__":
    main()
