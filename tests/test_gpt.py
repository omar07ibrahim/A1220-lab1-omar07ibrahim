from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from receipt_extractor import gpt
from receipt_extractor.file_io import ImagePayload
from receipt_extractor.schema import ExpenseCategory, ReceiptFields


@dataclass(slots=True)
class _Response:
    output_parsed: ReceiptFields | None


class _Responses:
    def __init__(self, output_parsed: ReceiptFields | None) -> None:
        self.output_parsed = output_parsed
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return _Response(output_parsed=self.output_parsed)


@dataclass(slots=True)
class _Client:
    responses: _Responses


@pytest.fixture
def payload() -> ImagePayload:
    data = b"synthetic-image-bytes"
    return ImagePayload(
        name="receipt.png",
        media_type="image/png",
        data=data,
        sha256="not-used-by-adapter",
        width=3,
        height=2,
    )


def test_client_has_bounded_retry_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = object()

    def openai_factory(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(gpt, "OpenAI", openai_factory)

    assert gpt._get_client() is sentinel
    assert calls == [{"max_retries": 2, "timeout": 30.0}]


def test_adapter_sends_typed_responses_request_and_returns_json_values(
    payload: ImagePayload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = ReceiptFields(
        date="2026-07-24",
        amount="$12.50",
        vendor="Synthetic",
        category=ExpenseCategory.OTHER,
    )
    responses = _Responses(parsed)
    client = _Client(responses=responses)
    monkeypatch.setattr(gpt, "_get_client", lambda: client)

    result = gpt.extract_receipt_info(payload)

    assert result == {
        "date": "2026-07-24",
        "amount": "$12.50",
        "vendor": "Synthetic",
        "category": "Other",
    }
    [call] = responses.calls
    assert call == {
        "model": "gpt-4.1-mini",
        "instructions": gpt.INSTRUCTIONS,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Extract the receipt fields from this image.",
                    },
                    {
                        "type": "input_image",
                        "image_url": payload.data_url(),
                        "detail": "high",
                    },
                ],
            }
        ],
        "text_format": ReceiptFields,
        "max_output_tokens": 300,
        "store": False,
    }
    assert "seed" not in call


def test_adapter_rejects_missing_parsed_output_without_fallback(
    payload: ImagePayload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _Responses(None)
    monkeypatch.setattr(
        gpt,
        "_get_client",
        lambda: _Client(responses=responses),
    )

    with pytest.raises(
        ValueError,
        match=r"^provider returned no parsed receipt payload$",
    ):
        gpt.extract_receipt_info(payload)

    assert len(responses.calls) == 1
