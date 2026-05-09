"""All Pydantic models for the GitHub Project Finder MVP.

Schemas are split into three groups:

- LLM-facing schemas: every structured LLM response must validate against one
  of these. They are the contract for `app.llm.LLMProvider.call_structured`.
- Domain schemas: data we move between modules (queries, snapshots, scores,
  recommendations).
- API schemas: request/response wrappers for FastAPI endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Enums (as Literal types for compactness)
# ---------------------------------------------------------------------------

OS = Literal["windows", "macos", "linux", "unknown"]
SkillLevel = Literal["beginner", "intermediate", "advanced"]
DeploymentPreference = Literal["local", "docker", "cloud", "any"]
DeploymentLevel = Literal["S1", "S2", "S3", "S4", "S5"]
EvidenceKind = Literal["readme", "tree", "metadata", "topic", "release", "description"]
FeedbackAction = Literal[
    "favorite", "unfavorite", "not_interested",
    "deploy_success", "deploy_failed", "tutorial_helpful",
]


# ---------------------------------------------------------------------------
# LLM-facing schemas
# ---------------------------------------------------------------------------


class RequirementParseResult(BaseModel):
    """Output of the requirement parser (LLM call #1)."""

    intent: str = Field(..., description="One-sentence restatement of the user's goal.")
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    deployment: DeploymentPreference = "any"
    skill_level: SkillLevel = "beginner"
    keywords_en: list[str] = Field(default_factory=list)
    keywords_zh: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("must_have", "nice_to_have", "avoid", "tech_stack",
                     "keywords_en", "keywords_zh", mode="before")
    @classmethod
    def _coerce_to_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)


class Evidence(BaseModel):
    """A snippet of repository data that supports a recommendation."""

    kind: EvidenceKind
    excerpt: str = Field(..., max_length=600)
    source_url: Optional[str] = None

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, v: Any) -> str:
        """LLMs sometimes invent kinds. Normalise the common ones; otherwise
        fall back to ``metadata`` so a single odd evidence item never breaks
        the whole rerank batch."""
        if not isinstance(v, str):
            return "metadata"
        norm = v.strip().lower()
        aliases = {
            "readme": "readme",
            "tree": "tree",
            "metadata": "metadata",
            "topic": "topic",
            "topics": "topic",
            "release": "release",
            "releases": "release",
            "description": "description",
            "desc": "description",
            "summary": "description",
            "config": "tree",
            "file": "tree",
            "files": "tree",
            "repo": "metadata",
            "repository": "metadata",
            "url": "metadata",
            "license": "metadata",
            "stars": "metadata",
        }
        return aliases.get(norm, "metadata")


class RepoAnalysisResult(BaseModel):
    """Output of the LLM reranker for a single repo (LLM call #2)."""

    full_name: str
    summary: str
    fit_score: float = Field(..., ge=0.0, le=1.0)
    beginner_score: float = Field(..., ge=0.0, le=1.0)
    deployment_level: DeploymentLevel
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("pros", "cons", "differentiators", "risk_flags", "missing_info",
                     mode="before")
    @classmethod
    def _coerce_to_list(cls, v: Any) -> list[str]:
        """LLMs sometimes return a single string for these fields (especially
        when the value is a paragraph). Wrap it so a single bad item doesn't
        invalidate the entire batch and force a fallback to rule-only ranking."""
        if v is None:
            return []
        if isinstance(v, str):
            stripped = v.strip()
            return [stripped] if stripped else []
        return list(v)


class RerankBatchResult(BaseModel):
    """Wrapper so a single LLM call can return analyses for many repos."""

    analyses: list[RepoAnalysisResult]


class TutorialStep(BaseModel):
    title: str
    commands: list[str] = Field(default_factory=list)
    explanation: str = ""
    needs_verification: bool = False


class TutorialError(BaseModel):
    symptom: str
    cause: str = ""
    fix: str = ""


class TutorialPlan(BaseModel):
    """Output of the tutorial generator (LLM call #3)."""

    repo_full_name: str
    assumptions: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    steps: list[TutorialStep] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    common_errors: list[TutorialError] = Field(default_factory=list)
    rollback: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Domain schemas
# ---------------------------------------------------------------------------


class UserProfile(BaseModel):
    os: OS = "unknown"
    skill_level: SkillLevel = "beginner"
    has_docker: bool = False
    has_gpu: bool = False
    api_key_status: str = "none"
    preferred_language: str = "zh"


class GitHubQuery(BaseModel):
    """A single query we send to the GitHub search API."""

    q: str
    sort: Literal["best-match", "stars", "updated"] = "best-match"
    why: str = Field(..., description="Human-readable reason this query exists.")


class RepoMeta(BaseModel):
    full_name: str
    url: str
    description: str = ""
    stars: int = 0
    forks: int = 0
    license: Optional[str] = None
    topics: list[str] = Field(default_factory=list)
    language: Optional[str] = None
    pushed_at: Optional[datetime] = None
    archived: bool = False
    found_via_query: Optional[str] = None  # which GitHubQuery surfaced it


