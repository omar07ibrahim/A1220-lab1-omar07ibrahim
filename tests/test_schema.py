from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from receipt_extractor.schema import ExpenseCategory, ReceiptFields


def _valid_fields() -> dict[str, object]:
    return {
        "date": "2026-07-24",
        "amount": "$12.50",
        "vendor": "Synthetic",
        "category": ExpenseCategory.OTHER,
    }


def test_all_fields_are_required_but_nullable() -> None:
    receipt = ReceiptFields(
        date=None,
        amount=None,
        vendor=None,
        category=None,
    )

    assert receipt.model_dump() == {
        "date": None,
        "amount": None,
        "vendor": None,
        "category": None,
    }


@pytest.mark.parametrize("missing_field", ["date", "amount", "vendor", "category"])
def test_missing_field_is_rejected(missing_field: str) -> None:
    fields = _valid_fields()
    del fields[missing_field]

    with pytest.raises(ValidationError):
        ReceiptFields.model_validate(fields)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", 20260724),
        ("amount", 12.50),
        ("vendor", 123),
    ],
)
def test_scalar_coercion_is_rejected(field: str, value: object) -> None:
    fields = _valid_fields()
    fields[field] = value

    with pytest.raises(ValidationError):
        ReceiptFields.model_validate(fields)


def test_extra_field_is_rejected() -> None:
    fields = _valid_fields()
    fields["currency"] = "USD"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReceiptFields.model_validate(fields)


def test_unknown_category_is_rejected_from_provider_json() -> None:
    payload = {
        "date": "2026-07-24",
        "amount": "$12.50",
        "vendor": "Synthetic",
        "category": "Travel",
    }

    with pytest.raises(ValidationError):
        ReceiptFields.model_validate_json(json.dumps(payload))


def test_allowed_category_parses_from_provider_json() -> None:
    payload = {
        "date": "2026-07-24",
        "amount": "$12.50",
        "vendor": "Synthetic",
        "category": "Transport",
    }

    receipt = ReceiptFields.model_validate_json(json.dumps(payload))

    assert receipt.category is ExpenseCategory.TRANSPORT


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("date", "2026-07-24\u0000"),
        ("amount", "$12.50\nUSD"),
        ("vendor", "Syn\u200bthetic"),
    ],
)
def test_control_and_format_characters_are_rejected(
    field: str,
    unsafe_value: str,
) -> None:
    fields = _valid_fields()
    fields[field] = unsafe_value

    with pytest.raises(
        ValidationError,
        match="receipt text contains a control or format character",
    ):
        ReceiptFields.model_validate(fields)


def test_safe_text_is_trimmed_and_blank_text_becomes_null() -> None:
    receipt = ReceiptFields(
        date=" 2026-07-24 ",
        amount=" \t ",
        vendor=" Synthetic ",
        category=ExpenseCategory.OTHER,
    )

    assert receipt.date == "2026-07-24"
    assert receipt.amount is None
    assert receipt.vendor == "Synthetic"
