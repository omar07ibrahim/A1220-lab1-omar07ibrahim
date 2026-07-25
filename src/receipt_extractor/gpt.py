"""OpenAI client wrapper for extracting receipt information."""

from __future__ import annotations

import json
from typing import Any, cast

from openai import OpenAI

from receipt_extractor.file_io import ImagePayload

CATEGORIES = [
    "Meals",
    "Transport",
    "Lodging",
    "Office Supplies",
    "Entertainment",
    "Other",
]


def _get_client() -> OpenAI:
    """Create an OpenAI client from environment configuration.

    Returns:
        OpenAI: Initialized OpenAI client.
    """
    return OpenAI(max_retries=2, timeout=30.0)


def extract_receipt_info(image: ImagePayload) -> dict[str, Any]:
    """Extract receipt fields from one validated image using an LLM.

    Args:
        image (ImagePayload): Validated bytes with their detected media type.

    Returns:
        dict: Parsed JSON with keys: date, amount, vendor, category.

    Raises:
        json.JSONDecodeError: If the model response is not valid JSON.
    """
    client = _get_client()
    prompt = f"""
You are an information extraction system.
Extract ONLY the following fields from the receipt image:

date: the receipt date as a string
amount: the total amount paid as it appears on the receipt
vendor: the merchant or vendor name
category: one of [{", ".join(CATEGORIES)}]

Return EXACTLY one JSON object with these four keys and NOTHING ELSE.
Do not include explanations, comments, or formatting.
Do not wrap the JSON in markdown.
If a field cannot be determined, use null.

The output must be valid JSON.
"""
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        seed=43,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image.data_url()}},
                ],
            }
        ],
        max_completion_tokens=300,
    )
    content = response.choices[0].message.content
    if content is None:
        raise ValueError("provider returned no receipt payload")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise TypeError("provider returned a non-object receipt payload")
    return cast(dict[str, Any], parsed)
