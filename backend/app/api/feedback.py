"""Endpoint tiếp nhận phản hồi của người dùng và chuyển tiếp cho người phụ trách (Owner / HR / Legal)."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_persona, UnknownPersona

logger = logging.getLogger("app.api.feedback")

router = APIRouter(prefix="/api/v1", tags=["feedback"])


class FeedbackPayload(BaseModel):
    request_id: str = Field(min_length=1)
    context_id: str | None = None
    content: str = Field(default="", max_length=2000)
    escalate_to: str | None = Field(default="Knowledge Owner")  # "Knowledge Owner" | "HR/Legal"


@router.post("/feedback")
def submit_feedback(
    payload: FeedbackPayload,
    x_persona_id: str | None = Header(default=None, alias="X-Persona-Id"),
) -> dict[str, Any]:
    try:
        persona = get_persona(x_persona_id or "u_minh_anh")
    except UnknownPersona as exc:
        raise HTTPException(status_code=401, detail="Persona không hợp lệ") from exc

    # Ghi nhận phản hồi và chuyển tiếp cho người phụ trách
    logger.info(
        "Đã nhận phản hồi từ user %s (context: %s) cho request_id=%s, escalate_to=%s: %s",
        persona.user_id,
        payload.context_id,
        payload.request_id,
        payload.escalate_to,
        payload.content,
    )

    return {
        "status": "received",
        "request_id": payload.request_id,
        "escalate_to": payload.escalate_to or "Knowledge Owner",
        "message": f"Yêu cầu đã được chuyển tới {payload.escalate_to or 'Knowledge Owner'}. Cảm ơn bạn đã phản hồi.",
    }