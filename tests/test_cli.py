"""CLI partial-result warnings must not corrupt machine-readable output."""

import json

from typer.testing import CliRunner

from swecc_oss_explorer import cli
from tests.test_github import FakeResponse, make_client


def test_partial_json_results_warn_on_stderr(monkeypatch):
    item = {"id": 1, "title": "retained", "created_at": "2026-01-01T00:00:00Z"}
    client = make_client([
        ("/search/repositories", FakeResponse(200, {
            "items": [{"full_name": f"org/{'r' * 130}{i}"} for i in range(3)],
            "total_count": 3,
        })),
        ("/search/issues", FakeResponse(200, {"items": [item]})),
        ("/search/issues", FakeResponse(403, text="rate limit exceeded")),
    ])
    monkeypatch.setattr(cli, "GitHubClient", lambda: client)
    result = CliRunner().invoke(cli.app, ["explore", "--scope", "top", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [item]
    assert "partial results" in result.stderr
    assert "1/3 repo batches completed" in result.stderr
