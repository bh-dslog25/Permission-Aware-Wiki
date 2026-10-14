"""Audit sink/query helpers for the MVP schema."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.db import Database, get_conn, write_audit


class AuditLogger:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db

    def record(
        self,
        *,
        request_id: str,
        actor_id: str,
        active_context: str,
        action: str,
        authorization_result: dict[str, Any],
        source_ids: list[str],
        source_hashes: list[str],
        details: dict[str, Any],
    ) -> None:
        question_hash = None
        if details.get("question"):
            question_hash = hashlib.sha256(str(details["question"]).encode("utf-8")).hexdigest()

        timings = details.get("timings") or {}
        latency = timings.get("total_ms")
        structured = details.get("structured_query")
        outcome = details.get("status") or "error"
        if outcome == "needs_clarification":
            outcome = "abstained"

        with get_conn() as conn:
            write_audit(
                conn,
                user_id=actor_id,
                context_unit=active_context,
                question_hash=question_hash,
                as_of_date=details.get("as_of"),
                structured_query=structured,
                allowed_units=authorization_result.get("allowed_units"),
                max_classification=authorization_result.get("max_classification"),
                authz_decision={
                    "request_id": request_id,
                    "action": action,
                    **authorization_result,
                },
                retrieved_chunk_ids=source_ids,
                source_hashes=source_hashes,
                outcome=outcome if outcome in {"answered", "abstained", "denied", "error"} else "error",
                abstain_reason=details.get("abstain_code"),
                latency_ms=int(latency) if isinstance(latency, (int, float)) else None,
                resolution={
                    "dropped": details.get("dropped", []),
                    "notes": details.get("resolution_notes", []),
                    "request_id": request_id,
                },
            )


def query_audit_logs(
    *,
    user_id: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if user_id:
        clauses.append("user_id = %s")
        params.append(user_id)
    if outcome:
        clauses.append("outcome = %s")
        params.append(outcome)
    if since:
        clauses.append("ts >= %s")
        params.append(since)
    if until:
        clauses.append("ts <= %s")
        params.append(until)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT id, ts, user_id, context_unit, as_of_date, structured_query,
               allowed_units, max_classification, authz_decision,
               retrieved_chunk_ids, source_hashes, outcome, abstain_reason,
               latency_ms, resolution
        FROM audit_log
        {where_sql}
        ORDER BY ts DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    with get_conn() as conn:
        cur = conn.execute(sql, params)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
