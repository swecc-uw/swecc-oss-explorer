"""``swecc-oss`` command-line interface."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .discovery import SCOPES, discover
from .explore import search_issues_scoped
from .github import GitHubClient, GitHubError, RateLimitError
from .query import DEFAULT_LABELS, SORT_CHOICES
from .timeutil import days_ago_iso, parse_age

app = typer.Typer(
    name="swecc-oss",
    help="Browse open issues across SWECC repos and the most active OSS projects — live.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
error_console = Console(stderr=True)


def _age(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "?"
    days = (datetime.now(timezone.utc) - then).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day"
    if days < 30:
        return f"{days}d"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _labels(issue: dict) -> str:
    return ", ".join(label.get("name", "") for label in issue.get("labels", []))


@app.command()
def explore(
    scope: str = typer.Option(
        "top",
        "--scope",
        "-s",
        help="Repo scope to search: top (top 100 active) or all.",
        callback=lambda v: v if v in SCOPES else typer.BadParameter(f"must be one of {SCOPES}"),
    ),
    label: list[str] = typer.Option(
        None,
        "--label",
        "-l",
        help="Filter by label (repeatable). Default: good first issue + help wanted.",
    ),
    language: str | None = typer.Option(
        None, "--language", "-L", help="Filter by language, e.g. python."
    ),
    repo: list[str] = typer.Option(
        None, "--repo", "-r", help="Restrict to owner/name (repeatable; overrides --scope)."
    ),
    text: str | None = typer.Option(
        None, "--query", "-q", help="Free-text query, e.g. 'pagination'."
    ),
    max_age: str = typer.Option(
        "2mo",
        "--max-age",
        help="Only issues created within this window: <N>d/w/mo/y (e.g. 30d, 2w, 3mo) or 'any'.",
    ),
    sort: str = typer.Option(
        "created", "--sort", help=f"Sort order: {', '.join(SORT_CHOICES)}."
    ),
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=1000, help="Max issues to show."),
    show_all: bool = typer.Option(
        False, "--all", help="Drop the default label filter (with --label, additive)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
) -> typer.Exit:
    """Search open issues live across the selected scope."""
    labels = list(label) if label else ([] if show_all else list(DEFAULT_LABELS))
    effective_repos = list(repo) if repo else None

    try:
        age_days = parse_age(max_age)
    except ValueError as err:
        console.print(f"[bold red]Error:[/bold red] {err}")
        raise typer.Exit(2) from err
    created_since = days_ago_iso(age_days) if age_days is not None else None

    client = GitHubClient()
    partial = []
    status = console.status("[bold blue]Searching GitHub…")
    try:
        status.start()
        issues = search_issues_scoped(
            client,
            scope=scope,
            labels=labels,
            language=language,
            repos=effective_repos,
            text=text,
            created_since=created_since,
            sort=sort,
            per_page=100,
            on_progress=lambda done, total: status.update(
                status=f"[bold blue]Searching GitHub… repo batch {done}/{total}"
            ),
            on_partial=lambda done, total, err: partial.append((done, total, err)),
        )
        issues = issues[:limit]
    except (RateLimitError, GitHubError) as err:
        console.print(f"[bold red]Error:[/bold red] {err}")
        raise typer.Exit(1) from err
    finally:
        status.stop()

    if partial:
        done, total, err = partial[0]
        error_console.print(
            f"Warning: showing partial results; {done}/{total} repo batches completed. {err}",
            markup=False,
        )

    if json_output:
        console.print_json(json.dumps(issues))
        return

    if not issues:
        message = (
            "No issues found in the completed batches."
            if partial else "No issues found for these filters."
        )
        console.print(f"[yellow]{message}[/yellow]")
        return

    table = Table(title=f"{len(issues)} open issues", header_style="bold magenta")
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", max_width=60)
    table.add_column("Labels", style="yellow", max_width=28)
    table.add_column("Age", justify="right", style="dim")
    table.add_column("Comments", justify="right")

    for issue in issues:
        repo_name = issue.get("repository_url", "").removeprefix("https://api.github.com/repos/")
        table.add_row(
            repo_name,
            str(issue.get("number", "")),
            issue.get("title", ""),
            _labels(issue),
            _age(issue.get("created_at", "")),
            str(issue.get("comments", 0)),
        )
    console.print(table)
    console.print("[dim]Tip: GITHUB_TOKEN raises search limits (10/min → 30/min).[/dim]")


@app.command()
def repos(
    scope: str = typer.Option(
        "top", "--scope", "-s", help="Which repos to list: top or all.",
        callback=lambda v: v if v in SCOPES else typer.BadParameter(f"must be one of {SCOPES}"),
    ),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=1000),
    json_output: bool = typer.Option(False, "--json"),
) -> typer.Exit:
    """List the repos the explorer would scan for a scope (resolved live)."""
    client = GitHubClient()
    try:
        with console.status("[bold blue]Discovering repos…"):
            discovered = discover(client, scope, limit=limit)
    except (RateLimitError, GitHubError) as err:
        console.print(f"[bold red]Error:[/bold red] {err}")
        raise typer.Exit(1) from err

    if json_output:
        console.print_json(json.dumps(discovered))
        return

    table = Table(title=f"{scope} scope — {len(discovered)} repos", header_style="bold magenta")
    table.add_column("Repo", style="cyan")
    table.add_column("Stars", justify="right")
    table.add_column("Language", style="green")
    table.add_column("Description", max_width=60)
    for r in discovered:
        table.add_row(
            r.get("full_name", ""),
            str(r.get("stargazers_count", 0)),
            r.get("language") or "—",
            (r.get("description") or "")[:60],
        )
    console.print(table)


@app.command()
def web() -> typer.Exit:
    """Print the URL of the web explorer."""
    console.print("https://swecc-uw.github.io/swecc-oss-explorer/")


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"swecc-oss-explorer {__version__}")


if __name__ == "__main__":
    app()
