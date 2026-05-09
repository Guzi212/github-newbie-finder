"""LLM reranker that merges rule scores with semantic judgement.

Steps:
1. Score every snapshot with the rule scorer.
2. Drop hard-filter failures from the rerank set (kept on the side).
3. Send the top ~12 by rule score to the LLM in *one* call, getting a
   `RerankBatchResult` with one `RepoAnalysisResult` per repo.
4. Merge: `final_score = 0.6 * rule_total + 0.4 * llm_fit*100`. Sort by
   final_score, attach evidence excerpts, fill rationale.

Each `Recommendation` carries:
- the rule score (with breakdown — the regret-free baseline)
- the LLM analysis (semantic judgement)
- evidence excerpts pulled from the README around mentioned anchors
- a rationale string that combines the two views

This is the core "rule + LLM" split called out in spec §6 step 6 + §12.
"""

from __future__ import annotations

import logging
from typing import Iterable

from . import profiler
from .config import get_settings
from .llm import call_structured
from .schemas import (
    Evidence,
    Recommendation,
    RepoAnalysisResult,
    RepoSnapshot,
    RerankBatchResult,
    RequirementParseResult,
    RuleScore,
    UserProfile,
)
from .scorer import score as rule_score

log = logging.getLogger(__name__)


def _build_rerank_payload(
    parsed: RequirementParseResult,
    scored: list[tuple[RepoSnapshot, RuleScore]],
    *,
    lang: str = "zh",
) -> dict:
    candidates = []
    for snap, rs in scored:
        signals = snap.install_signals.model_dump()
        candidates.append({
            "full_name": snap.meta.full_name,
            "url": snap.meta.url,
            "description": snap.meta.description,
            "topics": snap.meta.topics,
            "stars": snap.meta.stars,
            "license": snap.meta.license,
            "language": snap.meta.language,
            "install_signals": signals,
            "rule_total": rs.total,
            "deployment_level_rule": rs.deployment_level,
            # Trimmed: smaller batch keeps DeepSeek under its slow-path budget.
            "readme_excerpt": (snap.readme or "")[:900],
        })
    return {
        "preferred_language": lang,
        "parsed_requirement": parsed.model_dump(),
        "candidates": candidates,
    }


def _evidence_for(snap: RepoSnapshot, analysis: RepoAnalysisResult) -> list[Evidence]:
    """Always return ≥1 evidence item per recommendation."""
    out: list[Evidence] = list(analysis.evidence or [])
    if out:
        return out

    # Build a synthetic evidence excerpt from the README around the first
    # mentioned anchor: a "pro" phrase, then must-have, then plain prefix.
    anchors = [p[:30] for p in analysis.pros[:1]]
    if anchors:
        snippet = profiler.excerpt(snap.readme, anchors[0], span=240)
    else:
        snippet = (snap.readme or snap.meta.description or "").strip()[:240]
    if snippet:
        out.append(Evidence(
            kind="readme",
            excerpt=snippet,
            source_url=snap.meta.url,
        ))
    if not out:
        out.append(Evidence(
            kind="metadata",
            excerpt=f"{snap.meta.description or snap.meta.full_name} · ★ {snap.meta.stars}",
            source_url=snap.meta.url,
        ))
    return out


_RAT_I18N = {
    "purpose":     {"zh": "📌 项目用途：",   "en": "📌 What it does: "},
    "summary":     {
        "zh": "🧮 综合判断：规则分 {total:.0f}/100（部署难度 {level}），最强维度是「{name}」（{ev}）。",
        "en": "🧮 Summary: rule score {total:.0f}/100 (deploy level {level}); strongest dimension is \"{name}\" ({ev}).",
    },
    "model_eval":  {
        "zh": "🤖 模型评估：与需求匹配度 {fit:.2f}，新手友好度 {beg:.2f}。",
        "en": "🤖 Model judgement: fit_score {fit:.2f}, beginner_score {beg:.2f}.",
    },
    "pros":        {"zh": "✅ 推荐理由：",   "en": "✅ Pros: "},
    "cons":        {"zh": "⚠️ 需要权衡：",   "en": "⚠️ Cons: "},
    "risks":       {"zh": "🚩 风险提示：",   "en": "🚩 Risks: "},
    "diffs":       {"zh": "✨ 差异化：",     "en": "✨ Differentiators: "},
    "missing":     {"zh": "❓ 资料缺口：",   "en": "❓ Missing info: "},
    "penalty":     {
        "zh": "⚖️ 评分修正：模型判断匹配度偏低，最终分已下调。",
        "en": "⚖️ Score adjusted: LLM fit-score is low, final score down-weighted.",
    },
    "joiner":      {"zh": "；", "en": "; "},
    "terminator":  {"zh": "。", "en": "."},
    "rerank_warn": {
        "zh": "ℹ️ 注意：LLM 重排本次未成功，仅使用规则评分（原因：{err}）。建议稍后重试以获得更高质量的语义匹配。",
        "en": "ℹ️ Heads-up: LLM rerank failed this run; only rule score was used (reason: {err}). Re-run later for higher-quality semantic matching.",
    },
}


def _t(key: str, lang: str, **fmt) -> str:
    val = _RAT_I18N[key].get(lang) or _RAT_I18N[key]["zh"]
    return val.format(**fmt) if fmt else val


