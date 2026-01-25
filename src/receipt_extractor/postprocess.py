"""Post-processing helpers for model outputs."""


def normalize_amount(receipt_data):
    """Normalize the amount field by removing '$' and casting to float.

    Args:
        receipt_data (dict): Parsed receipt data containing an "amount" field.

    Returns:
        dict: Updated receipt data with "amount" converted to float when possible.
            If conversion fails, "amount" is set to None.
    """
    if not isinstance(receipt_data, dict):
        return receipt_data

    amount = receipt_data.get("amount")
    if amount is None:
        return receipt_data

    if isinstance(amount, (int, float)):
        receipt_data["amount"] = float(amount)
        return receipt_data

    if isinstance(amount, str):
        cleaned = amount.replace("$", "").strip()
        try:
            receipt_data["amount"] = float(cleaned)
        except ValueError:
            receipt_data["amount"] = None
        return receipt_data

    receipt_data["amount"] = None
    return receipt_data
