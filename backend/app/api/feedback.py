"""
app/api/feedback.py
Endpoint tiếp nhận phản hồi của con người đối với đề xuất
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("app.api.feedback")

router = APIRouter()

class FeedbackPayload(BaseModel):
    context_