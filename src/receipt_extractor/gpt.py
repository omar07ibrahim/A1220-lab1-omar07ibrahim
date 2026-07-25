"""Typed OpenAI Responses adapter for receipt extraction."""

from __future__ import annotations

from typing import Any

from openai import OpenAI
from openai.types.responses import ResponseInputParam

from receipt_extractor.file_io import ImagePayload
from receipt_extractor.schema import ReceiptFields

MODEL = "gpt-4.1-mini"
INSTRUCTIONS = (
    "Extract exactly one receipt. Preserve the date and total amount exactly "
    "as printed. Choose the closest allowed expense category. Use null only "
    "when a field cannot be determined. Do not infer missing text."
)


def _get_client() -> OpenAI:
    """Create a bounded OpenAI client from environment configuration."""
    return OpenAI(max_retries=2, timeout=30.0)


def extract_receipt_info(image: ImagePayload) -> dict[str, Any]:
    """Extract one receipt through the typed Responses API contract."""
    request_input: ResponseInputParam = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Extract the receipt fields from this image.",
                },
                {
                    "type": "input_image",
                    "image_url": image.data_url(),
                    "detail": "high",
                },
            ],
        }
    ]
    response = _get_client().responses.parse(
        model=MODEL,
        instructions=INSTRUCTIONS,
        input=request_input,
        text_format=ReceiptFields,
        max_output_tokens=300,
        store=False,
    )
    parsed = response.output_parsed
    if parsed is None:
        raise ValueError("provider returned no parsed receipt payload")
    return parsed.model_dump(mode="json")
