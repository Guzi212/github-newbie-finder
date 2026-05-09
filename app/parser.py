"""Requirement parser: NL → RequirementParseResult.

A single LLM call. The system prompt + schema enforcement live in `app.llm`,
so this module is small.
"""

from __future__ import annotations

from .llm import call_structured
from .schemas import RequirementParseResult, UserProfile


def parse_requirement(
    raw_text: str,
    user_profile: UserProfile | None = None,
) -> RequirementParseResult:
    payload: dict = {"raw_text": raw_text}
    if user_profile is not None:
        payload["user_profile"] = user_profile.model_dump()
    return call_structured("parse_requirement", payload, RequirementParseResult)
