"""Opt-in OpenAI Responses API provider for cited research synthesis."""

from __future__ import annotations

import json as jsonlib
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.config import (
    SYNTHESIS_API_BASE_URL,
    SYNTHESIS_MAX_OUTPUT_TOKENS,
    SYNTHESIS_MODEL,
)


CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "classification": {
            "type": "string",
            "enum": ["sourced_fact", "interpretation"],
        },
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "content_hash": {"type": "string"},
                },
                "required": ["url", "content_hash"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["text", "classification", "citations"],
    "additionalProperties": False,
}

SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "business_overview": {"type": "array", "items": CLAIM_SCHEMA},
        "growth_drivers": {"type": "array", "items": CLAIM_SCHEMA},
        "risks": {"type": "array", "items": CLAIM_SCHEMA},
        "recent_developments": {"type": "array", "items": CLAIM_SCHEMA},
        "unanswered_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "business_overview", "growth_drivers", "risks",
        "recent_developments", "unanswered_questions",
    ],
    "additionalProperties": False,
}


class OpenAIResearchProvider:
    """Generate strict, source-constrained research through Responses API."""

    name = "openai"
    requires_evidence = True

    def __init__(
        self,
        api_key: str,
        model: str = SYNTHESIS_MODEL,
        maximum_output_tokens: int = SYNTHESIS_MAX_OUTPUT_TOKENS,
        api_base_url: str = SYNTHESIS_API_BASE_URL,
        post: Callable[..., object] | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for synthesis.")
        self.api_key = api_key
        self.model = model
        self.maximum_output_tokens = maximum_output_tokens
        self.api_base_url = api_base_url.rstrip("/")
        self.post = post or _post_json
        self.cache_identity = f"openai:{model}"
        self.last_usage: dict | None = None

    def generate(self, packet: dict, prompt: str) -> dict:
        self.last_usage = None
        response = self.post(
            f"{self.api_base_url}/responses",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "Produce a cautious equity research brief using only supplied "
                            "evidence. Never provide an investment recommendation."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_output_tokens": self.maximum_output_tokens,
                "store": False,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "discovery_research_brief",
                        "strict": True,
                        "schema": SYNTHESIS_SCHEMA,
                    }
                },
            },
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        self.last_usage = _normalized_usage(payload.get("usage"))
        text = _response_text(payload)
        result = jsonlib.loads(text)
        if not isinstance(result, dict):
            raise ValueError("OpenAI synthesis response must be a JSON object.")
        return result


def _normalized_usage(value: object) -> dict | None:
    """Return stable token counters from a Responses API usage payload."""
    if not isinstance(value, dict):
        return None
    usage = {
        "input_tokens": int(value.get("input_tokens") or 0),
        "output_tokens": int(value.get("output_tokens") or 0),
        "total_tokens": int(value.get("total_tokens") or 0),
    }
    return usage if any(usage.values()) else None


def _response_text(payload: dict) -> str:
    if payload.get("status") == "incomplete":
        details = payload.get("incomplete_details") or {}
        reason = details.get("reason") or "unknown reason"
        raise ValueError(f"OpenAI synthesis response was incomplete: {reason}")
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "refusal":
                raise ValueError(f"OpenAI refused synthesis: {content.get('refusal', '')}")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    raise ValueError("OpenAI response did not contain output text.")


class _UrlResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def _post_json(url: str, headers: dict, json: dict, timeout: int) -> _UrlResponse:
    request = Request(
        url,
        data=jsonlib.dumps(json).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = jsonlib.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API returned HTTP {error.code}: {detail}") from error
    return _UrlResponse(payload)
