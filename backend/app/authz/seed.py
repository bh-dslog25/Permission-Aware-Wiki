"""Nạp cây tổ chức và quan hệ user vào OpenFGA từ data/org.yaml.

Chạy:  python -m app.authz.seed     (từ thư mục backend/)
Yêu cầu: OpenFGA đang chạy (OPENFGA_API_URL) và `fga` CLI có trong PATH (hoặc đặt FGA_BIN)
         để đổi model.fga (DSL) sang JSON.
Lưu ý: seed XÓA và dựng lại store có tên OPENFGA_STORE_NAME, dành cho dev/demo.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx

try:
    from app.auth import load_org
except ImportError:
    from backend.app.auth import load_org

from app.authz.policy_adapter import DEFAULT_STORE_NAME, find_store_id

MODEL_FILE = Path(__file__).with_name("model.fga")
BATCH = 50  # OpenFGA giới hạn số tuple mỗi lần ghi


DEFAULT_AUTH_MODEL = {
    "schema_version": "1.1",
    "type_definitions": [
        {"type": "user"},
        {
            "type": "unit",
            "relations": {
                "child": {"this": {}},
                "assigned": {"this": {}},
                "member": {"this": {}},
                "viewer": {
                    "union": {
                        "child": [
                            {"computedUserset": {"relation": "member"}},
                            {
                                "tupleToUserset": {
                                    "tupleset": {"relation": "child"},
                                    "computedUserset": {"relation": "viewer"},
                                }
                            },
                        ]
                    }
                },
            },
            "metadata": {
                "relations": {
                    "child": {"directly_related_user_types": [{"type": "unit"}]},
                    "assigned": {"directly_related_user_types": [{"type": "user"}]},
                    "member": {"directly_related_user_types": [{"type": "user"}]},
                    "viewer": {"directly_related_user_types": []},
                }
            },
        },
    ],
}


def transform_model(path: Path = MODEL_FILE) -> dict:
    fga = os.getenv("FGA_BIN", "fga")
    try:
        out = subprocess.run(
            [fga, "model", "transform", "--file", str(path), "--output-format", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(out.stdout)
    except (FileNotFoundError, subprocess.SubprocessError):
        return DEFAULT_AUTH_MODEL


def build_tuples(org: dict) -> list[dict]:
    unit_ids = {u["id"] for u in org["units"]}
    tuples = []
    for u in org["units"]:
        if u.get("parent") is None:
            continue
        parent = u["parent"]
        if parent not in unit_ids:
            raise ValueError(f"Đơn vị cha không tồn tại: {parent} (của {u['id']})")
        # unit:con là "child" của unit:cha
        tuples.append({"user": f"unit:{u['id']}", "relation": "child", "object": f"unit:{parent}"})
    for user in org["users"]:
        for membership in user.get("memberships", []):
            team = membership.get("team")
            if not team or team not in unit_ids:
                raise ValueError(f"Membership của {user['id']} trỏ tới unit không tồn tại: {team!r}")
            tuples.append({"user": f"user:{user['id']}", "relation": "assigned", "object": f"unit:{team}"})
    return tuples


def seed_db(org: dict) -> None:
    """Tổ chức và quan hệ người dùng nằm ở org.yaml và OpenFGA (MVP)."""
    pass


def run(api_url: str | None = None, store_name: str | None = None) -> str:
    api_url = (api_url or os.getenv("OPENFGA_API_URL", "http://localhost:8080")).rstrip("/")
    store_name = store_name or os.getenv("OPENFGA_STORE_NAME", DEFAULT_STORE_NAME)
    org = load_org()
    seed_db(org)
    model = transform_model()
    tuples = build_tuples(org)

    with httpx.Client(base_url=api_url, timeout=10.0) as http:
        old = find_store_id(http, store_name)
        if old:
            http.delete(f"/stores/{old}").raise_for_status()
        r = http.post("/stores", json={"name": store_name})
        r.raise_for_status()
        store_id = r.json()["id"]

        r = http.post(f"/stores/{store_id}/authorization-models", json=model)
        r.raise_for_status()
        model_id = r.json()["authorization_model_id"]

        for i in range(0, len(tuples), BATCH):
            r = http.post(f"/stores/{store_id}/write", json={
                "authorization_model_id": model_id,
                "writes": {"tuple_keys": tuples[i:i + BATCH]},
            })
            r.raise_for_status()
    print(f"OpenFGA store '{store_name}' = {store_id}, model = {model_id}, tuples = {len(tuples)}")
    return store_id


if __name__ == "__main__":
    run()
