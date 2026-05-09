"""GitHub REST client.

Two responsibilities:

- `recall(queries)`: run the planner's queries against the search API and
  return a deduped list of `RepoMeta`. The query that surfaced each repo is
  recorded on `RepoMeta.found_via_query` (spec §12 risk: "candidate recall must
  record which query produced each candidate").
- `fetch_snapshot(full_name)`: fetch README + top-level tree + topics +
  license, returning a `RepoSnapshot`. Snapshots are cached in SQLite for
  `snapshot_ttl_hours` to stay within rate limits.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any, Iterable

import httpx

from . import db, profiler
from .config import get_settings
from .schemas import GitHubQuery, RepoMeta, RepoSnapshot

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"


def _headers() -> dict[str, str]:
    s = get_settings()
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-project-finder-mvp",
    }
    if s.github_token:
        h["Authorization"] = f"Bearer {s.github_token}"
    return h


def _client() -> httpx.Client:
    return httpx.Client(headers=_headers(), timeout=20.0, follow_redirects=True)


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


def _parse_pushed_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _meta_from_search_item(item: dict[str, Any], q: str) -> RepoMeta:
    license_obj = item.get("license") or {}
    return RepoMeta(
        full_name=item["full_name"],
        url=item.get("html_url") or f"https://github.com/{item['full_name']}",
        description=item.get("description") or "",
        stars=int(item.get("stargazers_count") or 0),
        forks=int(item.get("forks_count") or 0),
        license=license_obj.get("spdx_id") if isinstance(license_obj, dict) else None,
        topics=item.get("topics") or [],
        language=item.get("language"),
        pushed_at=_parse_pushed_at(item.get("pushed_at")),
        archived=bool(item.get("archived")),
        found_via_query=q,
    )


def recall(queries: Iterable[GitHubQuery]) -> list[RepoMeta]:
    s = get_settings()
    seen: dict[str, RepoMeta] = {}
    queries = list(queries)
    if not queries:
        return []

    with _client() as client:
        for gq in queries:
            if len(seen) >= s.recall_max_total:
                break
            params: dict[str, Any] = {
                "q": gq.q,
                "per_page": min(s.recall_per_query, 30),
            }
            if gq.sort == "stars":
                params["sort"] = "stars"
                params["order"] = "desc"
            elif gq.sort == "updated":
                params["sort"] = "updated"
                params["order"] = "desc"
            try:
                r = client.get(f"{GITHUB_API}/search/repositories", params=params)
            except httpx.HTTPError as e:
                log.warning("Search failed for %r: %s", gq.q, e)
                continue
            if r.status_code == 422:
                log.info("Skipping unparseable query %r", gq.q)
                continue
            if r.status_code == 403:
                log.warning("GitHub rate-limited (status 403). Stopping recall early.")
                break
            if r.status_code >= 400:
                log.warning("Search %r returned %s", gq.q, r.status_code)
                continue
            for item in (r.json() or {}).get("items", []):
                full = item.get("full_name")
                if not full or full in seen:
                    continue
                if item.get("archived"):
                    continue
                meta = _meta_from_search_item(item, gq.q)
                seen[full] = meta
                db.upsert_repo(meta)
                if len(seen) >= s.recall_max_total:
                    break

    return list(seen.values())


# ---------------------------------------------------------------------------
# Snapshot fetch (README + tree + signals)
# ---------------------------------------------------------------------------


_README_CANDIDATES = ["README.md", "README.rst", "README.txt", "README", "readme.md"]


def _try_raw_readme(client: httpx.Client, full_name: str, default_branch: str) -> str:
    for name in _README_CANDIDATES:
        for branch in (default_branch, "main", "master"):
            url = f"{RAW_BASE}/{full_name}/{branch}/{name}"
            try:
                r = client.get(url)
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and r.text.strip():
                return r.text
    return ""


def _api_readme(client: httpx.Client, full_name: str) -> str:
    try:
        r = client.get(f"{GITHUB_API}/repos/{full_name}/readme")
    except httpx.HTTPError as e:
        log.warning("readme fetch failed for %s: %s", full_name, e)
        return ""
    if r.status_code != 200:
        return ""
    payload = r.json() or {}
    if payload.get("encoding") == "base64" and payload.get("content"):
        try:
            return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def _list_tree(client: httpx.Client, full_name: str, branch: str) -> list[str]:
    try:
        r = client.get(f"{GITHUB_API}/repos/{full_name}/contents", params={"ref": branch})
    except httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    return [entry.get("name", "") for entry in r.json() or [] if entry.get("name")]


def fetch_snapshot(full_name: str, *, force: bool = False) -> RepoSnapshot:
    s = get_settings()
    if not force:
        cached = db.get_fresh_snapshot(full_name, s.snapshot_ttl_hours)
        if cached:
            return cached

    with _client() as client:
        # repo metadata (in case we don't have it cached from search)
        try:
            r = client.get(f"{GITHUB_API}/repos/{full_name}")
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to GET repo {full_name}: {e}") from e
        if r.status_code != 200:
            raise RuntimeError(f"GitHub returned {r.status_code} for {full_name}")
        repo_json = r.json() or {}

        license_obj = repo_json.get("license") or {}
        meta = RepoMeta(
            full_name=repo_json.get("full_name", full_name),
            url=repo_json.get("html_url") or f"https://github.com/{full_name}",
            description=repo_json.get("description") or "",
            stars=int(repo_json.get("stargazers_count") or 0),
            forks=int(repo_json.get("forks_count") or 0),
            license=license_obj.get("spdx_id") if isinstance(license_obj, dict) else None,
            topics=repo_json.get("topics") or [],
            language=repo_json.get("language"),
            pushed_at=_parse_pushed_at(repo_json.get("pushed_at")),
            archived=bool(repo_json.get("archived")),
        )
        db.upsert_repo(meta)

        default_branch = repo_json.get("default_branch") or "main"
        readme = _try_raw_readme(client, full_name, default_branch)
        if not readme:
            readme = _api_readme(client, full_name)

        tree = _list_tree(client, full_name, default_branch)

    signals = profiler.extract_signals(readme=readme, tree=tree, meta=meta)
    snap = RepoSnapshot(
        meta=meta,
        readme=readme,
        tree=tree,
        install_signals=signals,
        fetched_at=datetime.utcnow(),
    )
    db.save_snapshot(snap)
    return snap


def fetch_snapshots(full_names: Iterable[str], *, max_failures: int = 15) -> list[RepoSnapshot]:
    """Fetch snapshots in sequence, tolerating individual failures.

    A 403 here usually means GitHub is rate-limiting the unauthenticated
    client. We log a clearer message but keep going so a single throttled
    repo doesn't starve the rest of the candidate pool.
    """
    out: list[RepoSnapshot] = []
    failures = 0
    rate_limited = 0
    for fn in full_names:
        try:
            out.append(fetch_snapshot(fn))
        except Exception as e:
            failures += 1
            msg = str(e)
            if "403" in msg:
                rate_limited += 1
                log.warning(
                    "snapshot %s rate-limited (403). Set GITHUB_TOKEN in .env to avoid this.",
                    fn,
                )
            else:
                log.warning("snapshot %s failed: %s", fn, e)
            if failures >= max_failures:
                log.warning(
                    "Too many snapshot failures (rate-limited=%d). Stopping early.",
                    rate_limited,
                )
                break
    if rate_limited:
        log.info(
            "Hit %d GitHub rate-limit responses. Add GITHUB_TOKEN to .env "
            "for higher limits (5000/hr authenticated vs 60/hr unauthenticated).",
            rate_limited,
        )
    return out
