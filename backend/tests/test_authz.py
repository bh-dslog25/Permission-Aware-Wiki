"""Kiểm tra tầng phân quyền với OpenFGA thật. Bỏ qua nếu OpenFGA/fga CLI chưa chạy.

Chưa phải test_leakage ở tầng retrieval: file đó cần bảng chunks trong Postgres.
Test này kiểm tra bước trước đó: authorization constraints có đúng không.
"""
from datetime import date

import httpx
import pytest

from app.auth import get_persona, InvalidContext, resolve_context
from app.authz import seed
from app.authz.policy_adapter import ContextNotAssigned, PolicyAdapter, Constraints


@pytest.fixture(scope="module")
def adapter():
    try:
        seed.run()
    except (httpx.HTTPError, FileNotFoundError) as e:
        pytest.skip(f"Cần OpenFGA đang chạy và fga CLI trong PATH: {e}")
    return PolicyAdapter()


def constraints(adapter, persona_id, ctx=None, **kw):
    p = get_persona(persona_id)
    return adapter.build_constraints(
        user_id=p.id, context_unit=resolve_context(p, ctx), clearance=p.clearance, **kw)


def test_nhan_vien_a_chi_thay_nhanh_cua_minh(adapter):
    c = constraints(adapter, "an")
    assert set(c.allowed_units) == {"a_sales_t1", "a_sales", "company_a", "a_group"}
    # phòng khác cùng công ty, và công ty khác, không được thấy
    assert not {"a_hr", "company_b", "b_tech", "b_hr"} & set(c.allowed_units)


def test_nhan_vien_b(adapter):
    c = constraints(adapter, "binh")
    assert set(c.allowed_units) == {"b_tech_be", "b_tech", "company_b", "a_group"}


def test_kiem_nhiem_moi_lan_mot_ngu_canh_khong_gop_quyen(adapter):
    ca = constraints(adapter, "chi", "ctx_thu_ha_a")
    cb = constraints(adapter, "chi", "ctx_thu_ha_b")
    assert set(ca.allowed_units) == {"a_sales_t2", "a_sales", "company_a", "a_group"}
    assert set(cb.allowed_units) == {"b_tech_qa", "b_tech", "company_b", "a_group"}


def test_chon_ngu_canh_khong_duoc_gan_bi_tu_choi_boi_openfga(adapter):
    # Bỏ qua kiểm tra cấu hình (resolve_context) để chứng minh OpenFGA tự chặn
    p = get_persona("an")
    with pytest.raises(ContextNotAssigned):
        adapter.build_constraints(user_id=p.id, context_unit="b_tech", clearance=p.clearance)
    with pytest.raises(InvalidContext):
        resolve_context(p, "ctx_thu_ha_b")


def test_abac_do_mat(adapter):
    assert constraints(adapter, "an").allowed_classifications == ("public", "internal")
    assert constraints(adapter, "chi", "ctx_thu_ha_a").allowed_classifications == (
        "public", "internal", "confidential")


def test_sql_filter_hieu_luc_va_danh_sach_rong():
    c = Constraints("x", "u", (), 0, date(2024, 6, 1))
    f = c.to_sql()
    assert f.params["auth_units"] == [] and f.params["auth_as_of"] == date(2024, 6, 1)
    assert "owner_unit = ANY(%(auth_units)s)" in f.clause
    assert "status <> 'draft'" in f.clause
