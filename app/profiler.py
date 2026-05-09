"""Repo profiler.

Pure heuristics over the README + top-level file tree → `InstallSignals`.
These signals feed both the rule scorer and the tutorial generator.

Kept deterministic and dependency-free so it's easy to unit test.
"""

from __future__ import annotations

import re
from typing import Iterable

from .schemas import InstallSignals, RepoMeta


_DOCKERFILE_RE = re.compile(r"(?:^|/)(?:Dockerfile|dockerfile)(\.[\w.-]+)?$")
_COMPOSE_NAMES = {
    "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml",
}
_PACKAGE_FILES = {
    "requirements.txt": "pip",
    "pyproject.toml": "poetry/pep517",
    "package.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "Cargo.toml": "cargo",
    "go.mod": "go",
    "Gemfile": "bundler",
    "composer.json": "composer",
    "Pipfile": "pipenv",
}
_ONE_CLICK_PATTERNS = [
    r"\bone[-\s]?click\b",
    r"\bquick[-\s]?start\b",
    r"\bgetting[-\s]?started\b",
    r"\binstall\.sh\b",
    r"\bsetup\.sh\b",
]


def _has_match(patterns: Iterable[str], text: str) -> bool:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


def extract_signals(*, readme: str, tree: list[str], meta: RepoMeta) -> InstallSignals:
    tree_lower = {name.lower() for name in tree}
    readme = readme or ""

    has_dockerfile = any(_DOCKERFILE_RE.search(name) for name in tree) \
        or "Dockerfile" in tree
    has_compose = any(name.lower() in _COMPOSE_NAMES for name in tree)
    has_one_click_script = (
        any(name in tree for name in ("install.sh", "setup.sh", "start.sh"))
        or _has_match(_ONE_CLICK_PATTERNS, readme)
    )

    package_managers: list[str] = []
    for name, label in _PACKAGE_FILES.items():
        if name.lower() in tree_lower:
            package_managers.append(label)

    needs_gpu = bool(re.search(r"\b(cuda|nvidia|gpu|cudnn|rtx|a100|h100)\b", readme, re.I))
    needs_api_key = bool(re.search(
        r"\b(api[-_ ]?key|openai_api_key|anthropic_api_key|secret\s*token|ACCESS_TOKEN)\b",
        readme, re.I,
    ))
    needs_database = bool(re.search(
        r"\b(postgres|mysql|mariadb|sqlite|redis|mongodb|qdrant|chroma|pgvector)\b",
        readme, re.I,
    ))
    has_screenshots = bool(re.search(r"!\[[^\]]*\]\([^)]+\.(png|jpg|jpeg|gif|webp)\)", readme, re.I)) \
        or bool(re.search(r"<img[^>]+src=", readme, re.I))
    has_demo = bool(re.search(r"\b(demo|live\s*demo|preview|playground)\b", readme, re.I))
    documented_env_vars = bool(re.search(r"^\s*[A-Z][A-Z0-9_]+=", readme, re.M)) \
        or ".env.example" in tree_lower

    detected_languages = []
    if meta.language:
        detected_languages.append(meta.language)
    for hint in ("python", "typescript", "javascript", "go", "rust", "java", "ruby", "php"):
        if hint in readme.lower() and hint.title() not in detected_languages \
           and hint not in [x.lower() for x in detected_languages]:
            detected_languages.append(hint)

    return InstallSignals(
        has_dockerfile=has_dockerfile,
        has_compose=has_compose,
        has_one_click_script=has_one_click_script,
        package_managers=package_managers,
        needs_gpu=needs_gpu,
        needs_api_key=needs_api_key,
        needs_database=needs_database,
        has_screenshots=has_screenshots,
        has_demo=has_demo,
        documented_env_vars=documented_env_vars,
        readme_length=len(readme),
        detected_languages=detected_languages,
    )


def excerpt(readme: str, anchor: str, *, span: int = 220) -> str:
    """Return a short excerpt of `readme` around the first match of `anchor`."""
    if not readme:
        return ""
    idx = readme.lower().find(anchor.lower())
    if idx < 0:
        return readme[:span].strip()
    start = max(0, idx - span // 3)
    end = min(len(readme), idx + span)
    return readme[start:end].replace("\n\n", "\n").strip()
