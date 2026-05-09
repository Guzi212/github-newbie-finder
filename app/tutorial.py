"""Tutorial generator.

One LLM call. Inputs: the user profile + the repo snapshot (README + tree +
install signals). Output: a `TutorialPlan`.

Per spec §12: nothing is auto-executed. Every step that depends on un-confirmed
information (e.g. "this command might be needed") sets `needs_verification=true`
so the UI can render a warning.
"""

from __future__ import annotations

from . import db, github
from .llm import call_structured
from .schemas import RepoSnapshot, TutorialPlan, UserProfile


def _payload(snap: RepoSnapshot, profile: UserProfile) -> dict:
    return {
        "user_profile": profile.model_dump(),
        "snapshot": {
            "meta": snap.meta.model_dump(mode="json"),
            "install_signals": snap.install_signals.model_dump(),
            "tree": snap.tree[:60],
            "readme_excerpt": (snap.readme or "")[:6000],
        },
    }


def generate_tutorial(
    full_name: str,
    profile: UserProfile,
    *,
    user_id: int | None = None,
    query_id: int | None = None,
) -> tuple[TutorialPlan, RepoSnapshot]:
    """Fetch (or reuse) the snapshot and produce a TutorialPlan."""
    snap = github.fetch_snapshot(full_name)
    plan = call_structured("generate_tutorial", _payload(snap, profile), TutorialPlan)

    # Make sure the repo_full_name field is set even if the LLM omitted it.
    if not plan.repo_full_name:
        plan.repo_full_name = full_name

    db.save_tutorial(plan, user_id, query_id=query_id)
    return plan, snap
