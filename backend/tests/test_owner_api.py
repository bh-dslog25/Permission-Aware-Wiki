"""Unit test cho Knowledge Owner Portal API."""
from fastapi.testclient import TestClient
from app.main import create_app
import pytest

app = create_app()
client = TestClient(app)


def test_non_owner_forbidden():
    """Tài khoản nhân viên bình thường (u_minh_anh, role: employee) không được truy cập."""
    resp = client.get("/api/v1/owner/pending-reviews", headers={"X-Persona-Id": "u_minh_anh"})
    assert resp.status_code == 403
    assert "không có quyền Knowledge Owner" in resp.json()["detail"]


def test_missing_auth_header():
    """Không có header persona thì bị từ chối 401."""
    resp = client.get("/api/v1/owner/pending-reviews")
    assert resp.status_code == 401


def test_owner_access_allowed():
    """Tài khoản Knowledge Owner (u_pham_lan, role: hr_owner) được truy cập."""
    resp = client.get("/api/v1/owner/pending-reviews", headers={"X-Persona-Id": "u_pham_lan"})
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["owner"]["role"] == "hr_owner"
