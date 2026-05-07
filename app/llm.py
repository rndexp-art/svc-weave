"""OpenAI client + prompt-induction helpers used by the watcher and worker."""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI, OpenAIError

from .config import settings


log = logging.getLogger(__name__)


_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        cfg = settings()
        if not cfg.openai_api_key:
            raise OpenAIError("OPENAI_API_KEY is not configured")
        _client = OpenAI(api_key=cfg.openai_api_key)
    return _client


def chat_strict_json(*, system: str, user: str, json_schema: dict[str, Any],
                     model: str | None = None, temperature: float = 0.0) -> dict[str, Any]:
    """One round-trip to OpenAI with strict JSON schema. Returns parsed JSON."""
    cfg = settings()
    client = get_client()
    response = client.chat.completions.create(
        model=model or cfg.openai_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        response_format={"type": "json_schema", "json_schema": json_schema},
        temperature=temperature,
    )
    content = response.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.error("OpenAI returned non-JSON despite strict mode: %s", e)
        raise


# --- prompt induction ------------------------------------------------------

INDUCE_PROMPT_SYSTEM = """You are a meta-prompt engineer. You receive an example of an extraction (or synthesis) that a human or AI agent performed: a source piece of content (or set of inputs) and the structured output they produced from it. Your job is to write two prompts that another agent could use to (1) detect whether a future input contains the same kind of pattern, and (2) extract the same kind of structured output from it.

Rules:
- Be concrete. Refer to the kinds of fields the output has, not generic terms.
- The detector prompt must yield a single boolean (`{"present": true|false}`).
- The extractor prompt must yield JSON conforming to the output_schema you also produce.
- Keep prompts under 800 words each.
- Do not embed the example verbatim; generalize from it.
"""


_INDUCE_SCHEMA = {
    "name": "induced_pattern",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["pattern_key_suggestion", "detector_prompt", "extractor_prompt", "output_schema_json", "rationale"],
        "properties": {
            "pattern_key_suggestion": {"type": "string"},
            "detector_prompt":        {"type": "string"},
            "extractor_prompt":       {"type": "string"},
            "output_schema_json":     {"type": "string"},   # stringified JSON Schema
            "rationale":              {"type": "string"},
        },
    },
}


_DECIDE_SCHEMA = {
    "name": "prompt_update_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "revised_extractor_prompt", "revised_detector_prompt", "rationale"],
        "properties": {
            "decision":                 {"type": "string"},   # "keep" | "update"
            "revised_extractor_prompt": {"type": ["string", "null"]},
            "revised_detector_prompt":  {"type": ["string", "null"]},
            "rationale":                {"type": "string"},
        },
    },
}


def induce_pattern(*, source_repr: str, output_repr: str) -> dict[str, Any]:
    """Generate a brand-new prompt pair from a single example."""
    user = (
        "SOURCE INPUT:\n"
        f"{source_repr}\n\n"
        "OUTPUT THAT WAS PRODUCED:\n"
        f"{output_repr}\n"
    )
    return chat_strict_json(system=INDUCE_PROMPT_SYSTEM, user=user, json_schema=_INDUCE_SCHEMA)


_DECIDE_SYSTEM = """You are reviewing an existing extraction prompt against a new example. Decide if the prompt would have correctly handled this case as-is, or if it needs an update.

If the existing prompt would have produced the same output, respond `keep`. Otherwise respond `update` with revised detector and extractor prompts that would handle BOTH the existing pattern AND the new example. Keep prompts under 800 words.
"""


def decide_keep_or_update(*, existing_detector: str, existing_extractor: str,
                          source_repr: str, output_repr: str) -> dict[str, Any]:
    user = (
        f"EXISTING DETECTOR PROMPT:\n{existing_detector}\n\n"
        f"EXISTING EXTRACTOR PROMPT:\n{existing_extractor}\n\n"
        f"NEW SOURCE INPUT:\n{source_repr}\n\n"
        f"NEW OUTPUT THAT WAS PRODUCED:\n{output_repr}\n"
    )
    return chat_strict_json(system=_DECIDE_SYSTEM, user=user, json_schema=_DECIDE_SCHEMA)


# --- detector + extractor (used by the worker on stored prompts) -----------

_DETECT_SCHEMA = {
    "name": "detector_result",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["present"],
        "properties": {"present": {"type": "boolean"}},
    },
}


def detect(*, prompt: str, content: str) -> bool:
    """Run a detector prompt against a candidate input; returns True if present."""
    parsed = chat_strict_json(system=prompt, user=content, json_schema=_DETECT_SCHEMA)
    return bool(parsed.get("present"))


def extract(*, prompt: str, output_schema: dict[str, Any], content: str) -> dict[str, Any]:
    """Run an extractor prompt against a candidate input. Returns the parsed JSON."""
    return chat_strict_json(system=prompt, user=content, json_schema=output_schema)
