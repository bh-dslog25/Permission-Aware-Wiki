"""Client Gemini cho bước query-understanding và sinh câu trả lời.

Cài: `pip install google-genai`. Biến môi trường:
  GEMINI_API_KEY (bắt buộc), LLM_MODEL (mặc định bên dưới — hãy kiểm tra tên model hiện hành),
  LLM_TIMEOUT_S, LLM_MAX_RETRIES.

Lưu ý bảo mật: client này gửi nguyên prompt lên cloud. Việc quyết định bằng chứng nào được phép
rời hệ thống nằm ở generation.py (GenerationConfig.llm_allowed_classifications) — không nằm ở đây.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Gemini lỗi, bị chặn hoặc trả rỗng."""


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model: str = "gemini-3.8-flash"
    timeout_s: float = 30.0
    max_retries: int = 2
    max_output_tokens: int = 2048

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        from app.config import get_settings

        settings = get_settings()
        key = os.environ.get("GEMINI_API_KEY") or settings.gemini_api_key
        if not key:
            raise LLMError("GEMINI_API_KEY chưa được cấu hình")
        model = os.environ.get("LLM_MODEL") or os.environ.get("GEMINI_MODEL") or settings.gemini_model or cls.model
        return cls(
            api_key=key,
            model=model,
            timeout_s=float(os.environ.get("LLM_TIMEOUT_S", cls.timeout_s)),
            max_retries=int(os.environ.get("LLM_MAX_RETRIES", cls.max_retries)),
        )


class GeminiClient:
    """Thực thi giao thức `LLMClient` (generation.py)."""

    def __init__(self, config: GeminiConfig | None = None, *, client: Any | None = None) -> None:
        self._cfg = config or GeminiConfig.from_env()
        if client is None:
            from google import genai  # import muộn để module dùng được khi chưa cài SDK

            client = genai.Client(api_key=self._cfg.api_key)
        self._client = client

    def generate(self, *, system: str, user: str, temperature: float = 0.0, json_mode: bool = True) -> str:
        config: dict[str, Any] = {
            "system_instruction": system,
            "temperature": temperature,
            "max_output_tokens": self._cfg.max_output_tokens,
            "http_options": {"timeout": int(self._cfg.timeout_s * 1000)},  # ms
        }
        if json_mode:
            config["response_mime_type"] = "application/json"

        last: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            started = time.perf_counter()
            try:
                resp = self._client.models.generate_content(
                    model=self._cfg.model, contents=user, config=config
                )
                logger.info(
                    "gemini_call_completed model=%s attempt=%d duration_ms=%.1f",
                    self._cfg.model,
                    attempt + 1,
                    (time.perf_counter() - started) * 1000,
                )
                text = getattr(resp, "text", None)
                if not text:  # bị chặn bởi safety hoặc rỗng — thử lại không giúp ích
                    raise LLMError("Gemini trả về rỗng hoặc bị chặn")
                return text
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 — lỗi mạng/429/5xx: thử lại có backoff
                last = exc
                logger.warning(
                    "Gemini lỗi (lần %d): %s; duration_ms=%.1f",
                    attempt + 1,
                    type(exc).__name__,
                    (time.perf_counter() - started) * 1000,
                )
                if attempt < self._cfg.max_retries:
                    time.sleep(min(2 ** attempt, 8))
        raise LLMError("Gemini không phản hồi sau khi thử lại") from last


class ExtractiveJsonLLM:
    """Small local fallback for MVP demos without a Gemini key.

    It preserves the pipeline contract by returning JSON with citations drawn
    from the evidence blocks. It is deliberately conservative and extractive.
    """

    _EVIDENCE_RE = re.compile(
        r'<evidence[^>]*chunk_id="(?P<chunk>[^"]+)"[^>]*doc="(?P<doc>[^"]*)"[^>]*>\n(?P<body>.*?)\n</evidence>',
        re.S,
    )

    def generate(self, *, system: str, user: str, temperature: float = 0.0, json_mode: bool = True) -> str:
        import json

        if "chuẩn hóa câu hỏi" in system:
            question = user.rsplit("Câu hỏi:", 1)[-1].strip()
            keywords = [w for w in re.findall(r"[\wÀ-ỹ]+", question.lower()) if len(w) > 2][:8]
            return json.dumps(
                {
                    "intent": "policy_lookup",
                    "topic": None,
                    "subject": None,
                    "time": {"mode": "current", "at": None},
                    "requested_scope": "my_applicable_policy",
                    "keywords": list(dict.fromkeys(keywords)),
                },
                ensure_ascii=False,
            )

        evidences = list(self._EVIDENCE_RE.finditer(user))
        if not evidences:
            return json.dumps({"status": "abstained", "claims": [], "clarification_question": None, "reason": "no evidence"}, ensure_ascii=False)

        claims = []
        for match in evidences[:3]:
            body = re.sub(r"\s+", " ", match.group("body")).strip()
            quote = body[:180]
            claims.append(
                {
                    "text": quote,
                    "citations": [{"chunk_id": match.group("chunk"), "quote": quote}],
                }
            )
        return json.dumps({"status": "answered", "claims": claims, "clarification_question": None, "reason": None}, ensure_ascii=False)


def get_default_llm() -> GeminiClient | ExtractiveJsonLLM:
    from app.config import get_settings

    if os.environ.get("GEMINI_API_KEY") or get_settings().gemini_api_key:
        return GeminiClient()
    return ExtractiveJsonLLM()
