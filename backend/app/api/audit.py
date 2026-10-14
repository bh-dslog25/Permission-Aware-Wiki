"""Audit HTTP endpoints."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query

from app.audit import query_audit_logs
from app.auth import get_persona

router = APIRouter(prefix="/api/v1", tags=["audit"])


@router.get("/audit")
def audit_logs(
    x_persona_id: str | None = Header(default=None, alias="X-Persona-Id"),
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    persona = get_persona(x_persona_id or "u_minh_anh")
    if persona.role not in {"hr_owner", "security", "manager"}:
        raise HTTPException(status_code=403, detail="Chỉ owner/manager demo được xem audit")
    return {
        "items": query_audit_logs(
            user_id=None,
            outcome=outcome,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    }
