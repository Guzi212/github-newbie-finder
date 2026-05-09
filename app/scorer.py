"""Rule-based scorer (spec §7).

Five weighted dimensions: 35/25/20/10/10.
Returns a `RuleScore` whose `breakdown` field documents which rule fired for
which dimension — that's what gets shown in the UI as "scoring rationale".

Also classifies repos into deployment difficulty S1-S5 (spec §7.1).

Hard filters (spec §13 scenario C): if the user said `deployment="local"`
and the repo *only* documents Docker, we mark `hard_filter_failed=True`. The
ranker then drops it out of the Top-N (but keeps it in the candidate pool
for transparency).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .schemas import (
    DeploymentLevel,
    RepoSnapshot,
    RequirementParseResult,
    RuleScore,
    RuleScoreBreakdown,
)


WEIGHTS = {
    "fit": 0.35,
    "beginner": 0.25,
    "deployability": 0.20,
    "activity": 0.10,
    "documentation": 0.10,
}


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


# ---------------------------------------------------------------------------
# Deployment difficulty classification (spec §7.1)
# ---------------------------------------------------------------------------


def classify_deployment_level(snapshot: RepoSnapshot) -> DeploymentLevel:
    s = snapshot.install_signals
    readme = snapshot.readme or ""

    # S5: not maintained / clearly not for beginners
    if snapshot.meta.archived:
        return "S5"
    if not readme.strip():
        return "S5"
    pushed = snapshot.meta.pushed_at
    if pushed:
        # `pushed_at` parsed via fromisoformat keeps UTC tzinfo.
        cutoff = datetime.now(timezone.utc) - timedelta(days=900)
        if pushed.tzinfo is None:
            pushed = pushed.replace(tzinfo=timezone.utc)
        if pushed < cutoff:
            return "S5"

    if s.needs_gpu:
        return "S4"

    needs_many = sum([s.needs_database, s.needs_api_key]) >= 2
    if needs_many or (s.needs_database and len(s.package_managers) >= 2):
        return "S3"

    if s.has_compose or s.has_one_click_script:
        if s.has_screenshots and not s.needs_api_key:
            return "S1"
        return "S2"

    if s.has_dockerfile or len(s.package_managers) >= 1:
        return "S2"

    return "S3"


# ---------------------------------------------------------------------------
# Per-dimension scoring
# ---------------------------------------------------------------------------


# Words that strongly suggest the repo is a developer tool / plugin / SDK
# rather than a deployable end-user product. If the user did NOT ask for one
# of these forms, we penalise fit so a popular SDK can't beat a smaller but
# correctly-shaped product. Acts as a fallback when the LLM reranker fails.
_FORM_MISMATCH_TOKENS = (
    "plugin", "extension", "sdk", "library", "client",
    "wrapper", "binding", "boilerplate", "starter kit", "template",
    "cli tool", "command-line", "addon", "add-on",
)


def _form_mismatch_penalty(parsed: RequirementParseResult, snap: RepoSnapshot) -> tuple[float, str]:
    """Return a multiplier in [0.4, 1.0] for fit, plus an explanation.

    1.0 = no penalty. <1.0 = repo looks like a developer artefact while the
    user asked for a deployable product.
    """
    user_text = " ".join([
        " ".join(parsed.must_have),
        " ".join(parsed.nice_to_have),
        " ".join(parsed.keywords_en + parsed.keywords_zh),
    ]).lower()
    user_wants_form = any(tok in user_text for tok in _FORM_MISMATCH_TOKENS)
    if user_wants_form:
        return 1.0, ""

    haystack = " ".join([
        snap.meta.description or "",
        " ".join(snap.meta.topics or []),
        snap.readme[:600] if snap.readme else "",
    ]).lower()
    matched = [tok for tok in _FORM_MISMATCH_TOKENS if tok in haystack]
    if not matched:
        return 1.0, ""
    return 0.45, f"form mismatch (matches: {', '.join(matched[:3])})"


def _fit_score(parsed: RequirementParseResult, snap: RepoSnapshot) -> tuple[float, str]:
    haystack_parts = [
        snap.meta.description or "",
        " ".join(snap.meta.topics or []),
        snap.readme[:3000] if snap.readme else "",
    ]
    hay = " ".join(haystack_parts).lower()

    must = [m.lower() for m in parsed.must_have]
    nice = [m.lower() for m in parsed.nice_to_have]
    keywords = [k.lower() for k in (parsed.keywords_en + parsed.keywords_zh)]

    must_hits = sum(1 for m in must if m and m in hay)
    nice_hits = sum(1 for m in nice if m and m in hay)
    kw_hits = sum(1 for k in keywords if k and k in hay)

    must_part = (must_hits / max(1, len(must))) if must else 0.5
    nice_part = (nice_hits / max(1, len(nice))) if nice else 0.0
    kw_part = min(1.0, kw_hits / max(3, len(keywords) or 3))

    raw = 0.55 * must_part + 0.20 * nice_part + 0.25 * kw_part
    explanation = (
        f"must_have hits {must_hits}/{max(1, len(must))}, "
        f"nice {nice_hits}/{max(1, len(nice))}, "
        f"keywords {kw_hits}/{max(1, len(keywords))}."
    )

    mismatch_mult, mismatch_note = _form_mismatch_penalty(parsed, snap)
    if mismatch_mult < 1.0:
        raw *= mismatch_mult
        explanation = f"{explanation} | penalty: {mismatch_note}"

    return _clip(raw), explanation


def _beginner_score(snap: RepoSnapshot) -> tuple[float, str]:
    s = snap.install_signals
    score = 0.0
    notes: list[str] = []
    if s.has_compose:
        score += 0.35
        notes.append("docker-compose")
    elif s.has_dockerfile:
        score += 0.20
        notes.append("Dockerfile")
    if s.has_one_click_script:
        score += 0.20
        notes.append("one-click script")
    if s.has_screenshots:
        score += 0.15
        notes.append("screenshots")
    if s.has_demo:
        score += 0.10
        notes.append("demo")
    if s.documented_env_vars:
        score += 0.10
        notes.append(".env.example/docs")
    if s.readme_length > 2000:
        score += 0.05
        notes.append("substantive README")
    if s.needs_gpu:
        score -= 0.20
        notes.append("(-) GPU required")
    if s.needs_api_key:
        score -= 0.05
        notes.append("(-) API key required")
    return _clip(score), ", ".join(notes) or "few beginner-friendly signals"


def _deployability_score(snap: RepoSnapshot, parsed: RequirementParseResult) -> tuple[float, str]:
    s = snap.install_signals
    score = 0.4  # base
    notes: list[str] = []
    if s.has_compose:
        score += 0.30
        notes.append("compose available")
    elif s.has_dockerfile:
        score += 0.15
        notes.append("Dockerfile")
    if s.package_managers:
        score += 0.10
        notes.append(f"package manager: {','.join(s.package_managers)}")
    if s.needs_gpu:
        score -= 0.30
        notes.append("(-) GPU")
    if s.needs_database:
        score -= 0.10
        notes.append("(-) external DB")
    if s.has_one_click_script:
        score += 0.10
        notes.append("one-click")
    # Deployment preference penalty
    if parsed.deployment == "local" and not s.has_one_click_script and not s.package_managers \
       and (s.has_dockerfile or s.has_compose):
        score -= 0.20
        notes.append("(-) only Docker, user wants local")
    return _clip(score), ", ".join(notes) or "baseline deployability"


def _activity_score(snap: RepoSnapshot) -> tuple[float, str]:
    pushed = snap.meta.pushed_at
    if not pushed:
        return 0.4, "no pushed_at"
    if pushed.tzinfo is None:
        pushed = pushed.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - pushed).days
    if days < 60:
        return 1.0, f"updated {days}d ago"
    if days < 180:
        return 0.85, f"updated {days}d ago"
    if days < 365:
        return 0.65, f"updated {days}d ago"
    if days < 730:
        return 0.4, f"updated {days}d ago"
    return 0.2, f"stale: {days}d since last push"


def _documentation_score(snap: RepoSnapshot) -> tuple[float, str]:
    rl = snap.install_signals.readme_length
    notes: list[str] = []
    if rl < 200:
        return 0.1, "README very short"
    score = 0.3
    notes.append(f"README {rl}ch")
    if rl > 1500:
        score += 0.25
    if rl > 4000:
        score += 0.15
    if snap.install_signals.has_screenshots:
        score += 0.15
        notes.append("screenshots")
    if snap.install_signals.documented_env_vars:
        score += 0.15
        notes.append("env vars documented")
    return _clip(score), ", ".join(notes)


# ---------------------------------------------------------------------------
# Hard filters (spec §13 scenario C)
# ---------------------------------------------------------------------------


def _hard_filter(parsed: RequirementParseResult, snap: RepoSnapshot) -> tuple[bool, str]:
    sig = snap.install_signals
    avoid = [a.lower() for a in parsed.avoid]

    if "docker" in " ".join(avoid) or any("docker" in a and "without" in a for a in avoid):
        if (sig.has_compose or sig.has_dockerfile) and not sig.package_managers:
            return True, "User asked to avoid Docker; repo only documents Docker."

    if parsed.deployment == "local":
        if sig.needs_gpu and "gpu" not in " ".join(parsed.nice_to_have).lower():
            return True, "User wants local deployment; repo requires GPU."

    if snap.meta.archived:
        return True, "Repo is archived."

    return False, ""


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def score(parsed: RequirementParseResult, snap: RepoSnapshot) -> RuleScore:
    fit, fit_why = _fit_score(parsed, snap)
    beg, beg_why = _beginner_score(snap)
    dep, dep_why = _deployability_score(snap, parsed)
    act, act_why = _activity_score(snap)
    doc, doc_why = _documentation_score(snap)
    level = classify_deployment_level(snap)
    blocked, reason = _hard_filter(parsed, snap)

    total = (
        fit * WEIGHTS["fit"]
        + beg * WEIGHTS["beginner"]
        + dep * WEIGHTS["deployability"]
        + act * WEIGHTS["activity"]
        + doc * WEIGHTS["documentation"]
    ) * 100

    return RuleScore(
        fit=fit,
        beginner=beg,
        deployability=dep,
        activity=act,
        documentation=doc,
        deployment_level=level,
        total=round(total, 2),
        hard_filter_failed=blocked,
        hard_filter_reason=reason,
        breakdown=[
            RuleScoreBreakdown(name="需求匹配度", weight=WEIGHTS["fit"], score=fit, evidence=fit_why),
            RuleScoreBreakdown(name="新手友好度", weight=WEIGHTS["beginner"], score=beg, evidence=beg_why),
            RuleScoreBreakdown(name="部署难度", weight=WEIGHTS["deployability"], score=dep, evidence=dep_why),
            RuleScoreBreakdown(name="项目活跃度", weight=WEIGHTS["activity"], score=act, evidence=act_why),
            RuleScoreBreakdown(name="文档质量", weight=WEIGHTS["documentation"], score=doc, evidence=doc_why),
        ],
    )
