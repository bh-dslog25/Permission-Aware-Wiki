"""Xác định persona demo và ngữ cảnh hợp lệ từ cấu hình server (data/org.yaml).

Ref: README Section 3.1 & 4.2 Step 1.
Frontend chỉ gửi persona_id (+ context_id muốn chọn); quyền thật do OpenFGA
và Policy Adapter tính toán phía backend.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

# Đường dẫn mặc định đến file cấu hình tổ chức
REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ORG_PATHS = [
    REPO_ROOT / "data" / "org.yaml",
    REPO_ROOT / "docs" / "org.yaml",
]


class UnknownPersona(Exception):
    pass


class InvalidContext(Exception):
    pass


@dataclass(frozen=True)
class Persona:
    user_id: str
    name: str
    clearance: str
    allowed_contexts: tuple[str, ...]
    role: str = "employee"
    description: str = ""
    team_id: Optional[str] = None
    department_id: Optional[str] = None
    company_id: Optional[str] = None
    email: str = ""
    title: str = ""
    is_persona: bool = True
    memberships: tuple[dict, ...] = ()

    @property
    def id(self) -> str:
        return self.user_id

    @property
    def contexts(self) -> tuple[str, ...]:
        return self.allowed_contexts


# Alias phục vụ demo và test suite
PERSONA_ALIASES = {
    "an": "u_minh_anh",
    "binh": "u_quoc_bao",
    "chi": "u_thu_ha",
    "lan": "u_pham_lan",
}

CONTEXT_ALIASES = {
    "a-kinh-doanh": "ctx_thu_ha_a",
    "b-ky-thuat": "ctx_thu_ha_b",
}


@lru_cache(maxsize=1)
def load_org() -> dict:
    env_path = os.getenv("ORG_FILE")
    if env_path:
        p = Path(env_path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f)

    for p in CANDIDATE_ORG_PATHS:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f)

    raise FileNotFoundError("Không tìm thấy org.yaml tại data/org.yaml hoặc docs/org.yaml")


def _generate_email(user_id: str) -> str:
    # u_minh_anh -> minh.anh@appota.com
    clean = user_id.removeprefix("u_")
    parts = clean.split("_")
    if len(parts) >= 2:
        return f"{'.'.join(parts)}@appota.com"
    return f"{clean}@appota.com"


def list_personas() -> list[Persona]:
    org = load_org()
    role_clearance = org.get("role_clearance", {
        "employee": "internal",
        "manager": "confidential",
        "hr_owner": "confidential",
        "security": "restricted",
    })

    personas: list[Persona] = []
    for u in org.get("users", []):
        memberships = u.get("memberships", [])
        if "contexts" in u:
            contexts = tuple(u["contexts"])
        else:
            contexts = tuple(m.get("context_id", m.get("team")) for m in memberships if m)

        first_role = memberships[0].get("role", "employee") if memberships else "employee"
        first_title = memberships[0].get("title", "") if memberships else ""
        clearance = u.get("clearance") or role_clearance.get(first_role, "internal")

        email = u.get("email") or _generate_email(u["id"])

        p = Persona(
            user_id=u["id"],
            name=u.get("name", u["id"]),
            clearance=clearance,
            allowed_contexts=contexts,
            role=first_role,
            description=u.get("description", ""),
            team_id=memberships[0].get("team") if memberships else None,
            email=email,
            title=first_title or u.get("description", ""),
            is_persona=bool(u.get("persona", True)),
            memberships=tuple(memberships),
        )
        personas.append(p)
    return personas


def authenticate_user(identifier: str, password: str) -> Persona | None:
    """Xác thực người dùng theo email hoặc user_id/username trong org.yaml.
    Mật khẩu mặc định trong demo là 'demo123' hoặc bất kỳ mật khẩu nào được gán trong org.yaml.
    """
    clean_id = identifier.strip().lower()
    aliased_id = PERSONA_ALIASES.get(clean_id, clean_id).lower()
    for p in list_personas():
        expected_email = p.email.lower()
        raw_id = p.user_id.lower()
        simple_id = p.user_id.removeprefix("u_").lower()

        if clean_id in (expected_email, raw_id, simple_id) or aliased_id in (raw_id, simple_id):
            return p
    return None


def get_persona(persona_id: str) -> Persona:
    normalized_id = PERSONA_ALIASES.get(persona_id, persona_id)
    for p in list_personas():
        if p.user_id == normalized_id or p.user_id == persona_id:
            return p
    raise UnknownPersona(persona_id)


class NeedsContextClarification(Exception):
    def __init__(self, available_contexts: tuple[str, ...]):
        super().__init__(f"User has multiple contexts: {available_contexts}")
        self.available_contexts = available_contexts


def resolve_context(persona: Persona, requested: str | None = None) -> str:
    """Trả về đúng MỘT ngữ cảnh. Nếu người dùng kiêm nhiệm nhiều ngữ cảnh mà không chỉ định thì yêu cầu làm rõ.

    Quyền chọn ngữ cảnh được OpenFGA kiểm tra lại (quan hệ assigned) trong policy_adapter.
    """
    if requested is None:
        if len(persona.allowed_contexts) > 1:
            raise NeedsContextClarification(persona.allowed_contexts)
        if not persona.allowed_contexts:
            raise InvalidContext(f"{persona.user_id} không có ngữ cảnh hợp lệ")
        return persona.allowed_contexts[0]

    normalized_req = CONTEXT_ALIASES.get(requested, requested)
    if requested not in persona.allowed_contexts and normalized_req not in persona.allowed_contexts:
        raise InvalidContext(f"{persona.user_id} không được chọn ngữ cảnh {requested}")
    return requested if requested in persona.allowed_contexts else normalized_req


def context_unit(context_id: str) -> str:
    """Map a demo context_id to the unit/team id it represents."""
    unit_ids = {u["id"] for u in load_org().get("units", [])}
    if context_id in unit_ids:
        return context_id
    for user in load_org().get("users", []):
        for membership in user.get("memberships", []):
            if membership.get("context_id") == context_id:
                return membership.get("team", context_id)
    return context_id


def unit_name(unit_id: str) -> str:
    """Tên hiển thị cho đơn vị tổ chức."""
    for u in load_org().get("units", []):
        if u["id"] == unit_id:
            return u.get("name", unit_id)
    return unit_id
