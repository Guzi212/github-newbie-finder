"""Deterministic GitHub query planner.

Given a `RequirementParseResult`, produce 6-10 GitHub search queries that
together provide good recall. Queries are deterministic so they're easy to
inspect, tweak, and replay.

Each query records `why` so the UI / logs can explain the recall set.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .schemas import GitHubQuery, RequirementParseResult


_GENERIC_QUALITY_FILTER = "stars:>=20 archived:false"


def _slug(term: str) -> str:
    """Make a topic-friendly slug, lowercase, hyphenated."""
    s = term.strip().lower()
    s = re.sub(r"[^a-z0-9一-鿿]+", "-", s)
    return s.strip("-")


def _quality_window() -> str:
    """Repos pushed in the last ~18 months — keeps active projects."""
    cutoff = (date.today() - timedelta(days=540)).isoformat()
    return f"pushed:>{cutoff}"


def plan_queries(parsed: RequirementParseResult) -> list[GitHubQuery]:
    """Spec §6 step 2: query expansion across synonyms / topics / README."""

    queries: list[GitHubQuery] = []
    seen: set[str] = set()

    def push(q: str, why: str, sort: str = "best-match") -> None:
        norm = q.strip()
        if norm and norm not in seen:
            seen.add(norm)
            queries.append(GitHubQuery(q=norm, sort=sort, why=why))  # type: ignore[arg-type]

    keywords = (parsed.keywords_en + parsed.keywords_zh)[:5]
    top_kw = parsed.keywords_en[0] if parsed.keywords_en else (keywords[0] if keywords else "")
    primary_kw = " ".join(parsed.keywords_en[:3]) or " ".join(keywords[:3])

    # 0. A single high-recall query on the top keyword alone, star-sorted.
    # Without this, multi-word literal queries miss the well-known repos that
    # most users actually mean (e.g. "RAG knowledge base" misses Dify/AnythingLLM).
    if top_kw:
        push(
            f"{top_kw} stars:>=100 archived:false",
            f"Broad recall on the single top keyword `{top_kw}` — surfaces popular repos.",
            sort="stars",
        )

    # 1. Literal English keywords with quality filter.
    if primary_kw:
        push(
            f"{primary_kw} {_GENERIC_QUALITY_FILTER} {_quality_window()}",
            "Primary English keywords with quality filter (active, ≥20 stars, not archived).",
            sort="best-match",
        )
        push(
            f"{primary_kw} sort:stars-desc archived:false",
            "Star-sorted variant for the primary English keywords.",
            sort="stars",
        )

    # 2. Top synonym/keyword as a topic search (often very precise).
    for kw in parsed.keywords_en[:3]:
        slug = _slug(kw)
        if slug and len(slug) >= 3:
            push(
                f"topic:{slug} {_GENERIC_QUALITY_FILTER}",
                f"Repos tagged with the topic `{slug}` — high precision.",
            )

    # 3. README-targeted search for must-have terms.
    for must in parsed.must_have[:2]:
        push(
            f'"{must}" in:readme {_GENERIC_QUALITY_FILTER}',
            f"README mentions must-have requirement: {must!r}.",
        )

    # 4. Deployment-flavored variants.
    if parsed.deployment == "docker" and primary_kw:
        push(
            f"{primary_kw} docker compose archived:false",
            "Filter to Docker-Compose-friendly projects.",
        )
    if parsed.deployment == "local" and primary_kw:
        push(
            f"{primary_kw} self-hosted archived:false",
            "Filter to self-hosted / local-first projects.",
        )
        push(
            f"{primary_kw} \"one-click\" in:readme",
            "Look for projects advertising one-click install.",
        )

    # 5. Beginner-friendly cue.
    if "beginner" in " ".join(parsed.must_have + parsed.nice_to_have).lower() \
       or parsed.skill_level == "beginner":
        if primary_kw:
            push(
                f"{primary_kw} \"easy to deploy\" in:readme",
                "Project README explicitly mentions easy deployment.",
            )

    # 6. Tech-stack-specific narrowing.
    for stack in parsed.tech_stack[:2]:
        slug = _slug(stack)
        if slug and primary_kw:
            push(
                f"{primary_kw} language:{slug} archived:false",
                f"Restrict to {stack} projects.",
            )

    # 7. Avoid-term filter (can't truly negate via search; record as docs).
    # The scorer enforces avoid terms as soft penalties.

    # Hard cap: 10 queries.
    return queries[:10]
