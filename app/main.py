"""FastAPI application — six endpoints from spec §9.

Boundaries are kept thin: every endpoint is glue that calls one of the
domain modules and returns a Pydantic schema.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import db, github, llm, parser, query_planner, ranker, tutorial
from .config import get_settings
from .schemas import (
    CredentialPreview,
    CredentialUpsertRequest,
    FavoriteEntry,
    FeedbackRequest,
    FeedbackResponse,
    ParseRequest,
    ParseResponse,
    QueryDetailResponse,
    RankRequest,
    RankResponse,
    RepoProfileResponse,
    SearchRequest,
    SearchResponse,
    TutorialRequest,
    TutorialResponse,
)

log = logging.getLogger(__name__)

app = FastAPI(
    title="GitHub Project Finder MVP",
    version="0.1.0",
    description="Natural-language GitHub project discovery for beginners.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "llm_provider": settings.llm_provider}


# ---------------------------------------------------------------------------
# /api/requirements/parse
# ---------------------------------------------------------------------------


@app.post("/api/requirements/parse", response_model=ParseResponse)
def api_parse(req: ParseRequest) -> ParseResponse:
    parsed = parser.parse_requirement(req.raw_text, req.user_profile)
    queries = query_planner.plan_queries(parsed)
    query_id = db.save_query(raw_text=req.raw_text, parsed=parsed, queries=queries)
    return ParseResponse(query_id=query_id, parsed=parsed, queries=queries)


# ---------------------------------------------------------------------------
# /api/search
# ---------------------------------------------------------------------------


@app.post("/api/search", response_model=SearchResponse)
def api_search(req: SearchRequest) -> SearchResponse:
    if req.query_id is not None:
        record = db.get_query(req.query_id)
        if not record:
            raise HTTPException(404, f"query_id {req.query_id} not found")
        parsed = record["parsed"]
        queries = record["queries"]
        query_id = record["id"]
    elif req.raw_text:
        parsed = parser.parse_requirement(req.raw_text, req.user_profile)
        queries = query_planner.plan_queries(parsed)
        query_id = db.save_query(raw_text=req.raw_text, parsed=parsed, queries=queries)
    else:
        raise HTTPException(400, "Provide either raw_text or query_id")

    candidates = github.recall(queries)
    return SearchResponse(
        query_id=query_id,
        parsed=parsed,
        queries=queries,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# /api/recommendations/rank
# ---------------------------------------------------------------------------


@app.post("/api/recommendations/rank", response_model=RankResponse)
def api_rank(req: RankRequest) -> RankResponse:
    record = db.get_query(req.query_id)
    if not record:
        raise HTTPException(404, f"query_id {req.query_id} not found")
    parsed = record["parsed"]
    queries = record["queries"]

    candidates = github.recall(queries)
    if not candidates:
        return RankResponse(query_id=req.query_id, parsed=parsed, recommendations=[])

    snapshots = github.fetch_snapshots([c.full_name for c in candidates])
    recs = ranker.rank(parsed, snapshots, top_n=req.top_n)
    db.save_recommendations(req.query_id, recs)

    return RankResponse(
        query_id=req.query_id,
        parsed=parsed,
        recommendations=recs,
    )


# ---------------------------------------------------------------------------
# /api/repos/{owner}/{repo}/profile
# ---------------------------------------------------------------------------


@app.get("/api/repos/{owner}/{repo}/profile", response_model=RepoProfileResponse)
def api_profile(owner: str, repo: str) -> RepoProfileResponse:
    full_name = f"{owner}/{repo}"
    settings = get_settings()
    cached = db.get_fresh_snapshot(full_name, settings.snapshot_ttl_hours)
    if cached:
        return RepoProfileResponse(snapshot=cached, cached=True)
    try:
        snap = github.fetch_snapshot(full_name)
    except Exception as e:
        raise HTTPException(404, str(e))
    return RepoProfileResponse(snapshot=snap, cached=False)


# ---------------------------------------------------------------------------
# /api/tutorials/generate
# ---------------------------------------------------------------------------


@app.post("/api/tutorials/generate", response_model=TutorialResponse)
def api_tutorial(req: TutorialRequest) -> TutorialResponse:
    plan, snap = tutorial.generate_tutorial(
        req.full_name, req.user_profile, query_id=req.query_id
    )
    return TutorialResponse(plan=plan, snapshot_fetched_at=snap.fetched_at)


# ---------------------------------------------------------------------------
# /api/feedback
# ---------------------------------------------------------------------------


@app.post("/api/feedback", response_model=FeedbackResponse)
def api_feedback(req: FeedbackRequest) -> FeedbackResponse:
    db.save_feedback(
        full_name=req.full_name,
        action=req.action,
        user_id=req.user_id,
        success_status=req.success_status,
        note=req.note,
    )
    return FeedbackResponse(ok=True)


# ---------------------------------------------------------------------------
# Convenience: list past queries (used by Streamlit history page)
# ---------------------------------------------------------------------------


@app.get("/api/queries/recent")
def api_queries_recent(limit: int = 30) -> list[dict]:
    return db.list_queries(limit=limit)


@app.get("/api/queries/{query_id}/detail", response_model=QueryDetailResponse)
def api_query_detail(query_id: int) -> QueryDetailResponse:
    detail = db.get_query_detail(query_id)
    if not detail:
        raise HTTPException(404, f"query_id {query_id} not found")
    return detail


@app.delete("/api/queries/{query_id}", response_model=FeedbackResponse)
def api_delete_query(query_id: int) -> FeedbackResponse:
    if not db.delete_query(query_id):
        raise HTTPException(404, f"query_id {query_id} not found")
    return FeedbackResponse(ok=True)


# ---------------------------------------------------------------------------
# /api/credentials — UI-managed API keys
# ---------------------------------------------------------------------------


@app.get("/api/credentials", response_model=list[CredentialPreview])
def api_list_credentials() -> list[CredentialPreview]:
    return db.list_credentials()


@app.post("/api/credentials", response_model=CredentialPreview)
def api_upsert_credential(req: CredentialUpsertRequest) -> CredentialPreview:
    if not req.api_key.strip():
        raise HTTPException(400, "api_key cannot be empty")
    cid = db.upsert_credential(
        provider=req.provider,
        label=req.label.strip() or req.provider,
        api_key=req.api_key.strip(),
        model=(req.model or "").strip(),
        base_url=(req.base_url or "").strip(),
    )
    llm.clear_provider_cache()
    for c in db.list_credentials():
        if c.id == cid:
            return c
    raise HTTPException(500, "credential vanished after upsert")


@app.delete("/api/credentials/{cid}", response_model=FeedbackResponse)
def api_delete_credential(cid: int) -> FeedbackResponse:
    db.delete_credential(cid)
    llm.clear_provider_cache()
    return FeedbackResponse(ok=True)


@app.post("/api/credentials/{cid}/activate", response_model=CredentialPreview)
def api_activate_credential(cid: int) -> CredentialPreview:
    if not db.get_credential(cid):
        raise HTTPException(404, f"credential {cid} not found")
    db.set_active_credential(cid)
    llm.clear_provider_cache()
    for c in db.list_credentials():
        if c.id == cid:
            return c
    raise HTTPException(500, "credential vanished after activate")


# ---------------------------------------------------------------------------
# /api/favorites
# ---------------------------------------------------------------------------


@app.get("/api/favorites", response_model=list[FavoriteEntry])
def api_list_favorites() -> list[FavoriteEntry]:
    return db.list_favorites()
