"""Strict domain schemas shared by live extraction and offline replay."""

from __future__ import annotations

import unicodedata
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExpenseCategory(StrEnum):
    """Closed expense taxonomy used by the extraction contract."""

    MEALS = "Meals"
    TRANSPORT = "Transport"
    LODGING = "Lodging"
    OFFICE_SUPPLIES = "Office Supplies"
    ENTERTAINMENT = "Entertainment"
    OTHER = "Other"


class ReceiptFields(BaseModel):
    """Required-but-nullable receipt fields returned by every provider."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    date: (
        Annotated[
            str,
            Field(
                max_length=64,
                description="Receipt date exactly as printed, without inference.",
            ),
        ]
        | None
    )
    amount: (
        Annotated[
            str,
            Field(
                max_length=64,
                description=(
                    "Total amount exactly as printed, including currency marks."
                ),
            ),
        ]
        | None
    )
    vendor: (
        Annotated[
            str,
            Field(
                max_length=200,
                description="Merchant or vendor name exactly as shown.",
            ),
        ]
        | None
    )
    category: ExpenseCategory | None

    @field_validator("date", "amount", "vendor")
    @classmethod
    def reject_unsafe_text(cls, value: str | None) -> str | None:
        """Reject hidden controls and normalize blank model fields to null."""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if any(
            unicodedata.category(character).startswith("C") for character in stripped
        ):
            raise ValueError("receipt text contains a control or format character")
        return stripped
