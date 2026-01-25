"""OpenAI client wrapper for extracting receipt information."""

import json
from openai import OpenAI

CATEGORIES = [
    "Meals",
    "Transport",
    "Lodging",
    "Office Supplies",
    "Entertainment",
    "Other",
]

def _get_client():
    """Create an OpenAI client from environment configuration.

    Returns:
        OpenAI: Initialized OpenAI client.
    """
    return OpenAI()


def extract_receipt_info(image_b64):
    """Extract receipt fields from a base64-encoded image using an LLM.

    Args:
        image_b64 (str): Base64-encoded image contents.

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
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        }
                    }
                ]
            }
        ]
    )
    return json.loads(response.choices[0].message.content)
