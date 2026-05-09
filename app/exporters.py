"""Markdown exporters for recommendations and tutorials.

Pure functions that take plain dicts (the JSON shape returned by the API)
and return Markdown text suitable for dropping into an Obsidian vault.

Output includes a YAML frontmatter block so Obsidian can parse `tags`,
`source`, etc. without further configuration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _yaml_escape(value: str) -> str:
    """Escape a scalar string for safe inclusion in YAML.

    We're intentionally conservative: anything with a colon, leading
    indicator char, or newline gets quoted.
    """
    if value is None:
        return ""
    s = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in s:
        # Use a folded scalar — preserve newlines via block literal.
        indented = "\n  ".join(s.split("\n"))
        return f"|\n  {indented}"
    needs_quote = any(c in s for c in ":#[]{},&*!|>'\"`%@") or s != s.strip()
    if needs_quote or s == "":
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _yaml_list(items: Iterable[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return "[]"
    parts = [_yaml_escape(i) for i in items]
    return "[" + ", ".join(parts) + "]"


def _frontmatter(fields: dict[str, Any]) -> str:
    out = ["---"]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        if isinstance(v, list):
            out.append(f"{k}: {_yaml_list(v)}")
        else:
            out.append(f"{k}: {_yaml_escape(v)}")
    out.append("---")
    return "\n".join(out)


def _slug(text: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in text.strip())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-").lower()[:80] or "note"


# ---------------------------------------------------------------------------
# i18n strings (zh / en)
# ---------------------------------------------------------------------------


_STRINGS: dict[str, dict[str, str]] = {
    # Recommendations
    "recs.title": {"zh": "🧭 推荐结果", "en": "🧭 Recommendations"},
    "recs.fm_title": {"zh": "GitHub 推荐 · {intent}", "en": "GitHub Recommendations · {intent}"},
    "recs.requirement": {"zh": "**需求**：{intent}", "en": "**Requirement**: {intent}"},
    "recs.deployment_pref": {"zh": "部署偏好", "en": "Deployment preference"},
    "recs.skill_level": {"zh": "技能水平", "en": "Skill level"},
    "recs.keywords": {"zh": "关键词", "en": "Keywords"},
    "recs.purpose": {"zh": "**📌 项目用途**", "en": "**📌 What it does**"},
    "recs.score_breakdown": {"zh": "评分明细", "en": "Score breakdown"},
    "recs.score_table_head": {
        "zh": "| 维度 | 权重 | 分值 | 依据 |\n| --- | ---: | ---: | --- |",
        "en": "| Dimension | Weight | Score | Evidence |\n| --- | ---: | ---: | --- |",
    },
    "recs.llm_analysis": {"zh": "LLM 重排分析", "en": "LLM rerank analysis"},
    "recs.pros": {"zh": "**优点**", "en": "**Pros**"},
    "recs.cons": {"zh": "**注意点**", "en": "**Cons**"},
    "recs.diffs": {"zh": "**差异化**", "en": "**Differentiators**"},
    "recs.risks": {"zh": "**风险**", "en": "**Risks**"},
    "recs.why": {"zh": "为什么推荐", "en": "Why recommended"},
    "recs.evidence": {"zh": "Evidence", "en": "Evidence"},
    "recs.evidence_source": {"zh": "来源", "en": "source"},
    "recs.tags": {"zh": ["github-finder", "recommendation"], "en": ["github-finder", "recommendation"]},
    "recs.difficulty": {"zh": "难度", "en": "difficulty"},
    "recs.composite": {"zh": "综合分", "en": "composite"},

    # Tutorial
    "tut.fm_title": {"zh": "部署教程 · {repo}", "en": "Deployment Tutorial · {repo}"},
    "tut.h1": {"zh": "📘 {repo} 部署教程", "en": "📘 {repo} Deployment Tutorial"},
    "tut.repo_url_label": {"zh": "项目地址", "en": "Repository"},
    "tut.assumptions": {"zh": "假设环境", "en": "Assumed environment"},
    "tut.prereqs": {"zh": "准备工作", "en": "Prerequisites"},
    "tut.steps": {"zh": "部署步骤", "en": "Deployment steps"},
    "tut.step_label": {"zh": "步骤 {i}：{title}", "en": "Step {i}: {title}"},
    "tut.needs_verification": {"zh": " ⚠️ 需要核对", "en": " ⚠️ needs verification"},
    "tut.verification": {"zh": "跑起来后怎么验证", "en": "How to verify"},
    "tut.common_errors": {"zh": "常见报错", "en": "Common errors"},
    "tut.cause": {"zh": "**原因**", "en": "**Cause**"},
    "tut.fix": {"zh": "**修复**", "en": "**Fix**"},
    "tut.rollback": {"zh": "回滚", "en": "Rollback"},
    "tut.next_steps": {"zh": "下一步", "en": "Next steps"},
}


def _t(key: str, lang: str, **fmt: Any) -> Any:
    """Translate a string key. Falls back to zh on unknown lang."""
    entry = _STRINGS.get(key, {})
    val = entry.get(lang) or entry.get("zh") or key
    if isinstance(val, str) and fmt:
        return val.format(**fmt)
    return val


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def render_recommendations_md(
    ranked: dict[str, Any],
    *,
    raw_text: str = "",
    lang: str = "zh",
) -> str:
    """Render the full Top-N recommendations as a single Markdown document."""
    lang = lang if lang in ("zh", "en") else "zh"
    parsed = ranked.get("parsed") or {}
    recs = ranked.get("recommendations") or []
    intent = parsed.get("intent") or raw_text or "GitHub recommendations"

    fm = _frontmatter({
        "title": _t("recs.fm_title", lang, intent=intent[:80]),
        "type": "recommendation",
        "query_id": ranked.get("query_id"),
        "query": raw_text,
        "lang": lang,
        "tags": _t("recs.tags", lang),
        "generated_at": _now_iso(),
    })

    body = [fm, "", f"# {_t('recs.title', lang)}", ""]
    if intent:
        body.append(f"> {_t('recs.requirement', lang, intent=intent)}")
        body.append("")
    if parsed:
        keywords = ", ".join(parsed.get("keywords_en") or []) or "—"
        body.append(f"- {_t('recs.deployment_pref', lang)}: `{parsed.get('deployment','any')}`")
        body.append(f"- {_t('recs.skill_level', lang)}: `{parsed.get('skill_level','beginner')}`")
        body.append(f"- {_t('recs.keywords', lang)}: {keywords}")
        body.append("")

    for r in recs:
        body.extend(_render_one_rec(r, lang=lang))
        body.append("")
        body.append("---")
        body.append("")

    return "\n".join(body).rstrip() + "\n"


def _render_one_rec(rec: dict[str, Any], *, lang: str = "zh") -> list[str]:
    repo = rec.get("repo") or {}
    rs = rec.get("rule_score") or {}
    rerank = rec.get("rerank") or None
    full_name = repo.get("full_name", "")
    url = repo.get("url", "")
    summary = (rerank or {}).get("summary") if rerank else ""
    summary = summary or repo.get("description", "")

    lines = [
        f"## #{rec.get('final_rank','?')} · [{full_name}]({url})",
        "",
    ]
    if summary:
        lines.append(f"{_t('recs.purpose', lang)}: {summary}")
        lines.append("")

    meta_row = [
        f"⭐ {repo.get('stars', 0)}",
        f"🍴 {repo.get('forks', 0)}",
        f"License `{repo.get('license') or '—'}`",
        f"Lang `{repo.get('language') or '—'}`",
        f"{_t('recs.difficulty', lang)} `{rs.get('deployment_level','—')}`",
        f"{_t('recs.composite', lang)} **{rec.get('final_score',0):.1f}**",
    ]
    lines.append(" · ".join(meta_row))
    lines.append("")

    breakdown = rs.get("breakdown") or []
    if breakdown:
        lines.append(f"### {_t('recs.score_breakdown', lang)}")
        lines.append("")
        lines.append(_t("recs.score_table_head", lang))
        for b in breakdown:
            ev = (b.get("evidence") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {b.get('name','')} | {int(b.get('weight',0)*100)}% | "
                f"{int(b.get('score',0)*100)}/100 | {ev} |"
            )
        lines.append("")

    if rerank:
        pros = rerank.get("pros") or []
        cons = rerank.get("cons") or []
        diffs = rerank.get("differentiators") or []
        risks = rerank.get("risk_flags") or []
        if pros or cons or diffs or risks:
            lines.append(f"### {_t('recs.llm_analysis', lang)}")
            lines.append("")
        if pros:
            lines.append(_t("recs.pros", lang))
            lines.extend(f"- {p}" for p in pros)
            lines.append("")
        if cons:
            lines.append(_t("recs.cons", lang))
            lines.extend(f"- {c}" for c in cons)
            lines.append("")
        if diffs:
            lines.append(_t("recs.diffs", lang))
            lines.extend(f"- {d}" for d in diffs)
            lines.append("")
        if risks:
            lines.append(_t("recs.risks", lang))
            lines.extend(f"- ⚠️ {r}" for r in risks)
            lines.append("")

    rationale = (rec.get("rationale") or "").strip()
    if rationale:
        lines.append(f"### {_t('recs.why', lang)}")
        lines.append("")
        lines.append(rationale)
        lines.append("")

    evidence = rec.get("evidence") or []
    if evidence:
        lines.append(f"### {_t('recs.evidence', lang)}")
        lines.append("")
        src_label = _t("recs.evidence_source", lang)
        for ev in evidence:
            src = ev.get("source_url") or ""
            kind = ev.get("kind") or ""
            head = f"_{kind}_" + (f" · [{src_label}]({src})" if src else "")
            lines.append(head)
            excerpt = (ev.get("excerpt") or "")[:600]
            lines.append("```")
            lines.append(excerpt)
            lines.append("```")
            lines.append("")

    return lines


def recommendations_filename(ranked: dict[str, Any]) -> str:
    qid = ranked.get("query_id", "x")
    intent = (ranked.get("parsed") or {}).get("intent") or "recommendations"
    return f"github-finder-recs-{qid}-{_slug(intent)}.md"


# ---------------------------------------------------------------------------
# Tutorial
# ---------------------------------------------------------------------------


def render_tutorial_md(
    tutorial: dict[str, Any],
    *,
    repo_url: str = "",
    lang: str = "zh",
) -> str:
    """Render a single tutorial plan as a standalone Markdown note."""
    lang = lang if lang in ("zh", "en") else "zh"
    plan = tutorial.get("plan") or tutorial  # tolerate either wrapper
    full_name = plan.get("repo_full_name", "")
    if not repo_url and full_name:
        repo_url = f"https://github.com/{full_name}"

    fm = _frontmatter({
        "title": _t("tut.fm_title", lang, repo=full_name),
        "type": "tutorial",
        "repo": full_name,
        "source": repo_url,
        "lang": lang,
        "tags": ["github-finder", "tutorial"],
        "generated_at": _now_iso(),
    })

    lines = [fm, "", f"# {_t('tut.h1', lang, repo=full_name)}", ""]
    if repo_url:
        lines.append(f"{_t('tut.repo_url_label', lang)}: {repo_url}")
        lines.append("")

    assumptions = plan.get("assumptions") or []
    if assumptions:
        lines.append(f"## {_t('tut.assumptions', lang)}")
        lines.append("")
        lines.extend(f"- {a}" for a in assumptions)
        lines.append("")

    prereqs = plan.get("prerequisites") or []
    if prereqs:
        lines.append(f"## {_t('tut.prereqs', lang)}")
        lines.append("")
        lines.extend(f"- {p}" for p in prereqs)
        lines.append("")

    steps = plan.get("steps") or []
    if steps:
        lines.append(f"## {_t('tut.steps', lang)}")
        lines.append("")
        for i, step in enumerate(steps, 1):
            tag = _t("tut.needs_verification", lang) if step.get("needs_verification") else ""
            lines.append(f"### {_t('tut.step_label', lang, i=i, title=step.get('title',''))}{tag}")
            if step.get("explanation"):
                lines.append("")
                lines.append(step["explanation"])
            cmds = step.get("commands") or []
            if cmds:
                lines.append("")
                lines.append("```bash")
                lines.extend(cmds)
                lines.append("```")
            lines.append("")

    verification = plan.get("verification") or []
    if verification:
        lines.append(f"## {_t('tut.verification', lang)}")
        lines.append("")
        lines.extend(f"- {v}" for v in verification)
        lines.append("")

    common_errors = plan.get("common_errors") or []
    if common_errors:
        lines.append(f"## {_t('tut.common_errors', lang)}")
        lines.append("")
        cause_label = _t("tut.cause", lang)
        fix_label = _t("tut.fix", lang)
        for e in common_errors:
            lines.append(f"### {e.get('symptom','')}")
            if e.get("cause"):
                lines.append(f"- {cause_label}: {e['cause']}")
            if e.get("fix"):
                lines.append(f"- {fix_label}: {e['fix']}")
            lines.append("")

    rollback = plan.get("rollback") or []
    if rollback:
        lines.append(f"## {_t('tut.rollback', lang)}")
        lines.append("")
        lines.extend(f"- {r}" for r in rollback)
        lines.append("")

    next_steps = plan.get("next_steps") or []
    if next_steps:
        lines.append(f"## {_t('tut.next_steps', lang)}")
        lines.append("")
        lines.extend(f"- {n}" for n in next_steps)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def tutorial_filename(tutorial: dict[str, Any]) -> str:
    plan = tutorial.get("plan") or tutorial
    full = plan.get("repo_full_name", "tutorial").replace("/", "_")
    return f"github-finder-tutorial-{_slug(full)}.md"
