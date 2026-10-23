import pytest

from app.rag.generation import abstain_message, public_refusal_reason
from app.rag.resolution import AbstainCode


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        (
            (
                AbstainCode.NO_EVIDENCE,
                AbstainCode.OUT_OF_SCOPE,
                AbstainCode.HIDDEN_SUCCESSOR,
            ),
            "A",
        ),
        (
            (AbstainCode.INSUFFICIENT_EVIDENCE, AbstainCode.NO_CITATION),
            "B",
        ),
        (
            (
                AbstainCode.UNRESOLVED_CONFLICT,
                AbstainCode.METADATA_INCONSISTENT,
                AbstainCode.AUTHZ_ERROR,
                AbstainCode.RETRIEVAL_ERROR,
                AbstainCode.LLM_ERROR,
                AbstainCode.INVALID_OUTPUT,
            ),
            "C+D",
        ),
    ],
)
def test_internal_refusal_codes_map_to_three_public_categories(codes, expected):
    assert all(public_refusal_reason(code) == expected for code in codes)
    assert len({abstain_message(code) for code in codes}) == 1


def test_success_has_no_public_refusal_category():
    assert public_refusal_reason(None) is None
