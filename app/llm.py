"""Vultr Serverless Inference client with explicit model configuration."""
import json
import os
import re

import httpx
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

BASE = os.getenv(
    "VULTR_INFERENCE_URL",
    "https://api.vultrinference.com/v1",
).rstrip("/")

MODEL = os.getenv("VULTR_MODEL", "").strip()


def _headers():
    key = os.getenv("VULTR_INFERENCE_KEY", "").strip()
    if not key:
        raise RuntimeError("VULTR_INFERENCE_KEY is not configured.")
    return {"Authorization": f"Bearer {key}"}


def models():
    response = httpx.get(
        f"{BASE}/models",
        headers=_headers(),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("Invalid model-list response.")

    return [
        item["id"]
        for item in payload["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def chat(messages):
    if not MODEL:
        raise RuntimeError("VULTR_MODEL must be explicitly configured.")

    response = httpx.post(
        f"{BASE}/chat/completions",
        headers=_headers(),
        timeout=120,
        json={
            "model": MODEL,
            "messages": messages,
            "temperature": 0.1,
        },
    )
    response.raise_for_status()
    payload = response.json()

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Invalid chat-completions response.") from exc

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Inference provider returned empty content.")

    return content


def extract_json(text):
    """Parse a JSON value, optionally enclosed in a Markdown code fence."""
    if not isinstance(text, str):
        raise ValueError("Expected a string containing JSON.")

    candidate = text.strip()
    candidate = re.sub(
        r"\A```(?:json)?\s*|\s*```\Z",
        "",
        candidate,
        flags=re.IGNORECASE,
    ).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()

        for index, char in enumerate(candidate):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
                return value
            except json.JSONDecodeError:
                continue

    raise ValueError("No valid JSON value found in model response.")


def extract_code(text):
    """Accept only an explicitly fenced Python code block."""
    if not isinstance(text, str):
        raise ValueError("Expected a string containing Python code.")

    match = re.search(
        r"```python\s*\n(.*?)```",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError("Model did not return a fenced Python code block.")

    code = match.group(1).strip()
    if not code:
        raise ValueError("Model returned an empty Python code block.")

    return code
