"""Orchestration for scoped issue searches.

Because GitHub's search API rejects queries over 256 characters, a scope
covering many repos can't be expressed as one query. Strategy per scope:

* ``top``    — discover the top active repos, pack them greedily into chunks of
               ``repo:`` qualifiers that each fit the 256-char budget, run one
               search per chunk, and merge the results.
* ``all``    — one global query, no scoping.

Explicit ``repos`` always win over the scope and are chunked the same way.
Everything runs live; the only caching is the merged result list in memory.
"""

from __future__ import annotations

from collections.abc import Callable

from .discovery import discover, repo_full_names
from .github import GitHubClient, GitHubError
from .query import build_issue_query

QUERY_CHAR_BUDGET = 250  # GitHub hard-caps at 256; leave headroom
QUALIFIER_TEMPLATE = "repo:{name} "


def chunk_repos(repos: list[str], char_budget: int = QUERY_CHAR_BUDGET) -> list[list[str]]:
    """Greedily pack repo names into groups whose ``repo:`` qualifiers fit one query."""
    chunks: list[list[str]] = []
    current: list[str] = []
    used = 0
    for name in repos:
        cost = len(QUALIFIER_TEMPLATE.format(name=name))
        if current and used + cost > char_budget:
            chunks.append(current)
            current, used = [], 0
        current.append(name)
        used += cost
    if current:
        chunks.append(current)
    return chunks


def _merged_sort(issues: list[dict], sort: str) -> list[dict]:
    """Re-sort merged chunk results by the requested order (descending)."""
    if sort in ("created", "updated"):
        field = f"{sort}_at"
        return sorted(issues, key=lambda i: i.get(field, ""), reverse=True)
    field = "comments" if sort == "comments" else "reactions"
    return sorted(issues, key=lambda i: i.get(field, 0) or 0, reverse=True)


def search_issues_scoped(
    client: GitHubClient,
    *,
    scope: str = "all",
    labels: list[str] | None = None,
    language: str | None = None,
    repos: list[str] | None = None,
    text: str | None = None,
    created_since: str | None = None,
    sort: str = "created",
    per_page: int = 50,
    on_progress: Callable[[int, int], None] | None = None,
    on_partial: Callable[[int, int, GitHubError], None] | None = None,
) -> list[dict]:
    """Return merged, sorted issues, retaining completed batches on a later API error.

    ``on_partial`` receives the completed batch count, total batches, and error.
    Discovery, single-query, and first-batch errors still propagate.
    """
    if repos:
        chunks = _chunk_for_query(repos, labels, language, text, created_since)
    elif scope == "top":
        discovered = discover(client, scope)
        chunks = _chunk_for_query(
            sorted(repo_full_names(discovered)), labels, language, text, created_since
        )
    else:
        chunks = None

    if chunks is None:
        query = build_issue_query(
            scope=scope, labels=labels, language=language, text=text, created_since=created_since
        )
        return client.search_issues(query, sort=sort, per_page=per_page, max_pages=1)

    seen: set[int] = set()
    merged: list[dict] = []
    for idx, chunk in enumerate(chunks, start=1):
        if on_progress:
            on_progress(idx, len(chunks))
        query = build_issue_query(
            scope="all",
            labels=labels,
            language=language,
            repos=chunk,
            text=text,
            created_since=created_since,
        )
        try:
            batch = client.search_issues(query, sort=sort, per_page=per_page, max_pages=1)
        except GitHubError as err:
            if idx == 1:
                raise
            if on_partial:
                on_partial(idx - 1, len(chunks), err)
            break
        for issue in batch:
            if issue.get("id") not in seen:
                seen.add(issue["id"])
                merged.append(issue)
    return _merged_sort(merged, sort)


def _chunk_for_query(
    repos: list[str],
    labels: list[str] | None,
    language: str | None,
    text: str | None,
    created_since: str | None = None,
) -> list[list[str]]:
    """Chunk repos so that prefix + repo qualifiers fits GitHub's 256-char cap."""
    base = build_issue_query(
        scope="all", labels=labels, language=language, text=text, created_since=created_since
    )
    return chunk_repos(repos, char_budget=256 - len(base))
