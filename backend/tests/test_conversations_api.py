from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import query
from app.rag.pipeline import AskResponse


def test_conversation_list_is_scoped_to_persona(monkeypatch):
    requested_users = []
    monkeypatch.setattr(
        query,
        "list_conversations",
        lambda user_id: requested_users.append(user_id) or [],
    )

    assert query.conversations("u_quoc_bao") == []
    assert requested_users == ["u_quoc_bao"]


def test_conversation_detail_is_scoped_to_persona(monkeypatch):
    requested = []
    monkeypatch.setattr(
        query,
        "get_conversation",
        lambda user_id, conversation_id: requested.append((user_id, conversation_id))
        or {"id": conversation_id},
    )

    assert query.conversation("conversation-1", "u_minh_anh") == {"id": "conversation-1"}
    assert requested == [("u_minh_anh", "conversation-1")]


def test_conversation_detail_returns_not_found_for_other_persona(monkeypatch):
    monkeypatch.setattr(query, "get_conversation", lambda *_: None)

    with pytest.raises(HTTPException) as error:
        query.conversation("conversation-1", "u_quoc_bao")

    assert error.value.status_code == 404


def test_ask_saves_exchange_under_authenticated_demo_persona(monkeypatch):
    saved = []

    class Pipeline:
        def ask(self, **kwargs):
            return AskResponse(status="answered", answer="Answer", request_id="req-test")

    app = SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(pipeline=Pipeline())))
    request = Request({"type": "http", "app": app})
    monkeypatch.setattr(
        query,
        "append_exchange",
        lambda **kwargs: saved.append(kwargs),
    )

    response = query.ask(
        query.AskRequest(question="Question", context_id="ctx_minh_anh_main"),
        request,
        "u_minh_anh",
    )

    assert response.conversation_id
    assert saved[0]["user_id"] == "u_minh_anh"
    assert saved[0]["question"] == "Question"
    assert saved[0]["answer"]["request_id"] == "req-test"
