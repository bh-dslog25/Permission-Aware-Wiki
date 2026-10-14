"""Question-answering API for the permission-aware RAG MVP."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import (
    InvalidContext,
    UnknownPersona,
    authenticate_user,
    context_unit,
    get_persona,
    list_personas,
    resolve_context,
    unit_name,
)
from app.rag.pipeline import RequestContext

router = APIRouter(prefix="/api/v1", tags=["query"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    context_id: str | None = None
    as_of: date | None = None


class LoginRequest(BaseModel):
    username_or_email: str
    password: str = ""


def _container(request: Request) -> Any:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="Ứng dụng chưa sẵn sàng")
    return container


def _persona(persona_id: str | None):
    try:
        return get_persona(persona_id or "u_minh_anh")
    except UnknownPersona as exc:
        raise HTTPException(status_code=401, detail="Persona demo không hợp lệ") from exc


def _serialize_persona(p: Any) -> dict[str, Any]:
    contexts = []
    for ctx in p.allowed_contexts:
        unit_id = context_unit(ctx)
        contexts.append({"id": ctx, "label": unit_name(unit_id)})
    return {
        "id": p.user_id,
        "name": p.name,
        "email": p.email,
        "title": p.title,
        "role": p.role,
        "clearance": p.clearance,
        "description": p.description,
        "contexts": contexts,
        "default_context_id": p.allowed_contexts[0] if p.allowed_contexts else None,
        "is_persona": p.is_persona,
    }


@router.get("/personas")
def get_all_personas() -> list[dict[str, Any]]:
    """Trả về danh sách tài khoản / persona trực tiếp từ org.yaml."""
    return [_serialize_persona(p) for p in list_personas() if p.is_persona or p.allowed_contexts]


@router.post("/auth/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    """Đăng nhập bằng email hoặc username có trong org.yaml."""
    p = authenticate_user(payload.username_or_email, payload.password)
    if not p:
        raise HTTPException(
            status_code=401,
            detail="Tài khoản hoặc mật khẩu không chính xác. Vui lòng kiểm tra danh sách tài khoản trong org.yaml.",
        )
    return {
        "status": "success",
        "user": _serialize_persona(p),
    }


@router.get("/me")
def me(x_persona_id: str | None = Header(default=None, alias="X-Persona-Id")) -> dict[str, Any]:
    p = _persona(x_persona_id)
    contexts = []
    for ctx in p.allowed_contexts:
        unit_id = context_unit(ctx)
        contexts.append({"id": ctx, "label": unit_name(unit_id)})
    return {
        "id": p.user_id,
        "name": p.name,
        "email": p.email,
        "title": p.title,
        "clearance": p.clearance,
        "role": p.role,
        "contexts": contexts,
        "personas": [_serialize_persona(item) for item in list_personas() if item.is_persona or item.allowed_contexts],
    }


@router.post("/ask")
def ask(
    payload: AskRequest,
    request: Request,
    x_persona_id: str | None = Header(default=None, alias="X-Persona-Id"),
):
    p = _persona(x_persona_id)
    try:
        context_id = resolve_context(p, payload.context_id)
    except InvalidContext as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    pipeline = _container(request).pipeline
    return pipeline.ask(
        ctx=RequestContext(user_id=p.user_id, context_id=context_id, clearance=p.clearance),
        question=payload.question,
        as_of=payload.as_of,
    )
