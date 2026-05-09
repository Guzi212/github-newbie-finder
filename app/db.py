"""SQLite persistence layer (SQLAlchemy core).

The 8 tables mirror the data model in spec §8, plus `llm_calls` so every LLM
call we make is persisted and replayable.

Each accessor returns plain dicts/Pydantic models, never ORM rows — this keeps
the rest of the app schema-driven.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete as sql_delete,
    desc,
    event,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine

from .config import get_settings
from .schemas import (
    CredentialPreview,
    Evidence,
    FavoriteEntry,
    GitHubQuery,
    InstallSignals,
    LLMCallRecord,
    QueryDetailResponse,
    Recommendation,
    RepoMeta,
    RepoSnapshot,
    RequirementParseResult,
    RuleScore,
    TutorialPlan,
    TutorialPointer,
    UserProfile,
)

metadata = MetaData()


users_t = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("profile_json", Text, nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
)

queries_t = Table(
    "queries", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True),
    Column("raw_text", Text, nullable=False),
    Column("parsed_json", Text, nullable=False),
    Column("queries_json", Text, nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
)

repos_t = Table(
    "repos", metadata,
    Column("full_name", String(255), primary_key=True),
    Column("url", String(500), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("stars", Integer, nullable=False, default=0),
    Column("forks", Integer, nullable=False, default=0),
    Column("license", String(120), nullable=True),
    Column("topics_json", Text, nullable=False, default="[]"),
    Column("language", String(60), nullable=True),
    Column("pushed_at", DateTime, nullable=True),
    Column("archived", Boolean, nullable=False, default=False),
    Column("found_via_query", String(500), nullable=True),
    Column("updated_at", DateTime, nullable=False, default=datetime.utcnow),
)

repo_snapshots_t = Table(
    "repo_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("full_name", String(255), nullable=False, index=True),
    Column("readme", Text, nullable=False, default=""),
    Column("tree_json", Text, nullable=False, default="[]"),
    Column("install_signals_json", Text, nullable=False, default="{}"),
    Column("fetched_at", DateTime, nullable=False, default=datetime.utcnow),
)

recommendations_t = Table(
    "recommendations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("query_id", Integer, nullable=False, index=True),
    Column("full_name", String(255), nullable=False),
    Column("rule_score_json", Text, nullable=False),
    Column("rerank_json", Text, nullable=True),
    Column("final_rank", Integer, nullable=False),
    Column("final_score", Float, nullable=False),
    Column("rationale", Text, nullable=False, default=""),
    Column("evidence_json", Text, nullable=False, default="[]"),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
)

tutorials_t = Table(
    "tutorials", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("full_name", String(255), nullable=False),
    Column("user_id", Integer, nullable=True),
    Column("query_id", Integer, nullable=True, index=True),
    Column("plan_json", Text, nullable=False),
    Column("assumptions_json", Text, nullable=False, default="{}"),
    Column("generated_at", DateTime, nullable=False, default=datetime.utcnow),
)

feedback_t = Table(
    "feedback", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True),
    Column("full_name", String(255), nullable=False),
    Column("action", String(40), nullable=False),
    Column("success_status", Boolean, nullable=True),
    Column("note", Text, nullable=False, default=""),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
)

llm_calls_t = Table(
    "llm_calls", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(40), nullable=False, index=True),
    Column("provider", String(40), nullable=False),
    Column("model", String(120), nullable=False),
    Column("prompt_json", Text, nullable=False),
    Column("response_json", Text, nullable=False),
    Column("latency_ms", Integer, nullable=False, default=0),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
)

api_credentials_t = Table(
    "api_credentials", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider", String(40), nullable=False),
    Column("label", String(120), nullable=False),
    Column("api_key", Text, nullable=False),
    Column("model", String(120), nullable=False, default=""),
    Column("base_url", String(255), nullable=False, default=""),
    Column("is_active", Boolean, nullable=False, default=False),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
)

app_settings_t = Table(
    "app_settings", metadata,
    Column("key", String(80), primary_key=True),
    Column("value", Text, nullable=False, default=""),
)


_engine: Optional[Engine] = None


def _sqlite_path_from_url(url: str) -> Optional[str]:
    if not url.startswith("sqlite:///"):
        return None
    p = url.replace("sqlite:///", "", 1)
    return p if p and p != ":memory:" else None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        if settings.database_url.startswith("sqlite"):
            # FastAPI runs sync route handlers in a thread pool; SQLite needs
            # this to allow connections to be passed across threads.
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.database_url,
            future=True,
            connect_args=connect_args,
        )
        if settings.database_url.startswith("sqlite"):
            # WAL mode survives concurrent reads-while-writes better and is
            # more forgiving if the file is replaced out from under us.
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):
                cursor = dbapi_conn.cursor()
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA foreign_keys=ON")
                finally:
                    cursor.close()
            # The DB file holds plaintext API keys — restrict to owner-only.
            path = _sqlite_path_from_url(settings.database_url)
            if path and os.path.exists(path):
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
    return _engine


def init_db() -> None:
    eng = get_engine()
    metadata.create_all(eng)
    _migrate_legacy(eng)


def _migrate_legacy(eng: Engine) -> None:
    """Best-effort schema upgrades for pre-existing finder.db files."""
    insp = inspect(eng)
    if "tutorials" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("tutorials")}
        if "query_id" not in cols:
            with eng.begin() as conn:
                conn.execute(text("ALTER TABLE tutorials ADD COLUMN query_id INTEGER"))


def _dumps(obj: Any) -> str:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(s: Optional[str], default: Any = None) -> Any:
    if not s:
        return default
    return json.loads(s)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def upsert_user(profile: UserProfile) -> int:
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(
            insert(users_t).values(
                profile_json=_dumps(profile),
                created_at=datetime.utcnow(),
            )
        )
        return int(result.inserted_primary_key[0])


def get_user(user_id: int) -> Optional[UserProfile]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(users_t).where(users_t.c.id == user_id)).first()
        if not row:
            return None
        return UserProfile(**_loads(row.profile_json, {}))


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def save_query(
    *,
    raw_text: str,
    parsed: RequirementParseResult,
    queries: Iterable[GitHubQuery],
    user_id: Optional[int] = None,
) -> int:
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(
            insert(queries_t).values(
                user_id=user_id,
                raw_text=raw_text,
                parsed_json=_dumps(parsed),
                queries_json=_dumps([q.model_dump() for q in queries]),
                created_at=datetime.utcnow(),
            )
        )
        return int(result.inserted_primary_key[0])


def get_query(query_id: int) -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(queries_t).where(queries_t.c.id == query_id)).first()
        if not row:
            return None
        return {
            "id": row.id,
            "user_id": row.user_id,
            "raw_text": row.raw_text,
            "parsed": RequirementParseResult(**_loads(row.parsed_json, {})),
            "queries": [GitHubQuery(**q) for q in _loads(row.queries_json, [])],
            "created_at": row.created_at,
        }


def delete_query(query_id: int) -> bool:
    """Delete a query and its descendant recommendations + tutorials.

    Returns True if a row was actually deleted, False if the id was unknown.
    Feedback rows are keyed by full_name (not query_id) so we leave them alone.
    """
    eng = get_engine()
    with eng.begin() as conn:
        existed = conn.execute(
            select(queries_t.c.id).where(queries_t.c.id == query_id)
        ).first()
        if not existed:
            return False
        conn.execute(
            sql_delete(recommendations_t).where(recommendations_t.c.query_id == query_id)
        )
        conn.execute(
            sql_delete(tutorials_t).where(tutorials_t.c.query_id == query_id)
        )
        conn.execute(sql_delete(queries_t).where(queries_t.c.id == query_id))
        return True


def list_queries(limit: int = 50) -> list[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(queries_t).order_by(desc(queries_t.c.id)).limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "raw_text": r.raw_text,
                "created_at": r.created_at,
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# repos + snapshots
# ---------------------------------------------------------------------------


def upsert_repo(meta: RepoMeta) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        existing = conn.execute(
            select(repos_t.c.full_name).where(repos_t.c.full_name == meta.full_name)
        ).first()
        values = dict(
            url=meta.url,
            description=meta.description or "",
            stars=meta.stars,
            forks=meta.forks,
            license=meta.license,
            topics_json=_dumps(meta.topics),
            language=meta.language,
            pushed_at=meta.pushed_at,
            archived=meta.archived,
            found_via_query=meta.found_via_query,
            updated_at=datetime.utcnow(),
        )
        if existing:
            conn.execute(
                update(repos_t).where(repos_t.c.full_name == meta.full_name).values(**values)
            )
        else:
            conn.execute(
                insert(repos_t).values(full_name=meta.full_name, **values)
            )


def get_repo_meta(full_name: str) -> Optional[RepoMeta]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            select(repos_t).where(repos_t.c.full_name == full_name)
        ).first()
        if not row:
            return None
        return RepoMeta(
            full_name=row.full_name,
            url=row.url,
            description=row.description or "",
            stars=row.stars,
            forks=row.forks,
            license=row.license,
            topics=_loads(row.topics_json, []),
            language=row.language,
            pushed_at=row.pushed_at,
            archived=row.archived,
            found_via_query=row.found_via_query,
        )


def save_snapshot(snapshot: RepoSnapshot) -> int:
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(
            insert(repo_snapshots_t).values(
                full_name=snapshot.meta.full_name,
                readme=snapshot.readme or "",
                tree_json=_dumps(snapshot.tree),
                install_signals_json=_dumps(snapshot.install_signals),
                fetched_at=snapshot.fetched_at,
            )
        )
        return int(result.inserted_primary_key[0])


def get_fresh_snapshot(full_name: str, ttl_hours: int) -> Optional[RepoSnapshot]:
    """Return the most recent snapshot if it is within the TTL, else None."""
    eng = get_engine()
    cutoff = datetime.utcnow() - timedelta(hours=ttl_hours)
    with eng.connect() as conn:
        row = conn.execute(
            select(repo_snapshots_t)
            .where(repo_snapshots_t.c.full_name == full_name)
            .where(repo_snapshots_t.c.fetched_at >= cutoff)
            .order_by(desc(repo_snapshots_t.c.id))
            .limit(1)
        ).first()
        if not row:
            return None
        meta = get_repo_meta(full_name)
        if not meta:
            return None
        return RepoSnapshot(
            meta=meta,
            readme=row.readme or "",
            tree=_loads(row.tree_json, []),
            install_signals=InstallSignals(**_loads(row.install_signals_json, {})),
            fetched_at=row.fetched_at,
        )


# ---------------------------------------------------------------------------
# recommendations
# ---------------------------------------------------------------------------


def save_recommendations(query_id: int, recs: list[Recommendation]) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        # Replace previous rows for this query.
        conn.execute(
            recommendations_t.delete().where(recommendations_t.c.query_id == query_id)
        )
        for r in recs:
            conn.execute(
                insert(recommendations_t).values(
                    query_id=query_id,
                    full_name=r.repo.full_name,
                    rule_score_json=_dumps(r.rule_score),
                    rerank_json=_dumps(r.rerank) if r.rerank else None,
                    final_rank=r.final_rank,
                    final_score=r.final_score,
                    rationale=r.rationale,
                    evidence_json=_dumps([e.model_dump() for e in r.evidence]),
                    created_at=datetime.utcnow(),
                )
            )


def get_recommendations(query_id: int) -> list[Recommendation]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(recommendations_t)
            .where(recommendations_t.c.query_id == query_id)
            .order_by(recommendations_t.c.final_rank)
        ).all()
        out: list[Recommendation] = []
        for r in rows:
            meta = get_repo_meta(r.full_name)
            if not meta:
                continue
            from .schemas import RepoAnalysisResult  # avoid circular import order
            rerank_data = _loads(r.rerank_json, None)
            out.append(
                Recommendation(
                    repo=meta,
                    rule_score=RuleScore(**_loads(r.rule_score_json, {})),
                    rerank=RepoAnalysisResult(**rerank_data) if rerank_data else None,
                    final_rank=r.final_rank,
                    final_score=r.final_score,
                    rationale=r.rationale or "",
                    evidence=[Evidence(**e) for e in _loads(r.evidence_json, [])],
                )
            )
        return out


# ---------------------------------------------------------------------------
# tutorials
# ---------------------------------------------------------------------------


def save_tutorial(
    plan: TutorialPlan,
    user_id: Optional[int],
    *,
    query_id: Optional[int] = None,
) -> int:
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(
            insert(tutorials_t).values(
                full_name=plan.repo_full_name,
                user_id=user_id,
                query_id=query_id,
                plan_json=_dumps(plan),
                assumptions_json=_dumps(plan.assumptions),
                generated_at=datetime.utcnow(),
            )
        )
        return int(result.inserted_primary_key[0])


def list_tutorials_for_query(query_id: int) -> list[TutorialPointer]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(tutorials_t.c.full_name, tutorials_t.c.generated_at)
            .where(tutorials_t.c.query_id == query_id)
            .order_by(desc(tutorials_t.c.generated_at))
        ).all()
        # De-duplicate by full_name, keeping the most recent run.
        seen: set[str] = set()
        out: list[TutorialPointer] = []
        for r in rows:
            if r.full_name in seen:
                continue
            seen.add(r.full_name)
            out.append(TutorialPointer(full_name=r.full_name, generated_at=r.generated_at))
        return out


def get_query_detail(query_id: int) -> Optional[QueryDetailResponse]:
    record = get_query(query_id)
    if not record:
        return None
    recs = get_recommendations(query_id)
    tuts = list_tutorials_for_query(query_id)
    return QueryDetailResponse(
        query_id=record["id"],
        raw_text=record["raw_text"],
        parsed=record["parsed"],
        recommendations=recs,
        tutorials=tuts,
    )


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------


def save_feedback(
    *,
    full_name: str,
    action: str,
    user_id: Optional[int] = None,
    success_status: Optional[bool] = None,
    note: str = "",
) -> int:
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(
            insert(feedback_t).values(
                user_id=user_id,
                full_name=full_name,
                action=action,
                success_status=success_status,
                note=note,
                created_at=datetime.utcnow(),
            )
        )
        return int(result.inserted_primary_key[0])


def list_favorites() -> list[FavoriteEntry]:
    """Repos that are currently favorited.

    A repo is *currently* favorited if its most recent feedback row in
    {favorite, unfavorite} is `favorite`. We attach the repo meta and the
    rationale from its most recent recommendation if either exists.
    """
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(feedback_t)
            .where(feedback_t.c.action.in_(("favorite", "unfavorite")))
            .order_by(desc(feedback_t.c.id))
        ).all()
        latest: dict[str, Any] = {}
        for r in rows:
            if r.full_name in latest:
                continue
            latest[r.full_name] = r
        out: list[FavoriteEntry] = []
        for full_name, r in latest.items():
            if r.action != "favorite":
                continue
            meta = get_repo_meta(full_name)
            rec_row = conn.execute(
                select(recommendations_t.c.rationale)
                .where(recommendations_t.c.full_name == full_name)
                .order_by(desc(recommendations_t.c.id))
                .limit(1)
            ).first()
            out.append(FavoriteEntry(
                full_name=full_name,
                repo=meta,
                last_rationale=(rec_row.rationale if rec_row else "") or "",
                favorited_at=r.created_at,
            ))
        return out


def is_favorited(full_name: str) -> bool:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            select(feedback_t.c.action)
            .where(feedback_t.c.full_name == full_name)
            .where(feedback_t.c.action.in_(("favorite", "unfavorite")))
            .order_by(desc(feedback_t.c.id))
            .limit(1)
        ).first()
        return bool(row and row.action == "favorite")


# ---------------------------------------------------------------------------
# api_credentials
# ---------------------------------------------------------------------------


def _key_preview(api_key: str) -> str:
    if not api_key:
        return ""
    tail = api_key[-4:]
    return f"***{tail}"


def list_credentials() -> list[CredentialPreview]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(api_credentials_t).order_by(desc(api_credentials_t.c.id))
        ).all()
        return [
            CredentialPreview(
                id=r.id,
                provider=r.provider,
                label=r.label,
                model=r.model or "",
                base_url=r.base_url or "",
                key_preview=_key_preview(r.api_key),
                is_active=bool(r.is_active),
                created_at=r.created_at,
            )
            for r in rows
        ]


def get_credential(cid: int) -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            select(api_credentials_t).where(api_credentials_t.c.id == cid)
        ).first()
        if not row:
            return None
        return {
            "id": row.id,
            "provider": row.provider,
            "label": row.label,
            "api_key": row.api_key,
            "model": row.model or "",
            "base_url": row.base_url or "",
            "is_active": bool(row.is_active),
            "created_at": row.created_at,
        }


def upsert_credential(
    *,
    provider: str,
    label: str,
    api_key: str,
    model: str = "",
    base_url: str = "",
) -> int:
    """Upsert by (provider, label). New rows are inactive by default; if no
    other credential is active and this is the first row, activate it."""
    eng = get_engine()
    with eng.begin() as conn:
        existing = conn.execute(
            select(api_credentials_t).where(
                and_(
                    api_credentials_t.c.provider == provider,
                    api_credentials_t.c.label == label,
                )
            )
        ).first()
        if existing:
            conn.execute(
                update(api_credentials_t)
                .where(api_credentials_t.c.id == existing.id)
                .values(api_key=api_key, model=model, base_url=base_url)
            )
            return int(existing.id)
        result = conn.execute(
            insert(api_credentials_t).values(
                provider=provider,
                label=label,
                api_key=api_key,
                model=model,
                base_url=base_url,
                is_active=False,
                created_at=datetime.utcnow(),
            )
        )
        new_id = int(result.inserted_primary_key[0])
        # First credential ever? Make it active automatically.
        any_active = conn.execute(
            select(api_credentials_t.c.id).where(api_credentials_t.c.is_active.is_(True))
        ).first()
        if not any_active:
            conn.execute(
                update(api_credentials_t)
                .where(api_credentials_t.c.id == new_id)
                .values(is_active=True)
            )
        return new_id


def delete_credential(cid: int) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(sql_delete(api_credentials_t).where(api_credentials_t.c.id == cid))


def set_active_credential(cid: int) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(update(api_credentials_t).values(is_active=False))
        conn.execute(
            update(api_credentials_t)
            .where(api_credentials_t.c.id == cid)
            .values(is_active=True)
        )


def get_active_credential() -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            select(api_credentials_t).where(api_credentials_t.c.is_active.is_(True)).limit(1)
        ).first()
        if not row:
            return None
        return {
            "id": row.id,
            "provider": row.provider,
            "label": row.label,
            "api_key": row.api_key,
            "model": row.model or "",
            "base_url": row.base_url or "",
            "is_active": True,
            "created_at": row.created_at,
        }


# ---------------------------------------------------------------------------
# llm_calls (replay)
# ---------------------------------------------------------------------------


def save_llm_call(record: LLMCallRecord) -> int:
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(
            insert(llm_calls_t).values(
                kind=record.kind,
                provider=record.provider,
                model=record.model,
                prompt_json=_dumps(record.prompt),
                response_json=_dumps(record.response),
                latency_ms=record.latency_ms,
                created_at=record.created_at,
            )
        )
        return int(result.inserted_primary_key[0])


def get_llm_call(call_id: int) -> Optional[LLMCallRecord]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            select(llm_calls_t).where(llm_calls_t.c.id == call_id)
        ).first()
        if not row:
            return None
        return LLMCallRecord(
            id=row.id,
            kind=row.kind,
            provider=row.provider,
            model=row.model,
            prompt=_loads(row.prompt_json, {}),
            response=_loads(row.response_json, {}),
            latency_ms=row.latency_ms,
            created_at=row.created_at,
        )
