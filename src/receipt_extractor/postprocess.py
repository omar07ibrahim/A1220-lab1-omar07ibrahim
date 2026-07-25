"""Post-processing helpers for model outputs."""

from __future__ import annotations

import math
from typing import Any


def normalize_amount(receipt_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize the amount field by removing '$' and casting to float.

    Args:
        receipt_data (dict): Parsed receipt data containing an "amount" field.

    Returns:
        dict: Updated receipt data with "amount" converted to float when possible.
            If conversion fails, "amount" is set to None.
    """
    amount = receipt_data.get("amount")
    if amount is None:
        return receipt_data

    if isinstance(amount, bool):
        receipt_data["amount"] = None
        return receipt_data

    normalized: float
    if isinstance(amount, (int, float)):
        try:
            normalized = float(amount)
        except OverflowError:
            receipt_data["amount"] = None
            return receipt_data
    if isinstance(amount, str):
        cleaned = amount.replace("$", "").strip()
        try:
            normalized = float(cleaned)
        except (OverflowError, ValueError):
            receipt_data["amount"] = None
            return receipt_data
    elif not isinstance(amount, (int, float)):
        receipt_data["amount"] = None
        return receipt_data

    receipt_data["amount"] = normalized if math.isfinite(normalized) else None
    return receipt_data
