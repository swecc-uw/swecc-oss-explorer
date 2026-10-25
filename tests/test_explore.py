"""Tests for chunked multi-repo searching — network calls are mocked."""

import pytest

from swecc_oss_explorer.explore import chunk_repos, search_issues_scoped
from swecc_oss_explorer.github import GitHubError, RateLimitError
from tests.test_github import FakeResponse, make_client


class TestChunkRepos:
    def test_short_names_pack_tightly(self):
        chunks = chunk_repos(["a/b", "c/d", "e/f"])
        assert chunks == [["a/b", "c/d", "e/f"]]

    def test_respects_char_budget(self):
        repos = [f"verylongorganizationname/repo-{i:04d}" for i in range(30)]
        for chunk in chunk_repos(repos):
            cost = sum(len(f"repo:{r} ") for r in chunk)
            assert cost <= 250
            assert len(chunk) < 30  # actually split into multiple chunks

    def test_handles_long_single_repo(self):
        one = ["x/" + "r" * 300]
        assert chunk_repos(one) == [one]


def issue(n):
    return {"id": n, "title": f"issue {n}", "created_at": f"2026-01-0{n}T00:00:00Z"}


class TestSearchIssuesScoped:
    EMPTY = {"items": [], "total_count": 0}

    def test_all_scope_uses_single_query(self):
        client = make_client([("/search/issues", FakeResponse(200, self.EMPTY))])
        search_issues_scoped(client, scope="all")
        assert len(client.session.calls) == 1
        assert "repo:" not in client.session.calls[0][1]["q"]

    def test_created_since_is_passed_to_query(self):
        client = make_client([("/search/issues", FakeResponse(200, self.EMPTY))])
        search_issues_scoped(client, scope="all", created_since="2026-08-24")
        assert "created:>=2026-08-24" in client.session.calls[0][1]["q"]

    def test_rejects_removed_swecc_scope(self):
        client = make_client([("/search/issues", FakeResponse(200, self.EMPTY))])
        with pytest.raises(ValueError):
            search_issues_scoped(client, scope="swecc")

    def test_top_scope_issues_chunked_and_merged(self):
        # Discovery returns 3 repos -> 1 chunk; each search returns one issue.
        repo_payload = {
            "items": [{"full_name": f"a/r{i}"} for i in range(3)],
            "total_count": 3,
        }
        client = make_client([
            ("/search/repositories", FakeResponse(200, repo_payload)),
            ("/search/issues", FakeResponse(200, {"items": [issue(1), issue(2)],
                                                 "total_count": 2})),
        ])
        issues = search_issues_scoped(client, scope="top")
        assert len(issues) == 2
        # discovery query + one chunk query
        assert len(client.session.calls) == 2

    def test_duplicates_are_deduped_across_chunks(self):
        repo_payload = {
            "items": [{"full_name": f"o/r{i}"} for i in range(40)],  # forces 2+ chunks
            "total_count": 40,
        }
        dup = {"id": 1, "title": "same", "created_at": "2026-01-01T00:00:00Z"}
        client = make_client([
            ("/search/repositories", FakeResponse(200, repo_payload)),
            ("/search/issues", FakeResponse(200, {"items": [dup], "total_count": 1})),
        ])
        issues = search_issues_scoped(client, scope="top")
        assert len(issues) == 1

    def test_merged_results_resort_by_created(self):
        repo_payload = {
            "items": [{"full_name": f"o/r{i}"} for i in range(40)],
            "total_count": 40,
        }
        old = {"id": 1, "created_at": "2026-01-01T00:00:00Z"}
        new = {"id": 2, "created_at": "2026-02-01T00:00:00Z"}
        client = make_client([
            ("/search/repositories", FakeResponse(200, repo_payload)),
            ("/search/issues", FakeResponse(200, {"items": [old], "total_count": 1})),
            ("/search/issues", FakeResponse(200, {"items": [new], "total_count": 1})),
        ])
        issues = search_issues_scoped(client, scope="top", sort="created")
        assert [i["id"] for i in issues] == [2, 1]

    def test_on_progress_reports_batch(self):
        repo_payload = {
            "items": [{"full_name": f"o/r{i}"} for i in range(40)],
            "total_count": 40,
        }
        client = make_client([
            ("/search/repositories", FakeResponse(200, repo_payload)),
            ("/search/issues", FakeResponse(200, {"items": [], "total_count": 0})),
        ])
        seen = []
        search_issues_scoped(client, scope="top", on_progress=lambda d, t: seen.append((d, t)))
        assert seen[0] == (1, len(seen))

    def test_github_errors_propagate(self):
        client = make_client([("/search/issues", FakeResponse(422, {"message": "bad"}))])
        with pytest.raises(GitHubError):
            search_issues_scoped(client, scope="all")

    @pytest.mark.parametrize("status, text, error_type", [
        (403, "API rate limit exceeded", RateLimitError),
        (500, "server unavailable", GitHubError),
    ])
    def test_later_batch_failure_preserves_sorted_deduplicated_results(
        self, status, text, error_type
    ):
        repos = [f"org/{'r' * 130}{i}" for i in range(4)]
        client = make_client([
            ("/search/issues", FakeResponse(200, {"items": [issue(1)]})),
            ("/search/issues", FakeResponse(200, {"items": [issue(1), issue(2)]})),
            ("/search/issues", FakeResponse(status, text=text)),
        ])
        partial = []
        result = search_issues_scoped(
            client, repos=repos,
            on_partial=lambda done, total, err: partial.append((done, total, err)),
        )
        assert [item["id"] for item in result] == [2, 1]
        assert len(client.session.calls) == 3  # do not keep querying after failure
        assert len(partial) == 1
        assert partial[0][:2] == (2, 4)
        assert isinstance(partial[0][2], error_type)

    def test_first_batch_failure_still_raises(self):
        client = make_client([("/search/issues", FakeResponse(500))])
        with pytest.raises(GitHubError):
            search_issues_scoped(client, repos=["org/repo"])

    def test_successful_empty_batch_is_still_partial_progress(self):
        client = make_client([
            ("/search/issues", FakeResponse(200, self.EMPTY)),
            ("/search/issues", FakeResponse(429, text="rate limit exceeded")),
        ])
        partial = []
        result = search_issues_scoped(
            client, repos=[f"org/{'r' * 130}{i}" for i in range(2)],
            on_partial=lambda done, total, err: partial.append((done, total)),
        )
        assert result == []
        assert partial == [(1, 2)]
