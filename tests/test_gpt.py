from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from receipt_extractor import gpt
from receipt_extractor.file_io import ImagePayload


@dataclass(slots=True)
class _Message:
    content: str | None


@dataclass(slots=True)
class _Choice:
    message: _Message


@dataclass(slots=True)
class _Response:
    choices: list[_Choice]


class _Completions:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return _Response(choices=[_Choice(message=_Message(self.content))])


@dataclass(slots=True)
class _Chat:
    completions: _Completions


@dataclass(slots=True)
class _Client:
    chat: _Chat


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


def test_adapter_sends_typed_data_url_and_parses_object(
    payload: ImagePayload,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = _Completions(
        '{"date":"2026-07-24","amount":"12.50","vendor":"Synthetic","category":"Other"}'
    )
    client = _Client(chat=_Chat(completions=completions))
    monkeypatch.setattr(gpt, "_get_client", lambda: client)

    result = gpt.extract_receipt_info(payload)

    assert result["vendor"] == "Synthetic"
    [call] = completions.calls
    assert call["model"] == "gpt-4.1-mini"
    assert call["seed"] == 43
    assert call["max_completion_tokens"] == 300
    content = call["messages"][0]["content"]
    assert "one of [Meals, Transport" in content[0]["text"]
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": payload.data_url()},
    }


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (None, ValueError),
        ("[]", TypeError),
        ("not-json", json.JSONDecodeError),
    ],
)
def test_adapter_rejects_missing_or_nonobject_payloads(
    payload: ImagePayload,
    monkeypatch: pytest.MonkeyPatch,
    content: str | None,
    error: type[Exception],
) -> None:
    client = _Client(chat=_Chat(completions=_Completions(content)))
    monkeypatch.setattr(gpt, "_get_client", lambda: client)

    with pytest.raises(error):
        gpt.extract_receipt_info(payload)
