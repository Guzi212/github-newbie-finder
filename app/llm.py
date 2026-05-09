"""LLM provider abstraction.

All structured LLM calls go through `call_structured(kind, prompt, schema)`,
which:

- dispatches to the configured provider
- enforces JSON-only output (provider-specific JSON mode where available,
  prompt instructions otherwise)
- validates the result against a Pydantic schema
- persists the prompt + response to `llm_calls` for replay/debugging

Providers:

- `EchoProvider` — deterministic stub. Builds a valid schema instance from the
  input. No API key needed; the entire pipeline is demo-able with `LLM_PROVIDER=echo`.
- `DeepSeekProvider` — recommended real provider. Uses OpenAI SDK with DeepSeek
  base URL and JSON response format.
- `OpenAIProvider` — OpenAI JSON mode.
- `AnthropicProvider` — Anthropic tool-use to enforce JSON shape.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Type, TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import db
from .config import get_settings
from .schemas import (
    Evidence,
    LLMCallRecord,
    RepoAnalysisResult,
    RequirementParseResult,
    RerankBatchResult,
    TutorialError,
    TutorialPlan,
    TutorialStep,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

LLMKind = str  # "parse_requirement" | "rerank_repos" | "generate_tutorial"


_PARSE_SKELETON = """{
  "intent": "<one-sentence restatement of the user's goal in their language>",
  "must_have": ["..."],
  "nice_to_have": ["..."],
  "avoid": ["..."],
  "tech_stack": ["python", "react", "..."],
  "deployment": "local | docker | cloud | any",
  "skill_level": "beginner | intermediate | advanced",
  "keywords_en": ["english search terms — translate Chinese ones"],
  "keywords_zh": ["原始中文关键词"],
  "confidence": 0.0
}"""

_RERANK_SKELETON = """{
  "analyses": [
    {
      "full_name": "owner/repo",
      "summary": "1-2 sentence summary",
      "fit_score": 0.0,            // 0..1
      "beginner_score": 0.0,       // 0..1
      "deployment_level": "S1 | S2 | S3 | S4 | S5",
      "pros": ["short, evidence-backed"],
      "cons": ["short"],
      "differentiators": ["what makes this repo different from the others"],
      "risk_flags": ["e.g. requires GPU, archived, requires API key"],
      "missing_info": ["things you would need to confirm"],
      "evidence": [
        {"kind": "readme | metadata | topic", "excerpt": "<=600 chars from the candidate's README/description/topics", "source_url": "https://github.com/..."}
      ]
    }
  ]
}"""

_TUTORIAL_SKELETON = """{
  "repo_full_name": "owner/repo",
  "assumptions": ["e.g. macOS, Docker installed"],
  "prerequisites": ["Python 3.10+", "git"],
  "steps": [
    {
      "title": "Step title",
      "commands": ["one shell command per array entry"],
      "explanation": "why this step",
      "needs_verification": false
    }
  ],
  "verification": ["how to confirm it works"],
  "common_errors": [
    {"symptom": "...", "cause": "...", "fix": "..."}
  ],
  "rollback": ["how to undo"],
  "next_steps": ["follow-on suggestions"]
}"""


SYSTEM_PROMPTS: dict[str, str] = {
    "parse_requirement": (
        "You are a requirement parser for a GitHub project finder aimed at beginners.\n"
        "Read the user's natural-language description and produce a JSON object with EXACTLY this shape:\n"
        f"{_PARSE_SKELETON}\n"
        "Rules:\n"
        "- ALWAYS populate every field. If a list is empty, use [].\n"
        "- `intent` is required and must be a single sentence in the user's language.\n"
        "- Be conservative with `must_have`; only items the user truly insists on. "
        "Soft preferences go to `nice_to_have`. Things the user said NOT to use go to `avoid`.\n"
        "- `keywords_en` MUST contain English terms (translate Chinese ones) so we can search GitHub broadly. "
        "Aim for 5-10 keywords.\n"
        "- `confidence` is your own 0..1 estimate of how clear the request was.\n"
        "Output ONLY the JSON object. No prose, no markdown, no code fences."
    ),
    "rerank_repos": (
        "You are a senior open-source curator. For each candidate repository, decide how well it matches "
        "the user's parsed requirement, especially how friendly it is for a beginner to deploy.\n"
        "Use ONLY the evidence provided (description, topics, README excerpt, install signals). "
        "For every claim in pros/cons/risks, the evidence array must include the supporting excerpt.\n"
        "Output a JSON object with EXACTLY this shape:\n"
        f"{_RERANK_SKELETON}\n"
        "Rules:\n"
        "- Return one analysis per candidate, with full_name copied verbatim.\n"
        "- `summary` MUST be a clear 1-2 sentence description of WHAT THE PROJECT DOES, in the user's "
        "language when possible. This is shown to the user as the project's elevator pitch.\n"
        "- `fit_score` is STRICT. If the candidate is a *related-area tool but the wrong product form* "
        "(e.g. user asked for a 'RAG knowledge base with Web UI' and the candidate is a developer plugin / "
        "library / SDK / agent extension), fit_score MUST be <= 0.30 and you MUST include a `risk_flag` "
        "naming the form mismatch. Do not reward popularity in fit_score.\n"
        "- `pros` and `cons` should each be 2-4 short bullets that a beginner would actually care about.\n"
        "- Always include >=1 evidence entry per analysis (a short README excerpt is fine).\n"
        "- Output ONLY the JSON object."
    ),
    "generate_tutorial": (
        "You are a patient technical mentor writing a deployment tutorial for a beginner.\n"
        "Use ONLY the evidence in the snapshot. If a step is not directly supported by README or config "
        "files, set needs_verification=true on that step. Tailor commands to the user's OS and skills. "
        "Never invent commands.\n"
        "Output a JSON object with EXACTLY this shape:\n"
        f"{_TUTORIAL_SKELETON}\n"
        "Rules:\n"
        "- repo_full_name MUST match the snapshot's meta.full_name.\n"
        "- Each command in `commands` is one shell line. Multiple lines = multiple array entries.\n"
        "- Output ONLY the JSON object."
    ),
}


class LLMProvider(ABC):
    name: str = "base"

    def __init__(self, model: str = "") -> None:
        self.model = model or self.default_model()

    @abstractmethod
    def default_model(self) -> str: ...

    @abstractmethod
    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        """Return the raw JSON object the model produced."""


# ---------------------------------------------------------------------------
# Echo provider — deterministic fallback so the pipeline runs with no keys.
# ---------------------------------------------------------------------------


class EchoProvider(LLMProvider):
    name = "echo"

    def default_model(self) -> str:
        return "echo-1"

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        try:
            payload = json.loads(user)
        except Exception:
            payload = {"raw_text": user}
        if "RequirementParseResult" in system:
            return _echo_parse(payload.get("raw_text", "") or user)
        if "RerankBatchResult" in system:
            return _echo_rerank(payload)
        if "TutorialPlan" in system:
            return _echo_tutorial(payload)
        return {}


_STOPWORDS = {
    "i", "want", "to", "a", "an", "the", "for", "of", "and", "or", "with",
    "on", "in", "is", "are", "be", "have", "find", "looking", "looking-for",
    "我", "想", "要", "找", "一个", "适合", "并", "可以", "希望", "最好",
    "用", "做", "的", "和", "或者", "需要", "项目", "工具",
}


def _tokenize(text: str) -> list[str]:
    parts = re.split(r"[\s，。,.!?;；:：()（）/\\\-_]+", text.lower())
    return [p for p in parts if p and p not in _STOPWORDS and len(p) > 1]


_ZH_TO_EN_HINTS = {
    "知识库": ["knowledge base", "rag", "document chat"],
    "问答": ["qa", "chatbot"],
    "本地": ["local", "self-hosted"],
    "部署": ["deploy", "self host"],
    "新手": ["beginner friendly", "easy setup"],
    "图片": ["image"],
    "压缩": ["compression"],
    "管理": ["manager"],
    "发票": ["invoice"],
    "识别": ["ocr", "recognition"],
    "报销": ["expense"],
    "笔记": ["notes"],
    "插件": ["plugin"],
    "聊天": ["chat"],
    "ui": ["web ui", "gui"],
}


def _echo_parse(raw: str) -> dict[str, Any]:
    tokens = _tokenize(raw)
    keywords_zh = [t for t in tokens if any(ord(c) > 127 for c in t)][:8]
    keywords_en = [t for t in tokens if all(ord(c) < 128 for c in t)][:8]
    for zh, ens in _ZH_TO_EN_HINTS.items():
        if zh in raw:
            for e in ens:
                if e not in keywords_en:
                    keywords_en.append(e)

    deployment = "any"
    if "docker" in raw.lower() and ("不" in raw or "no docker" in raw.lower() or "without docker" in raw.lower()):
        deployment = "local"
    elif "docker" in raw.lower():
        deployment = "docker"
    elif "本地" in raw or "local" in raw.lower():
        deployment = "local"

    must_have, nice, avoid = [], [], []
    if "web ui" in raw.lower() or "ui" in keywords_en or "界面" in raw:
        nice.append("web UI")
    if "gpu" in raw.lower():
        nice.append("GPU optional")
    if "新手" in raw or "beginner" in raw.lower() or "小白" in raw:
        must_have.append("beginner friendly setup")
    if "不" in raw and "docker" in raw.lower():
        avoid.append("Docker required")

    return {
        "intent": raw.strip()[:200] or "Find a relevant open-source project.",
        "must_have": must_have,
        "nice_to_have": nice,
        "avoid": avoid,
        "tech_stack": [],
        "deployment": deployment,
        "skill_level": "beginner",
        "keywords_en": keywords_en or ["open source"],
        "keywords_zh": keywords_zh,
        "confidence": 0.55,
    }


def _echo_rerank(data: dict[str, Any]) -> dict[str, Any]:
    """Build a stub RerankBatchResult by reading the parsed payload."""
    if not isinstance(data, dict):
        return {"analyses": []}
    analyses = []
    for cand in data.get("candidates", []):
        full = cand.get("full_name", "")
        readme_excerpt = (cand.get("readme_excerpt") or "")[:400]
        signals = cand.get("install_signals") or {}
        beginner = 0.7 if signals.get("has_compose") or signals.get("has_one_click_script") else 0.45
        if signals.get("needs_gpu"):
            beginner -= 0.15
        deployment_level = (
            "S1" if signals.get("has_compose")
            else "S2" if signals.get("has_dockerfile") or signals.get("package_managers")
            else "S3" if signals.get("needs_database")
            else "S4" if signals.get("needs_gpu")
            else "S2"
        )
        pros = []
        if signals.get("has_compose"):
            pros.append("Has docker-compose for one-command startup.")
        if signals.get("has_screenshots"):
            pros.append("README includes screenshots/demo.")
        cons = []
        if signals.get("needs_gpu"):
            cons.append("Requires GPU.")
        if signals.get("needs_api_key"):
            cons.append("Requires external API keys.")
        ev = []
        if readme_excerpt:
            ev.append({
                "kind": "readme",
                "excerpt": readme_excerpt,
                "source_url": cand.get("url"),
            })
        analyses.append({
            "full_name": full,
            "summary": (cand.get("description") or full)[:200],
            "fit_score": min(1.0, max(0.2, cand.get("rule_total", 50) / 100)),
            "beginner_score": max(0.1, min(1.0, beginner)),
            "deployment_level": deployment_level,
            "pros": pros or ["Active repository."],
            "cons": cons,
            "differentiators": [],
            "risk_flags": ["Tutorial generated by deterministic stub."]
                if not pros else [],
            "missing_info": [],
            "evidence": ev,
        })
    return {"analyses": analyses}


def _echo_tutorial(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    snap = data.get("snapshot", {})
    full = snap.get("meta", {}).get("full_name", "")
    signals = snap.get("install_signals", {})
    profile = data.get("user_profile", {})
    os_name = profile.get("os", "macos")
    has_docker = profile.get("has_docker", False)

    steps: list[dict[str, Any]] = []
    prereqs: list[str] = []
    if signals.get("has_compose") and has_docker:
        prereqs += ["Docker Desktop", "Docker Compose v2"]
        steps.append({
            "title": "Clone the repo",
            "commands": [f"git clone https://github.com/{full}.git", f"cd {full.split('/')[-1]}"],
            "explanation": "Get the source.",
            "needs_verification": False,
        })
        steps.append({
            "title": "Start with docker compose",
            "commands": ["docker compose up -d"],
            "explanation": "Bring up the stack defined in compose.yaml.",
            "needs_verification": True,
        })
    elif "pip" in (signals.get("package_managers") or []):
        prereqs += [f"Python 3.10+ on {os_name}"]
        steps.append({
            "title": "Clone and create a venv",
            "commands": [
                f"git clone https://github.com/{full}.git",
                f"cd {full.split('/')[-1]}",
                "python -m venv .venv",
                "source .venv/bin/activate" if os_name != "windows" else ".venv\\Scripts\\activate",
            ],
            "explanation": "Isolate dependencies.",
            "needs_verification": False,
        })
        steps.append({
            "title": "Install Python dependencies",
            "commands": ["pip install -r requirements.txt"],
            "explanation": "Install the project's pinned dependencies.",
            "needs_verification": True,
        })
    else:
        prereqs.append("Read the project README for prerequisites.")
        steps.append({
            "title": "Inspect the README",
            "commands": [f"open https://github.com/{full}#readme"],
            "explanation": "The deterministic stub could not detect a clear install path; review the README first.",
            "needs_verification": True,
        })

    verification = ["Confirm the service responds on the documented port (often 3000/8080).",
                    "Check logs for startup errors."]
    common_errors = [
        {"symptom": "Port already in use",
         "cause": "Another process is bound to the same port.",
         "fix": "Stop the other process or set the port via the project's config."}
    ]

    return {
        "repo_full_name": full,
        "assumptions": [f"User OS = {os_name}", f"User skill = {profile.get('skill_level', 'beginner')}"],
        "prerequisites": prereqs,
        "steps": steps,
        "verification": verification,
        "common_errors": common_errors,
        "rollback": ["docker compose down -v" if signals.get("has_compose") else "Remove the cloned directory."],
        "next_steps": ["Star the repo if it works.", "Open an issue if a step fails."],
    }


# ---------------------------------------------------------------------------
# Real providers
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider(LLMProvider):
    """Shared logic for OpenAI + DeepSeek (DeepSeek's API is OpenAI-compatible)."""

    base_url: str | None = None
    api_key: str = ""
    request_timeout: float = 45.0  # seconds per attempt

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        return self._call_with_retry(system, user)

    @retry(
        reraise=True,
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _call_with_retry(self, system: str, user: str) -> dict[str, Any]:
        from openai import OpenAI

        kwargs = {"api_key": self.api_key, "timeout": self.request_timeout, "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        text = resp.choices[0].message.content or "{}"
        return _safe_json_loads(text)


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"

    def __init__(self, model: str = "", *, api_key: str = "", base_url: str = "") -> None:
        s = get_settings()
        self.api_key = api_key or s.openai_api_key
        if base_url:
            self.base_url = base_url
        super().__init__(model)

    def default_model(self) -> str:
        return get_settings().llm_model or "gpt-4o-mini"


class DeepSeekProvider(OpenAICompatibleProvider):
    name = "deepseek"

    def __init__(self, model: str = "", *, api_key: str = "", base_url: str = "") -> None:
        s = get_settings()
        self.api_key = api_key or s.deepseek_api_key
        self.base_url = base_url or s.deepseek_base_url
        super().__init__(model)

    def default_model(self) -> str:
        return get_settings().llm_model or "deepseek-chat"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str = "", *, api_key: str = "", base_url: str = "") -> None:
        s = get_settings()
        self.api_key = api_key or s.anthropic_api_key
        # base_url accepted for API symmetry; Anthropic SDK ignores it here.
        super().__init__(model)

    def default_model(self) -> str:
        return get_settings().llm_model or "claude-sonnet-4-6"

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        return self._call_with_retry(system, user)

    @retry(
        reraise=True,
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _call_with_retry(self, system: str, user: str) -> dict[str, Any]:
        from anthropic import Anthropic

        client = Anthropic(api_key=self.api_key, timeout=45.0, max_retries=0)
        # Force JSON via prompt; Claude is reliable about following this.
        msg = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system + "\nReturn ONLY a JSON object. No prose, no fences.",
            messages=[{"role": "user", "content": user}],
            temperature=0.2,
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _safe_json_loads(text)


def _safe_json_loads(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract the first {...} block.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


_provider_cache: dict[str, LLMProvider] = {}


def _build_provider(name: str, *, model: str = "", api_key: str = "", base_url: str = "") -> LLMProvider:
    n = name.lower()
    if n == "echo":
        return EchoProvider()
    if n == "openai":
        return OpenAIProvider(model=model, api_key=api_key, base_url=base_url)
    if n == "deepseek":
        return DeepSeekProvider(model=model, api_key=api_key, base_url=base_url)
    if n == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key, base_url=base_url)
    log.warning("Unknown LLM provider %r, falling back to echo", n)
    return EchoProvider()


def get_provider() -> LLMProvider:
    """Resolve the active LLM provider.

    Preference order:
      1. The credential marked active in the `api_credentials` table (UI-managed).
      2. Fallback to environment-defined provider in `Settings`.

    The cache key includes the active credential id so flipping which
    credential is active surfaces immediately on the next call.
    """
    cred = None
    try:
        cred = db.get_active_credential()
    except Exception as e:  # pragma: no cover — DB not yet initialised
        log.debug("Could not read active credential: %s", e)

    if cred:
        cache_key = f"db:{cred['id']}:{cred['provider']}:{cred.get('model','')}"
        if cache_key in _provider_cache:
            return _provider_cache[cache_key]
        provider = _build_provider(
            cred["provider"],
            model=cred.get("model") or "",
            api_key=cred.get("api_key") or "",
            base_url=cred.get("base_url") or "",
        )
        _provider_cache[cache_key] = provider
        return provider

    s = get_settings()
    cache_key = f"env:{s.llm_provider}:{s.llm_model}"
    if cache_key in _provider_cache:
        return _provider_cache[cache_key]
    provider = _build_provider(s.llm_provider, model=s.llm_model)
    _provider_cache[cache_key] = provider
    return provider


def clear_provider_cache() -> None:
    """Invalidate cached providers — call after credentials change."""
    _provider_cache.clear()


def call_structured(
    kind: LLMKind,
    user_payload: dict[str, Any],
    schema: Type[T],
) -> T:
    """Call the configured LLM and validate the response against `schema`.

    Side effect: the call is persisted to `llm_calls` for replay.
    """
    provider = get_provider()
    system = SYSTEM_PROMPTS[kind] + f"\nSchema name: {schema.__name__}"
    user_text = json.dumps(user_payload, ensure_ascii=False)

    started = time.perf_counter()
    raw = provider.complete_json(system, user_text)
    latency_ms = int((time.perf_counter() - started) * 1000)

    try:
        validated = schema.model_validate(raw)
    except ValidationError:
        # Salvage path: try to coerce missing fields with sensible defaults
        log.warning("LLM output failed schema validation; attempting fallback for %s", schema.__name__)
        validated = schema.model_validate(_coerce_for_schema(raw, schema))

    db.save_llm_call(LLMCallRecord(
        kind=kind,
        provider=provider.name,
        model=provider.model,
        prompt={"system": system, "user": user_payload},
        response=raw,
        latency_ms=latency_ms,
        created_at=datetime.utcnow(),
    ))

    return validated


def _coerce_for_schema(raw: dict[str, Any], schema: Type[BaseModel]) -> dict[str, Any]:
    """Fill in defaults for known schemas if the model omitted required fields."""
    if schema is RequirementParseResult:
        return {
            "intent": raw.get("intent") or "",
            "must_have": raw.get("must_have", []),
            "nice_to_have": raw.get("nice_to_have", []),
            "avoid": raw.get("avoid", []),
            "tech_stack": raw.get("tech_stack", []),
            "deployment": raw.get("deployment", "any"),
            "skill_level": raw.get("skill_level", "beginner"),
            "keywords_en": raw.get("keywords_en", []),
            "keywords_zh": raw.get("keywords_zh", []),
            "confidence": float(raw.get("confidence", 0.4)),
        }
    if schema is RerankBatchResult:
        return {"analyses": raw.get("analyses", [])}
    if schema is TutorialPlan:
        return {
            "repo_full_name": raw.get("repo_full_name", ""),
            "assumptions": raw.get("assumptions", []),
            "prerequisites": raw.get("prerequisites", []),
            "steps": raw.get("steps", []),
            "verification": raw.get("verification", []),
            "common_errors": raw.get("common_errors", []),
            "rollback": raw.get("rollback", []),
            "next_steps": raw.get("next_steps", []),
        }
    return raw


def replay(call_id: int) -> dict[str, Any]:
    """Re-run a stored LLM call (used by `app.cli replay`)."""
    record = db.get_llm_call(call_id)
    if not record:
        raise SystemExit(f"No llm_call with id={call_id}")
    provider = get_provider()
    system = record.prompt.get("system", "")
    user = json.dumps(record.prompt.get("user", {}), ensure_ascii=False)
    return provider.complete_json(system, user)