class InstallSignals(BaseModel):
    """Heuristic flags extracted by `app.profiler` from the snapshot."""

    has_dockerfile: bool = False
    has_compose: bool = False
    has_one_click_script: bool = False
    package_managers: list[str] = Field(default_factory=list)  # pip/npm/poetry/...
    needs_gpu: bool = False
    needs_api_key: bool = False
    needs_database: bool = False
    has_screenshots: bool = False
    has_demo: bool = False
    documented_env_vars: bool = False
    readme_length: int = 0
    detected_languages: list[str] = Field(default_factory=list)


class RepoSnapshot(BaseModel):
    meta: RepoMeta
    readme: str = ""
    tree: list[str] = Field(default_factory=list)
    install_signals: InstallSignals = Field(default_factory=InstallSignals)
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class RuleScoreBreakdown(BaseModel):
    """One row per scoring dimension; `evidence` is the rule that fired."""

    name: str
    weight: float
    score: float
    evidence: str


class RuleScore(BaseModel):
    fit: float = 0.0
    beginner: float = 0.0
    deployability: float = 0.0
    activity: float = 0.0
    documentation: float = 0.0
    deployment_level: DeploymentLevel = "S3"
    total: float = 0.0
    hard_filter_failed: bool = False
    hard_filter_reason: str = ""
    breakdown: list[RuleScoreBreakdown] = Field(default_factory=list)


class Recommendation(BaseModel):
    """The unit of output for the ranking endpoint."""

    repo: RepoMeta
    rule_score: RuleScore
    rerank: Optional[RepoAnalysisResult] = None
    final_rank: int = 0
    final_score: float = 0.0
    rationale: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API request / response wrappers (one per endpoint in spec §9)
# ---------------------------------------------------------------------------


class ParseRequest(BaseModel):
    raw_text: str
    user_profile: Optional[UserProfile] = None


class ParseResponse(BaseModel):
    query_id: int
    parsed: RequirementParseResult
    queries: list[GitHubQuery]


class SearchRequest(BaseModel):
    raw_text: Optional[str] = None
    query_id: Optional[int] = None
    user_profile: Optional[UserProfile] = None


class SearchResponse(BaseModel):
    query_id: int
    parsed: RequirementParseResult
    queries: list[GitHubQuery]
    candidates: list[RepoMeta]


class RankRequest(BaseModel):
    query_id: int
    top_n: int = 5
    user_profile: Optional[UserProfile] = None


class RankResponse(BaseModel):
    query_id: int
    parsed: RequirementParseResult
    recommendations: list[Recommendation]


class TutorialRequest(BaseModel):
    full_name: str
    user_profile: UserProfile
    query_id: Optional[int] = None


class TutorialResponse(BaseModel):
    plan: TutorialPlan
    snapshot_fetched_at: datetime


class FeedbackRequest(BaseModel):
    user_id: Optional[int] = None
    full_name: str
    action: FeedbackAction
    success_status: Optional[bool] = None
    note: str = ""


class FeedbackResponse(BaseModel):
    ok: bool = True


class RepoProfileResponse(BaseModel):
    snapshot: RepoSnapshot
    cached: bool


# ---------------------------------------------------------------------------
# API credentials (managed in-app rather than via .env)
# ---------------------------------------------------------------------------


CredentialProvider = Literal["deepseek", "openai", "anthropic"]


class CredentialPreview(BaseModel):
    """Safe representation: never includes the raw api_key."""

    id: int
    provider: CredentialProvider
    label: str
    model: str = ""
    base_url: str = ""
    key_preview: str  # e.g. "***c9q8"
    is_active: bool = False
    created_at: datetime


class CredentialUpsertRequest(BaseModel):
    provider: CredentialProvider
    label: str
    api_key: str
    model: str = ""
    base_url: str = ""


# ---------------------------------------------------------------------------
# Favorites / history detail
# ---------------------------------------------------------------------------


class FavoriteEntry(BaseModel):
    full_name: str
    repo: Optional[RepoMeta] = None
    last_rationale: str = ""
    favorited_at: datetime


class TutorialPointer(BaseModel):
    full_name: str
    generated_at: datetime


class QueryDetailResponse(BaseModel):
    query_id: int
    raw_text: str
    parsed: RequirementParseResult
    recommendations: list[Recommendation]
    tutorials: list[TutorialPointer] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Replay envelope used by `app.llm`
# ---------------------------------------------------------------------------


class LLMCallRecord(BaseModel):
    """What gets persisted to the `llm_calls` table for replay."""

    id: Optional[int] = None
    kind: Literal["parse_requirement", "rerank_repos", "generate_tutorial"]
    provider: str
    model: str
    prompt: dict[str, Any]
    response: dict[str, Any]
    latency_ms: int
    created_at: datetime = Field(default_factory=datetime.utcnow)