def _rationale(rs: RuleScore, analysis: RepoAnalysisResult | None,
               *, lang: str = "zh", penalty_applied: bool = False) -> str:
    """Compose a multi-line, evidence-rich rationale for the recommendation card."""
    strongest = max(rs.breakdown, key=lambda b: b.score * b.weight)
    parts: list[str] = []
    joiner = _t("joiner", lang)
    term = _t("terminator", lang)

    if analysis and analysis.summary:
        parts.append(_t("purpose", lang) + analysis.summary.strip())

    parts.append(_t("summary", lang,
                    total=rs.total, level=rs.deployment_level,
                    name=strongest.name, ev=strongest.evidence))

    if analysis:
        parts.append(_t("model_eval", lang,
                        fit=analysis.fit_score, beg=analysis.beginner_score))
        if analysis.pros:
            parts.append(_t("pros", lang) + joiner.join(analysis.pros[:3]) + term)
        if analysis.cons:
            parts.append(_t("cons", lang) + joiner.join(analysis.cons[:2]) + term)
        if analysis.risk_flags:
            parts.append(_t("risks", lang) + joiner.join(analysis.risk_flags[:2]) + term)
        if analysis.differentiators:
            parts.append(_t("diffs", lang) + joiner.join(analysis.differentiators[:2]) + term)
        if analysis.missing_info:
            parts.append(_t("missing", lang) + joiner.join(analysis.missing_info[:2]) + term)

    if penalty_applied:
        parts.append(_t("penalty", lang))

    return "\n".join(parts)


def rank(
    parsed: RequirementParseResult,
    snapshots: Iterable[RepoSnapshot],
    *,
    top_n: int | None = None,
    user_profile: UserProfile | None = None,
) -> list[Recommendation]:
    s = get_settings()
    top_n = top_n or s.top_n
    lang = (user_profile.preferred_language if user_profile else "zh") or "zh"
    if lang not in ("zh", "en"):
        lang = "zh"

    snapshots = list(snapshots)
    if not snapshots:
        return []

    # Step 1: rule score every snapshot.
    scored: list[tuple[RepoSnapshot, RuleScore]] = []
    blocked: list[tuple[RepoSnapshot, RuleScore]] = []
    for snap in snapshots:
        rs = rule_score(parsed, snap)
        if rs.hard_filter_failed:
            blocked.append((snap, rs))
        else:
            scored.append((snap, rs))

    scored.sort(key=lambda x: x[1].total, reverse=True)

    # Step 2: rerank the top ~8 with the LLM in one call. Smaller batch keeps
    # DeepSeek under its slow-path timeout budget.
    rerank_window = scored[:max(top_n + 3, 7)]
    analyses_by_name: dict[str, RepoAnalysisResult] = {}
    rerank_failed = False
    rerank_error = ""
    if rerank_window:
        try:
            payload = _build_rerank_payload(parsed, rerank_window, lang=lang)
            batch = call_structured("rerank_repos", payload, RerankBatchResult)
            analyses_by_name = {a.full_name: a for a in batch.analyses}
        except Exception as e:
            rerank_failed = True
            rerank_error = str(e)
            log.warning("LLM rerank failed; falling back to rule order: %s", e)

    # Step 3: merge into final ranking.
    # Hard penalty for "wrong product form" matches: the LLM is instructed to
    # return fit_score<=0.30 in that case; we additionally apply a *score*
    # multiplier on the merged final_score so a popular but-wrong-form repo
    # can't out-rank a smaller, well-fitting one purely on rule points.
    recs: list[Recommendation] = []
    for snap, rs in scored:
        analysis = analyses_by_name.get(snap.meta.full_name)
        if analysis:
            llm_part = analysis.fit_score * 100
            # Boosted LLM weight (0.55) so semantic judgement dominates more.
            base = 0.45 * rs.total + 0.55 * llm_part
            penalty_applied = analysis.fit_score < 0.35
            if penalty_applied:
                base *= 0.55       # severe down-rank for form-mismatch
            elif analysis.fit_score < 0.55:
                base *= 0.85       # mild down-rank for "kind of fits"
        else:
            base = rs.total
            penalty_applied = False
        final = round(base, 2)
        recs.append(Recommendation(
            repo=snap.meta,
            rule_score=rs,
            rerank=analysis,
            final_rank=0,            # filled below
            final_score=final,
            rationale=_rationale(rs, analysis, lang=lang, penalty_applied=penalty_applied),
            evidence=_evidence_for(snap, analysis) if analysis else [
                Evidence(
                    kind="metadata",
                    excerpt=(snap.meta.description or snap.meta.full_name)[:240],
                    source_url=snap.meta.url,
                )
            ],
        ))

    recs.sort(key=lambda r: r.final_score, reverse=True)
    recs = recs[:top_n]
    for i, r in enumerate(recs, start=1):
        r.final_rank = i

    if rerank_failed and recs:
        # Surface the fallback in the top result's rationale so the UI shows it.
        warn = _t("rerank_warn", lang, err=rerank_error[:140])
        recs[0].rationale = warn + "\n\n" + recs[0].rationale

    return recs
